# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Option 2 prototype: derive the Halide_LLVM facts from `llvm-config`.

    uv run --frozen python option2_llvm_config.py inputs/native-arm64.json [--hybrid]

Three parts, kept apart so their sizes can be counted:
- GENERIC: run a tool query and keep its output words with the query as the
  origin (`tool_query`), and map compiler flags to props (`flag_props`, the
  pkg-config importer's Cflags mapping). Nothing here knows LLVM.
- LLVM KNOWLEDGE: which llvm-config queries mean what, and the install layout
  llvm-config does not report (clang-<major>, lld's separate prefix and file
  names). Needed by any consumer of LLVM, not only Halide.
- HALIDE RULES: FindHalide_LLVM.cmake's own derivation (the definitions, the
  components, shared-if-WebAssembly-and-dylib, LLD/Clang version = LLVM version).

The rows use the importers' `res_pkg_*` vocabulary and the Option 1 renames, so
they can be compared with raw/option1-<name>/rows.json. `--hybrid` also writes
the Soufflé inputs for hybrid/halide_llvm.dl (llvm-config words plus the
generated export files read by buildlog-cpp's own CMake-export importer) and runs it.

Outputs: raw/option2-<name>/{rows.json,queries.json}, raw/hybrid-<name>/.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import config_values
from buildlog_cpp.importers import pkg_import

HERE = Path(__file__).resolve().parent

# ---- GENERIC --------------------------------------------------------------------


class Queries:
    """Cached tool queries; each output word keeps `tool args` as its origin."""

    def __init__(self):
        self.log: list[dict] = []
        self.seconds = 0.0

    def words(self, tool: str, *args: str) -> list[str]:
        t0 = time.perf_counter()
        p = subprocess.run(
            [tool, *args],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.seconds += time.perf_counter() - t0
        dt = round(time.perf_counter() - t0, 4)
        self.log.append(
            {"argv": [tool, *args], "rc": p.returncode, "s": dt, "out": p.stdout.strip()}
        )
        if p.returncode:
            raise SystemExit(f"{tool} {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout.split()

    def one(self, tool: str, *args: str) -> str:
        w = self.words(tool, *args)
        if len(w) != 1:
            raise SystemExit(f"{tool} {' '.join(args)}: expected one word, got {w}")
        return w[0]


class Rows:
    def __init__(self):
        self.rows: dict[str, list[tuple]] = {}
        self.origin: dict[str, str] = {}

    def add(self, rel: str, *values: str, origin: str) -> None:
        row = tuple(values)
        rs = self.rows.setdefault(rel, [])
        if row not in rs:
            rs.append(row)
            self.origin[rel + "\t" + "\t".join(row)] = origin


def flag_props(rows: Rows, t: str, words: list[str], origin: str, drop=()) -> None:
    """Compile flags as props: -I/-isystem include dirs, -D defines, other flags."""
    for w in words:
        if w.startswith("-D") and w[2:] not in drop:
            rows.add("res_pkg_prop", t, "cc.define", w[2:], "", origin=origin)
        elif w.startswith("-isystem"):
            rows.add("res_pkg_prop", t, "cc.include_dir", w[8:], "", origin=origin)
        elif w.startswith("-I"):
            rows.add("res_pkg_prop", t, "cc.include_dir", w[2:], "", origin=origin)
        elif not w.startswith("-D"):
            rows.add("res_pkg_prop", t, "cc.compile_flag", w, "", origin=origin)


# ---- LLVM KNOWLEDGE ---------------------------------------------------------------


def llvm_facts(q: Queries, llvm_config: str) -> dict:
    ver = q.one(llvm_config, "--version")
    major, minor, patch = ver.split("git")[0].split(".")
    bindir = q.one(llvm_config, "--bindir")
    return {
        "version": ver,
        "major": major,
        "minor": minor,
        "patch": patch,
        "cppflags": q.words(llvm_config, "--cppflags"),
        "includedir": q.one(llvm_config, "--includedir"),
        "libdir": q.one(llvm_config, "--libdir"),
        "bindir": bindir,
        "cmakedir": q.one(llvm_config, "--cmakedir"),
        "shared_mode": q.one(llvm_config, "--shared-mode"),
        "rtti": q.one(llvm_config, "--has-rtti"),
        "targets": q.words(llvm_config, "--targets-built"),
        "dylib": q.one(llvm_config, "--link-shared", "--libfiles"),
        # Not reported by llvm-config: the clang driver is installed as
        # clang-<major> (bin/clang is a symlink to it); llvm-as is a plain tool.
        "clang": f"{bindir}/clang-{major}",
        "llvm_as": f"{bindir}/llvm-as",
    }


def lld_facts(q: Queries, lld_root: str) -> dict:
    # lld is a separate package (Homebrew lld@N); llvm-config knows nothing
    # about it. Its version comes from running it, its layout is lld's.
    root = os.path.realpath(lld_root)
    words = q.words(f"{root}/bin/ld.lld", "--version")
    return {
        "root": root,
        "version": next(w for w in words if w[:1].isdigit()),
        "include": f"{root}/include",
        "libs": {n: f"{root}/lib/lib{n}.dylib" for n in ("lldWasm", "lldCommon")},
    }


def clang_version(q: Queries, clang: str) -> str:
    words = q.words(clang, "--version")
    return words[words.index("version") + 1]


# ---- HALIDE RULES (FindHalide_LLVM.cmake) --------------------------------------------


def halide_rules(rows: Rows, llvm: dict, lld: dict, clang_ver: str, reg: dict, lc: str) -> None:
    rn = reg["rename"]
    o = lambda *a: f"{lc} {' '.join(a)}"
    want = reg["args"]
    i_req, i_opt = want.index("COMPONENTS"), want.index("OPTIONAL_COMPONENTS")
    required, optional = want[i_req + 1 : i_opt], want[i_opt + 1 :]
    if clang_ver != llvm["version"]:
        rows.add("error", f"Clang {clang_ver} != LLVM {llvm['version']}", origin=llvm["clang"])
    dylib = llvm["shared_mode"] == "shared"
    shared = "WebAssembly" in llvm["targets"] and dylib
    found = []
    for comp in required + optional:
        ok = comp in llvm["targets"]
        if comp == "WebAssembly":
            ok = ok and lld["version"] == llvm["version"] and not (dylib and not shared)
        if ok:
            found.append(comp)
        elif comp in required:
            rows.add(
                "error", f"required LLVM component {comp} not found", origin=o("--targets-built")
            )
    core = "Halide_LLVM::Core"
    rows.add("res_pkg_target", core, "interface", origin="rule")
    # LLVM_DEFINITIONS is the -D part of --cppflags; Halide takes its include
    # directory from LLVM_INCLUDE_DIRS (--includedir), and none of --cxxflags.
    defines = [w for w in llvm["cppflags"] if w.startswith("-D")]
    flag_props(rows, core, defines, o("--cppflags"), drop=("_GLIBCXX_ASSERTIONS",))
    rows.add(
        "res_pkg_prop",
        core,
        "cc.define",
        f"LLVM_VERSION={llvm['major']}{llvm['minor']}",
        "",
        origin=o("--version"),
    )
    rows.add(
        "res_pkg_prop", core, "cc.include_dir", llvm["includedir"], "", origin=o("--includedir")
    )
    if not shared:
        raise SystemExit("static LLVM (per-component --libfiles) is not prototyped")
    rows.add("res_pkg_dep", core, rn["LLVM"], "public", "", origin="rule: shared")
    for comp in found:
        t = f"Halide_LLVM::{comp}"
        rows.add("res_pkg_target", t, "interface", origin=o("--targets-built"))
        rows.add("res_pkg_dep", t, core, "public", "", origin="rule")
        if comp == "WebAssembly":
            rows.add("res_pkg_prop", t, "cc.include_dir", lld["include"], "", origin="lld layout")
            for n in ("lldWasm", "lldCommon"):
                rows.add("res_pkg_dep", t, rn[n], "public", "", origin="rule")
    # The artifacts: locations from llvm-config and the lld/clang layout rules.
    rel = "import:native.opt=release"
    rows.add("res_pkg_condition", rel, "$Eq(native.opt, release)", origin="rule")
    for name, kind, path in [
        ("LLVM", "library", llvm["dylib"]),
        ("clang", "executable", llvm["clang"]),
        ("llvm-as", "executable", llvm["llvm_as"]),
        *[(n, "library", p) for n, p in lld["libs"].items()],
    ]:
        rows.add("res_pkg_target", rn[name], kind, origin="rule")
        rows.add("res_pkg_location", rn[name], path, rel, origin="layout")
        if kind == "library":
            d = os.path.dirname(path)
            rows.add(
                "res_pkg_prop",
                rn[name],
                "native.link_flag",
                f"-Wl,-rpath,{d}",
                rel,
                origin="rule: rpath = libdir",
            )
    for key, value in [
        ("halide_llvm.version", f"{llvm['major']}.{llvm['minor']}.{llvm['patch']}"),
        ("halide_llvm.components", ";".join(found)),
        ("halide_llvm.shared_libs", "YES" if shared else "NO"),
        ("halide_llvm.rtti", "ON" if llvm["rtti"] == "YES" else "OFF"),
    ]:
        rows.add("res_probe_setting", key, value, origin=o("..."))


# ---- the existing CMake-export importer on the generated files ------------------------


def import_exports(files: list[str]) -> dict:
    out = {}
    for f in files:
        t0 = time.perf_counter()
        r = pkg_import(
            None, "r", ("cmake", f), {"res_platform_constraint": {("r", "p", "os", "macos")}}
        )
        out[f] = {
            "status": r["res_pkg_import"][0][1],
            "seconds": round(time.perf_counter() - t0, 3),
            "errors": [e[1] for e in r["res_pkg_error"]],
            "target_errors": [e[2] for e in r["res_pkg_target_error"]],
            "counts": {k: len(v) for k, v in r.items() if k != "__origin__" and v},
            "result": r,
        }
    return out


# ---- hybrid: Soufflé over llvm-config words + imported export facts -----------------


def tool_words(q: Queries, lc: str) -> list[tuple]:
    words = []
    for key, args in [
        ("version", ["--version"]),
        ("cppflags", ["--cppflags"]),
        ("includedir", ["--includedir"]),
        ("shared-mode", ["--shared-mode"]),
        ("has-rtti", ["--has-rtti"]),
        ("targets-built", ["--targets-built"]),
    ]:
        ws = q.words(lc, *args)
        if key == "version":
            ws = ws[0].split(".")
        words += [(key, i, w) for i, w in enumerate(ws)]
    return words


def config_words(llvm_root: str, lld_root: str) -> tuple[list[tuple], str, str, dict]:
    """The same words, read statically from the configured *Config.cmake files."""
    cm = f"{llvm_root}/lib/cmake"
    v = config_values.read(
        Path(f"{cm}/llvm/LLVMConfig.cmake"),
        [
            "LLVM_VERSION_MAJOR",
            "LLVM_VERSION_MINOR",
            "LLVM_VERSION_PATCH",
            "LLVM_DEFINITIONS",
            "LLVM_INCLUDE_DIRS",
            "LLVM_LINK_LLVM_DYLIB",
            "LLVM_ENABLE_RTTI",
            "LLVM_TARGETS_TO_BUILD",
        ],
    )
    cv = config_values.read(Path(f"{cm}/clang/ClangConfigVersion.cmake"), ["PACKAGE_VERSION"])
    lv = config_values.read(
        Path(f"{lld_root}/lib/cmake/lld/LLDConfigVersion.cmake"), ["PACKAGE_VERSION"]
    )
    on = {"ON": True, "OFF": False}
    words = [
        ("version", i, v[k][0])
        for i, k in enumerate(["LLVM_VERSION_MAJOR", "LLVM_VERSION_MINOR", "LLVM_VERSION_PATCH"])
    ]
    words += [("cppflags", i, w) for i, w in enumerate(v["LLVM_DEFINITIONS"][0].split())]
    words += [("includedir", 0, v["LLVM_INCLUDE_DIRS"][0])]
    words += [("shared-mode", 0, "shared" if on[v["LLVM_LINK_LLVM_DYLIB"][0]] else "static")]
    words += [("has-rtti", 0, "YES" if on[v["LLVM_ENABLE_RTTI"][0]] else "NO")]
    words += [
        ("targets-built", i, t) for i, t in enumerate(v["LLVM_TARGETS_TO_BUILD"][0].split(";"))
    ]
    origins = {k: f"LLVMConfig.cmake:{ln}" for k, (_, ln) in v.items()}
    return words, lv["PACKAGE_VERSION"][0], cv["PACKAGE_VERSION"][0], origins


def hybrid(inp: dict, words: list, lld: dict, cver: str, imports: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    reg = inp["registration"]

    def write(rel: str, rows) -> None:
        (out / f"{rel}.facts").write_text("".join("\t".join(map(str, r)) + "\n" for r in rows))

    write("tool_word", words)
    write("lld_version", [(lld["version"],)])
    write("lld_root", [(lld["root"],)])
    write("clang_version", [(cver,)])
    i_req, i_opt = reg["args"].index("COMPONENTS"), reg["args"].index("OPTIONAL_COMPONENTS")
    comps = reg["args"][i_req + 1 :]
    write(
        "want_component",
        [
            (c, n, 1 if n < i_opt - i_req - 1 else 0)
            for n, c in enumerate(c for c in comps if c != "OPTIONAL_COMPONENTS")
        ],
    )
    tg, dp, pr, lo = [], [], [], []
    for f, r in imports.items():
        res = r["result"]
        tg += [x[1:] for x in res["res_pkg_target"]]
        dp += [x[1:] for x in res["res_pkg_dep"]]
        pr += [x[1:] for x in res["res_pkg_prop"]]
        lo += [x[1:] for x in res["res_pkg_location"]]
    write("imp_target", tg)
    write("imp_dep", dp)
    write("imp_prop", pr)
    write("imp_location", lo)
    t0 = time.perf_counter()
    p = subprocess.run(
        ["souffle", "-F", str(out), "-D", str(out), str(HERE / "hybrid" / "halide_llvm.dl")],
        capture_output=True,
        text=True,
        check=False,
    )
    dt = time.perf_counter() - t0
    if p.returncode:
        raise SystemExit(p.stderr)
    rows: dict[str, list] = {}
    rn = reg["rename"]
    for rel, cols in [
        ("res_pkg_target", (0,)),
        ("res_pkg_dep", (0, 1)),
        ("res_pkg_prop", (0,)),
        ("res_pkg_location", (0,)),
        ("res_probe_setting", ()),
        ("error", ()),
    ]:
        f = out / f"{rel}.csv"
        lines = f.read_text().splitlines() if f.exists() else []
        rows[rel] = [
            tuple(rn.get(x, x) if i in cols else x for i, x in enumerate(ln.split("\t")))
            for ln in lines
        ]
    return {"rows": rows, "souffle_seconds": round(dt, 3)}


# ---- compare ----------------------------------------------------------------------


def strip_req(rows: dict) -> dict:
    return {rel: [tuple(r[1:]) for r in rs] for rel, rs in rows.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs")
    ap.add_argument("--hybrid", action="store_true")
    ap.add_argument("--source", choices=["llvm-config", "cmake-config"], default="llvm-config")
    a = ap.parse_args()
    inp = json.loads(Path(a.inputs).read_text())
    reg = inp["registration"]
    out = HERE / "raw" / f"option2-{inp['name']}"
    out.mkdir(parents=True, exist_ok=True)
    lc = os.path.join(inp["llvm_root"], "bin", "llvm-config")
    lld_root = inp.get("lld_root", "/opt/homebrew/opt/lld@{major}")

    q = Queries()
    t0 = time.perf_counter()
    llvm = llvm_facts(q, lc)
    lld = lld_facts(q, lld_root.format(**llvm))
    cver = clang_version(q, llvm["clang"])
    rows = Rows()
    halide_rules(rows, llvm, lld, cver, reg, lc)
    total = time.perf_counter() - t0
    (out / "queries.json").write_text(json.dumps(q.log, indent=1))
    (out / "rows.json").write_text(
        json.dumps({"rows": rows.rows, "origins": rows.origin}, indent=1)
    )
    print(f"option 2: {len(q.log)} tool queries, {q.seconds:.3f}s in tools, {total:.3f}s total")

    # The generated export files, through the existing importer (realpaths, so
    # the import prefixes match what CMake's REALPATH-based configs computed).
    cm = llvm["cmakedir"]
    files = [
        f"{cm}/LLVMExports.cmake",
        f"{os.path.dirname(cm)}/clang/ClangTargets.cmake",
        f"{lld['root']}/lib/cmake/lld/LLDTargets.cmake",
    ]
    imports = import_exports(files)
    summary = {f: {k: v for k, v in r.items() if k != "result"} for f, r in imports.items()}
    (out / "imports.json").write_text(json.dumps(summary, indent=1))
    for f, r in summary.items():
        print(f"  import {os.path.basename(f)}: {r['status']} in {r['seconds']}s, {r['counts']}")

    o1 = HERE / "raw" / f"option1-{inp['name']}" / "rows.json"
    ref = strip_req(json.loads(o1.read_text())["rows"]) if o1.exists() else None
    mine = {k: v for k, v in rows.rows.items() if k != "error"}
    if ref is not None:
        _diff("option 2 vs option 1", ref, mine)
    if rows.rows.get("error"):
        print("  errors:", rows.rows["error"])
    if a.hybrid:
        t1 = time.perf_counter()
        if a.source == "llvm-config":
            words, hlld, hcver = tool_words(q, lc), lld, cver
        else:
            words, lv, hcver, _ = config_words(os.path.realpath(inp["llvm_root"]), lld["root"])
            hlld = {**lld, "version": lv}
        t_words = time.perf_counter() - t1
        h = hybrid(
            inp, words, hlld, hcver, imports, HERE / "raw" / f"hybrid-{a.source}-{inp['name']}"
        )
        print(f"hybrid ({a.source}): words {t_words:.3f}s, souffle {h['souffle_seconds']}s")
        if ref is not None:
            _diff("hybrid vs option 1", ref, {k: v for k, v in h["rows"].items() if k != "error"})
        if h["rows"].get("error"):
            print("  errors:", h["rows"]["error"])


def _diff(what: str, ref: dict, mine: dict) -> None:
    skip = ("res_pkg_import", "res_pkg_condition")
    a = {(k, *map(str, r)) for k, rs in ref.items() if k not in skip for r in rs}
    b = {(k, *map(str, r)) for k, rs in mine.items() if k not in skip for r in rs}
    print(f"  {what}: {len(a & b)} rows equal, {len(a - b)} only option 1, {len(b - a)} only here")
    for r in sorted(a - b):
        print("    - " + shlex.join(r))
    for r in sorted(b - a):
        print("    + " + shlex.join(r))


if __name__ == "__main__":
    main()
