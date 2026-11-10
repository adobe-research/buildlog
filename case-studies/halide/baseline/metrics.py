# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Size and implicit-choice metrics for the Halide CMake baseline (prints Markdown).

    uv run python metrics.py cmake-size --fileapi raw/release/fileapi-reply \
        --baseline cmake-release.jsonl --install cmake-release-install.jsonl --src SRC
    uv run python metrics.py implicit --cache raw/release/CMakeCache.txt \
        --configure-log raw/release/configure.log --src SRC --build BUILD --llvm LLVM

`cmake-size` counts non-blank, non-comment lines of the project's CMake files. A file
*contributes to the in-scope targets* if it appears in the File API backtrace of an
in-scope target (i.e. it ran a command that shaped the target) or of an install rule of
an in-scope directory, or is the CMakeLists.txt of such a directory or of any ancestor
(the add_subdirectory chain). Files are split into Halide's reusable, installed helper
modules (their basename is installed under lib/cmake/), project-internal modules
(cmake/*.cmake, not installed) and project lists (CMakeLists.txt). The total of all
project CMake files read at configure time (File API cmakeFiles) is given as an upper
bound.

`implicit` lists the choices CMake made without being told: find_package/find_library
results, compiler detection and the LLVM selection, from CMakeCache.txt and the
configure log, with machine paths replaced by placeholders.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import normalize

BRACKET = re.compile(r"#\[(=*)\[.*?\]\1\]", re.DOTALL)


def sloc(path: str) -> int:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = BRACKET.sub("", f.read())
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def _load(reply: str, pattern: str) -> dict:
    (p,) = glob.glob(os.path.join(reply, pattern))
    with open(p) as f:
        return json.load(f)


def cmake_size(reply: str, baseline: str, install: str | None, src: str) -> str:
    with open(baseline) as f:
        recs = [json.loads(line) for line in f]
    in_targets = {r["target"] for r in recs if not r.get("excluded") and r.get("target")}
    in_dirs = {r["dir"] for r in recs if not r.get("excluded") and r.get("dir")}
    installed = set()
    if install:
        with open(install) as f:
            for line in f:
                p = json.loads(line)["path"]
                if "/lib/cmake/" in p:
                    installed.add(os.path.basename(p))
    cm = _load(reply, "codemodel-v2-*.json")
    conf = cm["configurations"][0]
    files: set[str] = set()

    def add_bt(obj: dict) -> None:
        for fp in obj.get("backtraceGraph", {}).get("files", []):
            if not os.path.isabs(fp):
                files.add(os.path.normpath(fp))

    for t in conf["targets"]:
        if t["name"] in in_targets:
            with open(os.path.join(reply, t["jsonFile"])) as f:
                add_bt(json.load(f))
    for d in conf["directories"]:
        if d["source"] in in_dirs:
            if d.get("jsonFile"):
                with open(os.path.join(reply, d["jsonFile"])) as f:
                    add_bt(json.load(f))
            parts = [] if d["source"] == "." else d["source"].split("/")
            for i in range(len(parts) + 1):
                p = os.path.join(*parts[:i], "CMakeLists.txt") if i else "CMakeLists.txt"
                if os.path.exists(os.path.join(src, p)):
                    files.add(p)
    cf = _load(reply, "cmakeFiles-v1-*.json")
    all_project = sorted(
        i["path"]
        for i in cf["inputs"]
        if not i.get("isExternal") and not i.get("isGenerated") and not os.path.isabs(i["path"])
    )
    # Modules pulled in with include()/find_package() shape global state (flags, helper
    # functions) without always showing up in a target backtrace; count every project
    # .cmake file read at configure time outside out-of-scope directories.
    out_of_scope = ("test/", "python_bindings/", "apps/", "tutorial/", "util/")
    files |= {p for p in all_project if p.endswith(".cmake") and not p.startswith(out_of_scope)}
    groups: dict[str, list[tuple[str, int]]] = {
        "helper": [],
        "module": [],
        "vendored": [],
        "list": [],
    }
    for p in sorted(files):
        full = os.path.join(src, p)
        if not os.path.exists(full):
            continue
        if os.path.basename(p) == "CMakeLists.txt":
            g = "list"
        elif p.startswith("dependencies/"):
            g = "vendored"
        elif os.path.basename(p) in installed:
            g = "helper"
        else:
            g = "module"
        groups[g].append((p, sloc(full)))
    titles = {
        "helper": "Reusable helper modules (installed with Halide, used by downstream projects)",
        "module": "Project-internal CMake modules (not installed)",
        "vendored": "Vendored package configs (dependencies/, not Halide-authored)",
        "list": "Project CMakeLists.txt files",
    }
    out = []
    grand = 0
    for g in ("helper", "module", "vendored", "list"):
        tot = sum(n for _, n in groups[g])
        if g != "vendored":
            grand += tot
        out.append(f"**{titles[g]}**: {len(groups[g])} files, {tot} lines\n")
        out.append("| file | lines |\n|---|---:|")
        out += [f"| `{p}` | {n} |" for p, n in groups[g]]
        out.append("")
    ap_lines = sum(
        sloc(os.path.join(src, p)) for p in all_project if os.path.exists(os.path.join(src, p))
    )
    out.append(
        f"**Total Halide-authored CMake contributing to in-scope targets: {grand} lines in "
        f"{sum(len(v) for k, v in groups.items() if k != 'vendored')} files** (vendored "
        f"configs excluded). Upper bound, every project CMake "
        f"file read at configure time (File API cmakeFiles, excluding generated): "
        f"{ap_lines} lines in {len(all_project)} files."
    )
    return "\n".join(out)


CACHE_LINE = re.compile(r"^([A-Za-z0-9_.+-]+):([A-Z]+)=(.*)$")
INTERESTING = re.compile(
    r"(_DIR|_LIBRARY(_RELEASE|_DEBUG)?|_INCLUDE_DIRS?|_EXECUTABLE|_COMPILER|_ROOT|_LINKER|"
    r"_AR|_RANLIB|OSX_SYSROOT|OSX_DEPLOYMENT_TARGET|_VERSION)$"
)
SKIP = re.compile(
    r"^(CMAKE_(CACHEFILE|HOME|PROJECT|INSTALL|FIND|EXTRA)|FETCHCONTENT|Halide_(SOURCE|BINARY)_DIR|_)"
)


def implicit(cache: str, log: str, roots: normalize.Roots) -> str:
    out = ["| cache entry | type | value |", "|---|---|---|"]
    with open(cache, encoding="utf-8") as f:
        for line in f:
            m = CACHE_LINE.match(line.strip())
            if not m or m.group(2) in ("INTERNAL", "STATIC"):
                continue
            k, ty, v = m.groups()
            if not INTERESTING.search(k) or SKIP.match(k):
                continue
            v = normalize.canon_str(roots, v)
            out.append(f"| `{k}` | {ty} | `{v}` |")
    out += ["", "Configure log (`-- Found …`, compiler identification, LLVM):", "", "```"]
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if re.match(
                r"-- (Found|The (C|CXX) compiler|Looking for|Performing Test|Using|Halide)", line
            ) or ("LLVM" in line and line.startswith("--")):
                out.append(normalize.canon_str(roots, line.rstrip()))
    out.append("```")
    return "\n".join(out)


def _tool_origin(tool: str | None, built: bool) -> str:
    if built:
        return "built in this graph"
    if not tool:
        return "?"
    for ph, what in (
        ("$LLVM", "imported (LLVM)"),
        ("$HOMEBREW", "imported (Homebrew)"),
        ("$BUILD", "build tree"),
    ):
        if tool.startswith(ph):
            return what
    return "imported (system)"


def scope_tables(baseline: str | list[dict]) -> str:
    if isinstance(baseline, str):
        with open(baseline) as f:
            recs = [json.loads(line) for line in f]
    else:
        recs = baseline
    ins = [r for r in recs if not r.get("excluded")]
    kinds = sorted({r["kind"] for r in ins})
    phases = sorted({r["phase"] for r in ins})
    out = ["| phase | " + " | ".join(kinds) + " | total |", "|---" * (len(kinds) + 2) + "|"]
    for ph in phases:
        row = [sum(1 for r in ins if r["phase"] == ph and r["kind"] == k) for k in kinds]
        out.append(f"| {ph} | " + " | ".join(str(n or "") for n in row) + f" | {sum(row)} |")
    tot = [sum(1 for r in ins if r["kind"] == k) for k in kinds]
    out.append("| **all** | " + " | ".join(map(str, tot)) + f" | {sum(tot)} |")
    excl: dict[str, int] = {}
    for r in recs:
        if r.get("excluded"):
            excl[r["excluded"]] = excl.get(r["excluded"], 0) + 1
    out += ["", "| excluded because | edges |", "|---|---:|"]
    out += [f"| {k} | {v} |" for k, v in sorted(excl.items(), key=lambda kv: -kv[1])]
    out += ["", "In-scope CMake targets per phase (non-phony edges):", ""]
    for ph in phases:
        tg: dict[str, int] = {}
        for r in ins:
            if r["phase"] == ph and not r.get("phony") and r.get("target"):
                tg[r["target"]] = tg.get(r["target"], 0) + 1
        names = ", ".join(f"`{t}` ({n})" for t, n in sorted(tg.items()))
        out.append(f"- **{ph}** ({len(tg)} targets): {names}")
    out += [
        "",
        "Custom commands (in scope) by main program:",
        "",
        "| phase | kind | tool | tool origin | edges | targets | also runs built |",
        "|---|---|---|---|---:|---:|---|",
    ]
    cc: dict[tuple, list[dict]] = {}
    for r in ins:
        if r.get("origin") == "custom_command":
            key = (
                r["phase"],
                r["kind"],
                r.get("tool_name"),
                _tool_origin(r.get("tool"), r.get("tool_built", False)),
            )
            cc.setdefault(key, []).append(r)
    for key in sorted(cc, key=lambda k: (k[0], k[2] or "")):
        rs = cc[key]
        extra = set()
        for r in rs:
            for b in r.get("runs_built", []):
                if b != r.get("tool"):
                    extra.add(os.path.basename(b))
        ex = ", ".join(sorted(extra)[:4]) + (" …" if len(extra) > 4 else "")
        tg = len({r.get("target") for r in rs})
        out.append(f"| {key[0]} | {key[1]} | `{key[2]}` | {key[3]} | {len(rs)} | {tg} | {ex} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("cmake-size")
    s.add_argument("--fileapi", required=True)
    s.add_argument("--baseline", required=True)
    s.add_argument("--install")
    s.add_argument("--src", required=True)
    i = sub.add_parser("implicit")
    i.add_argument("--cache", required=True)
    i.add_argument("--configure-log", required=True)
    i.add_argument("--src", required=True)
    i.add_argument("--build", required=True)
    i.add_argument("--llvm")
    t = sub.add_parser("scope-tables")
    t.add_argument("--baseline", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "scope-tables":
        print(scope_tables(a.baseline))
    elif a.cmd == "cmake-size":
        print(cmake_size(a.fileapi, a.baseline, a.install, a.src))
    else:
        roots = normalize.default_roots(a.src, a.build, a.llvm)
        print(implicit(a.cache, a.configure_log, roots))
    return 0


if __name__ == "__main__":
    sys.exit(main())
