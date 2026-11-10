#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare a buildlog build of ggml with a CMake baseline (the ggml case study).

    compare.py BASELINE_DIR BUILDLOG_BUILD_DIR CONFIG [--src SRC] [--out FILE]

BASELINE_DIR is baseline/<name> (compile-commands.jsonl, commands.txt,
exported-symbols.txt); BUILDLOG_BUILD_DIR is a `buildlog configure -C` dir,
built; CONFIG is its configuration directory name (arm64-macos.release.none).
For every object both builds compile (keyed by target and source), it
reports the defines, include dirs and other flags each has and the other
lacks, grouped so that one line covers every object with the same
difference; then each linked artifact's link inputs; then each shared
library's and module's global symbols (`nm -gU`) and load commands
(`otool -L`). Paths are normalized to $SRC and $BUILD; toolchain spelling
(compiler path, -arch vs --target/-isysroot, depfile flags) is dropped from
both sides and listed once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from collections import defaultdict
from pathlib import Path

SPELLING = {"-arch", "-isysroot", "-MD", "-MF", "-MT", "-c", "-o"}


def classify(args: list[str], src: str, build: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"define": set(), "include": set(), "flag": set()}
    i = 1
    while i < len(args):
        a = args[i]
        if a in SPELLING:
            i += 2 if a not in {"-MD", "-c"} else 1
            continue
        if a.startswith("--target="):
            i += 1
            continue
        if a.startswith(("-D", "-U")):
            out["define"].add(a)
        elif a in {"-I", "-isystem", "-F"}:
            out["include"].add(norm_dir(args[i + 1], src, build))
            i += 1
        elif a.startswith(("-I", "-F")):
            out["include"].add(norm_dir(a[2:], src, build))
        elif a.startswith("-std="):
            out["flag"].add(a)
        elif not a.startswith("-"):
            pass  # the source
        else:
            out["flag"].add(a)
        i += 1
    return out


def norm_dir(d: str, src: str, build: str) -> str:
    d = os.path.normpath(d.replace("$SRC", src).replace("$BUILD", build))
    if d.startswith(src):
        return "$SRC" + d[len(src) :]
    if "/gen/" in d or d.startswith(build):
        return "<generated>"
    if "SDKs" in d:
        return "<sdk frameworks>"
    return d


def cmake_objects(base: Path, src: str) -> dict[tuple[str, str], list[str]]:
    objs = {}
    for line in (base / "compile-commands.jsonl").read_text().splitlines():
        e = json.loads(line)
        m = re.search(r"CMakeFiles/([^/]+)\.dir/", e["output"])
        rel = e["file"].replace("$SRC/", "")
        objs[(m.group(1), rel)] = e["args"]
    return objs


def buildlog_objects(build: Path, cfg: str, src: str) -> dict[tuple[str, str], list[str]]:
    souffle = build / "souffle"
    db = json.loads(
        subprocess.check_output(["ninja", "-C", str(souffle), "-t", "compdb"], text=True)
    )
    objs = {}
    for e in db:
        out = e.get("output", "")
        m = re.match(rf"obj/{re.escape(cfg)}/([^/]+)/(.*)\.o$", out)
        if not m:
            continue
        args = shlex.split(e["command"])
        fixed = []
        for a in args:
            for pre in ("-I", ""):
                if a.startswith((pre + "../", pre + "gen/")):
                    a = pre + os.path.normpath(os.path.join(souffle, a[len(pre) :]))
                    break
            fixed.append(a)
        objs[(m.group(1), m.group(2))] = fixed
    return objs


def compile_report(cm, bl, src, cmake_build, bl_build) -> list[str]:
    lines = []
    both = sorted(set(cm) & set(bl))
    lines.append(
        f"objects: cmake {len(cm)} (all targets), buildlog {len(bl)}, compared {len(both)}"
    )
    only_bl = sorted(set(bl) - set(cm))
    if only_bl:
        lines.append("  only buildlog: " + ", ".join(f"{t}:{s}" for t, s in only_bl))
    groups: dict[tuple, list[str]] = defaultdict(list)
    for key in both:
        a = classify(cm[key], src, cmake_build)
        b = classify(bl[key], src, str(bl_build))
        diff = []
        for kind in ("define", "include", "flag"):
            ca, cb = a[kind] - b[kind], b[kind] - a[kind]
            if ca or cb:
                diff.append((kind, tuple(sorted(ca)), tuple(sorted(cb))))
        groups[tuple(diff)].append(f"{key[0]}:{key[1]}")
    for diff, keys in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        targets = sorted({k.split(":")[0] for k in keys})
        lines.append(f"- {len(keys)} objects of {', '.join(targets)}")
        if not diff:
            lines.append("    identical defines, include dirs and flags")
        for kind, ca, cb in diff:
            if ca:
                lines.append(f"    {kind} only cmake:    {' '.join(ca)}")
            if cb:
                lines.append(f"    {kind} only buildlog: {' '.join(cb)}")
    return lines


