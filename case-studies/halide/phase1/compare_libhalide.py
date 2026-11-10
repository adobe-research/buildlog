# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Normalize buildlog's libHalide graph and compare it with the CMake baseline.

    uv run python case-studies/halide/phase1/compare_libhalide.py BUILDLOG_BUILD_DIR \
        [--variant release|debug|asan] [--phase N ...] [--details N] [--json OUT.json] [--strict]

BUILDLOG_BUILD_DIR is the -C directory of `buildlog configure
case-studies/halide/libhalide`. The script
  1. maps every output of the buildlog graph to the target that owns its action
     (buildlog's target_action; the project's target names are CMake's), or
     "Halide" for edges no target owns (a buildlog graph has no CMake File API;
     normalize.py's --target-map is for this); the runtime's edges and objects
     (under gen/ and obj/ .../Halide/runtime/) map to CMake's OBJECT library
     Halide_initmod, which buildlog folds into Halide; targets named by their
     output name (phase 4: gengen, autoschedule_<name>) get CMake's names;
     each target gets its CMake source directory, read from the baseline (the
     File API's dir, which scope.toml's dir rules match; phase 4);
  2. runs normalize.py with the baseline's roots ($SRC is the pinned source tree,
     $LLVM is /opt/homebrew/opt/llvm@22, or /usr/lib/llvm-22 for the linux-* variants,
     whose realpath normalize.py also maps) and
     one extra root, $CMAKE, for the CMake build tree that supplies phase 2/3
     inputs (external objects, include/flatc);
  3. fuses each `buildlog -E copy_if_different TMP OUT` edge with the edge that
     alone produces TMP into one record (steps: the producer's, then the copy;
     main step: the producer's). CMake writes the same thing as one custom
     command of several steps (Halide.h: make_directory, build_halide_h > .tmp,
     copy_if_different, rm), buildlog as two edges, the copy with restat;
  4. runs compare.py --phase N (default 1; repeatable) against
     baseline/cmake-<variant>.jsonl (default release) with
     pathmap-libhalide.toml (only output-layout renames; see that file), or with
     no path map at all under --strict. The path map names the release
     configuration's directory (arm64-macos.release.none); for another
     variant it is rewritten to that build's configuration directory (phase
     7a: the build has one configuration, e.g. arm64-macos.debug.none).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE.parent / "baseline"
# The pinned source is the submodule; the CMake trees are baseline/run-cmake.sh's
# (its HALIDE_BUILD_ROOT).
SRC = os.environ.get("HALIDE_SRC") or str(HERE.parent / "upstream")
CMAKE_TREES = os.path.expanduser(
    os.environ.get("HALIDE_BUILD_ROOT", str(HERE.parents[2] / "build" / "case-halide"))
)
LLVM = "/opt/homebrew/opt/llvm@22"
# The Linux image's LLVM (apt.llvm.org), for the linux-* variants of docker/run-cmake-linux.sh.
LINUX_LLVM = "/usr/lib/llvm-22"
RUNTIME = "/Halide/runtime/"
# buildlog names artifacts by target, so these targets carry CMake's
# OUTPUT_NAME (phase 4).
CMAKE_NAMES = {
    "gengen": "Halide_GenRT",
    "autoschedule_adams2019": "Halide_Adams2019",
    "autoschedule_anderson2021": "Halide_Anderson2021",
    "autoschedule_li2018": "Halide_Li2018",
    "autoschedule_mullapudi2016": "Halide_Mullapudi2016",
}


def ninja_outputs(path: Path) -> list[str]:
    outs = []
    for line in path.read_text().splitlines():
        if line.startswith("build ") and ":" in line:
            head = line[len("build ") :].split(":", 1)[0]
            outs += [o for o in head.replace("|", " ").split() if o]
    return outs


def owners(facts: Path) -> dict[str, str]:
    """Action id (an out: path) -> the target that owns it."""
    own = {}
    for line in (facts / "target_action.csv").read_text().splitlines():
        t, _, a = line.split("\t")
        own[a.removeprefix("out:")] = CMAKE_NAMES.get(t, t)
    return own


def target_dirs(baseline: Path) -> dict[str, str]:
    """CMake target -> its source directory, as the baseline's File API has it."""
    dirs = {}
    for line in baseline.read_text().splitlines():
        r = json.loads(line)
        if r.get("target") and str(r.get("target_via", "")).startswith("fileapi"):
            dirs.setdefault(r["target"], r["dir"])
    return dirs


def only_config(build_ninja: Path, drop: list[str], out: Path) -> Path:
    """A copy of build.ninja without the edges that build another configuration's
    files (a cross build's host tools: CMake's tree 1, which the product's baseline
    does not contain; the product's edges still read their outputs as plain files)."""
    blocks, cur = [], []
    for line in build_ninja.read_text().splitlines(keepends=True):
        if line.startswith("build ") and cur:
            blocks.append(cur)
            cur = []
        cur.append(line)
    blocks.append(cur)
    out.write_text(
        "".join(
            "".join(b)
            for b in blocks
            if not (b[0].startswith("build ") and any(f"/{d}/" in b[0].split(":")[0] for d in drop))
        )
    )
    return out


def config_dir(facts: Path, only: str | None = None) -> str:
    """The one configuration directory of a native single-variant build (the
    configuration of every needed target); `only` names it in a build of several."""
    if only:
        return only
    names = dict(line.split("\t") for line in (facts / "cfg_name.csv").read_text().splitlines())
    used = {names[line.split("\t")[1]] for line in (facts / "needed.csv").read_text().splitlines()}
    if len(used) != 1:
        sys.exit(f"expected one configuration in the build, got {sorted(used)}")
    return used.pop()


def sample_closure(facts: Path, scope: Path, baseline: Path) -> list[str]:
    """The sampled tests' dependency closure in the buildlog graph (scope.toml's
    `sampled` condition; normalize.py computes it from the File API for CMake):
    resolved dependencies (dep_resolves) and the tools a target runs (tool_dep,
    inferred from use), in CMake's names."""
    edges: dict[str, set[str]] = {}
    for name, cols in (("dep_resolves.csv", (0, 2)), ("tool_dep.csv", (0, 2))):
        for line in (facts / name).read_text().splitlines():
            f = line.split("\t")
            edges.setdefault(f[cols[0]], set()).add(f[cols[1]])
    tests = tomllib.loads(scope.read_text())["tests"]
    todo = list(tests["correctness"]) + list(tests["generator"])
    seen: set[str] = set()
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += edges.get(n, ())
    # The baseline's own phase 6 targets stay in scope even when the buildlog
    # graph has no edge to them (_test_internal: CMake's tests depend on it for
    # PCH reuse; buildlog has no PCH).
    for line in baseline.read_text().splitlines():
        r = json.loads(line)
        if r.get("phase") == 6 and r.get("target"):
            seen.add(r["target"])
    return sorted(CMAKE_NAMES.get(t, t) for t in seen)


def initmod(out: str) -> str | None:
    """The runtime's edges (phase 3) belong to CMake's OBJECT library
    Halide_initmod; buildlog has no object libraries and compiles the embeds
    into Halide, which owns them (target_action)."""
    return "Halide_initmod" if RUNTIME in os.path.normpath(out) else None


def fuse_copy_if_different(path: Path) -> int:
    """Step 3 of the module docstring, in place; returns how many were fused."""
    recs = [json.loads(line) for line in path.read_text().splitlines() if line]
    by_out = {o: r for r in recs for o in r.get("outputs", [])}
    drop, fused = set(), 0
    for r in recs:
        steps = r.get("steps") or []
        argv = steps[0]["argv"] if len(steps) == 1 else []
        if argv[1:5] != ["-m", "buildlog", "-E", "copy_if_different"] or len(argv) != 7:
            continue
        t = by_out.get(argv[5])
        if t is None or t.get("outputs") != [argv[5]] or not t.get("steps"):
            continue
        r["steps"] = [*t["steps"], *steps]
        for k in ("kind", "main", "tool", "tool_name", "tool_built", "runs_built"):
            if k in t:
                r[k] = t[k]
        r.pop("argv", None)
        ins = {k: sorted(set(r["inputs"].get(k, [])) | set(t["inputs"].get(k, [])) - {argv[5]})
               for k in ("explicit", "implicit", "order_only")}  # fmt: skip
        r["inputs"] = ins
        drop.add(t["id"])
        fused += 1
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in recs if r["id"] not in drop)
    )
    return fused


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("build_dir", type=Path)
    ap.add_argument("--phase", type=int, action="append", help="phase(s) to compare (default 1)")
    ap.add_argument("--variant", default="release", help="CMake baseline variant (default release)")
    ap.add_argument("--llvm", help="LLVM root (default: by variant, macOS or linux-*)")
    ap.add_argument(
        "--config",
        help="compare only this configuration's edges (a cross build: the product's, "
        "e.g. arm64-linux.release.none)",
    )
    ap.add_argument("--sysroot", help="the cross build's sysroot (the root $SYSROOT)")
    ap.add_argument("--details", type=int, default=5)
    ap.add_argument("--full", action="store_true", help="keep every field of excluded records")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--strict", action="store_true", help="compare without the path map")
    ap.add_argument(
        "--out", type=Path, help="normalized JSONL (default: BUILD_DIR/libhalide.jsonl)"
    )
    args = ap.parse_args(argv)

    ninja_dir = (args.build_dir / "souffle").resolve()
    build_ninja = ninja_dir / "build.ninja"
    if args.config:
        names = {
            line.split("\t")[1]
            for line in (ninja_dir / "facts" / "cfg_name.csv").read_text().splitlines()
        }
        build_ninja = only_config(
            build_ninja, sorted(names - {args.config}), args.build_dir / "libhalide-product.ninja"
        )
    out = args.out or args.build_dir / "libhalide.jsonl"
    own = owners(ninja_dir / "facts")
    tmap = {
        f"$BUILD/{os.path.normpath(o)}": initmod(o) or own.get(os.path.normpath(o), "Halide")
        for o in ninja_outputs(build_ninja)
    }
    tmap_file = args.build_dir / "libhalide-targets.json"
    tmap_file.write_text(json.dumps(tmap, indent=0, sort_keys=True))
    dirs_file = args.build_dir / "libhalide-target-dirs.json"
    baseline = BASELINE / f"cmake-{args.variant}.jsonl"
    dirs = target_dirs(baseline)
    # A stub is its own (interface) target in buildlog, part of the generator's
    # target in CMake (add_halide_generator's STUB).
    for t in set(own.values()):
        if t.endswith(".stub") and t not in dirs and f"{t[:-5]}.generator" in dirs:
            dirs[t] = dirs[f"{t[:-5]}.generator"]
    dirs_file.write_text(json.dumps(dirs, indent=0))

    closure_file = args.build_dir / "libhalide-closure.json"
    closure_file.write_text(
        json.dumps(sample_closure(ninja_dir / "facts", BASELINE / "scope.toml", baseline))
    )

    llvm = args.llvm or (LINUX_LLVM if args.variant.startswith("linux-") else LLVM)
    cmake_tree = Path(f"{CMAKE_TREES}/cmake-{args.variant}")
    roots = ["--root", f"$CMAKE={cmake_tree}"] if cmake_tree.is_dir() else []
    # The tier-2 LLVM packages sit in overlay prefixes beside the real install; the
    # graph spells their tools and includes with the overlay, CMake with the install.
    for overlay in sorted((HERE.parent / "llvm-install").glob("*-2[12]")):
        roots += ["--root", f"$LLVM={overlay}"]
    if args.sysroot:
        roots += ["--root", f"$SYSROOT={args.sysroot}"]
    py = sys.executable
    subprocess.run(
        [py, str(BASELINE / "normalize.py"), "normalize",
         "--build-ninja", str(build_ninja), "--build", str(ninja_dir), "--src", SRC,
         "--llvm", llvm, "--scope", str(BASELINE / "scope.toml"),
         "--variant", args.variant, "--target-map", str(tmap_file),
         "--target-dirs", str(dirs_file), "--target-closure", str(closure_file),
         *roots, *(["--full"] if args.full else []), "-o", str(out)],
        check=True,
    )  # fmt: skip
    fuse_copy_if_different(out)
    cmd = [py, str(BASELINE / "compare.py"), str(baseline), str(out),
           "--details", str(args.details)]  # fmt: skip
    for ph in args.phase or [1]:
        cmd += ["--phase", str(ph)]
    if not args.strict:
        pathmap = HERE / "pathmap-libhalide.toml"
        cfg = config_dir(ninja_dir / "facts", args.config)
        if cfg != "arm64-macos.release.none":  # the path map names the macOS release directory
            text = pathmap.read_text().replace(
                r"arm64-macos\.release\.none", cfg.replace(".", r"\.")
            )
            pathmap = args.build_dir / "libhalide-pathmap.toml"
            pathmap.write_text(text)
        cmd += ["--map", str(pathmap)]
    if args.json:
        cmd += ["--json", str(args.json)]
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