LINK_OUT = re.compile(r" -o (\S+)")


def link_items(cmd: str) -> list[str]:
    toks = shlex.split(cmd)
    keep = []
    skip = False
    for i, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t in {
            "-o",
            "-install_name",
            "-current_version",
            "-compatibility_version",
            "-isysroot",
            "-arch",
        }:
            skip = True
            continue
        if t.endswith((".o", "/c++", "/clang++", "/cc", "/clang")) or t.startswith("--target="):
            continue
        if t in {":", "&&"}:
            continue
        if t in {"-O3", "-DNDEBUG"}:
            continue
        keep.append(re.sub(r"^.*/(lib[^/]+)$", r"\1", t))
    return keep


def link_report(base: Path, build: Path, cfg: str) -> list[str]:
    lines = []
    cm = {}
    for line in (base / "commands.txt").read_text().splitlines():
        if " -dynamiclib " in line or " -bundle " in line or "-Wl,-search_paths_first" in line:
            m = LINK_OUT.search(line)
            cm[Path(m.group(1)).name] = link_items(line)
    souffle = build / "souffle"
    cmds = subprocess.check_output(["ninja", "-C", str(souffle), "-t", "commands"], text=True)
    bl = {}
    for line in cmds.splitlines():
        if (
            " -dynamiclib " in line or " -bundle " in line or " -o bin/" in line
        ) and f"/{cfg}/" in line:
            if " -c " in line or "create_symlink" in line:
                continue
            m = LINK_OUT.search(line)
            bl[Path(m.group(1)).name] = link_items(line)
    for name in sorted(set(cm) & set(bl)):
        a, b = cm[name], bl[name]
        lines.append(f"- {name}")
        lines.append(f"    cmake:    {' '.join(a)}")
        lines.append(f"    buildlog: {' '.join(b)}")
    miss = sorted(set(bl) - set(cm))
    if miss:
        lines.append("  linked only by buildlog: " + ", ".join(miss))
    return lines


def symbols(path: Path) -> list[str]:
    out = subprocess.check_output(["nm", "-gU", str(path)], text=True)
    return sorted(line.split()[-1] for line in out.splitlines() if line.strip())


def symbol_report(base: Path, build: Path, cfg: str) -> list[str]:
    cm: dict[str, list[str]] = {}
    cur = None
    for line in (base / "exported-symbols.txt").read_text().splitlines():
        if line.startswith("===== "):
            cur = line[6:].strip()
            cm[cur] = []
        elif cur and line:
            cm[cur].append(line)
    lines = []
    out = build / "souffle"
    for name in sorted(cm):
        cands = [out / "lib" / cfg / name]
        f = next((c for c in cands if c.exists()), None)
        if f is None:
            lines.append(f"- {name}: not built by buildlog")
            continue
        b = symbols(f)
        a = sorted(cm[name])
        same = "same" if a == b else f"differ (-{len(set(a) - set(b))} +{len(set(b) - set(a))})"
        lines.append(f"- {name}: {len(a)} vs {len(b)} global symbols, {same}")
        loads = subprocess.check_output(["otool", "-L", str(f)], text=True).splitlines()[1:]
        lines.append("    loads: " + ", ".join(sorted(x.split()[0] for x in loads)))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", type=Path)
    ap.add_argument("build", type=Path)
    ap.add_argument("config")
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent / "upstream"))
    ap.add_argument("--out", type=Path)
    ns = ap.parse_args()
    src = os.path.realpath(ns.src)
    cmake_build = "$BUILD"
    cm = cmake_objects(ns.baseline, src)
    bl = buildlog_objects(ns.build, ns.config, src)
    lines = [
        f"# {ns.baseline.name} (CMake) vs buildlog {ns.config}",
        "# not compared (toolchain spelling): compiler path, -arch vs --target/-isysroot,",
        "# -o/-c/-MD/-MF/-MT",
        "",
        "## compiles",
        *compile_report(cm, bl, src, cmake_build, ns.build.resolve()),
        "",
        "## links",
        *link_report(ns.baseline, ns.build, ns.config),
        "",
        "## global symbols and load commands",
        *symbol_report(ns.baseline, ns.build, ns.config),
    ]
    text = "\n".join(lines) + "\n"
    if ns.out:
        ns.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
