#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare the buildlog release build with the CMake baseline.

Usage: compare.py BUILDLOG_BUILD_DIR [CONFIG_NAME]

Reads baseline/release/commands.txt (CMake, normalized by
baseline/run-cmake.sh) and `ninja -t commands` of the buildlog build, then
prints per-object flag/define/include differences, per-binary link inputs,
library members and defined global symbols. Standard library only.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_ABS = str(Path(os.environ.get("BUILDLOG_FLATBUFFERS_SRC", str(HERE / "upstream"))).expanduser())
BASE = HERE / "baseline" / "release"
LIB_SRCS = {
    "src/file_manager.cpp",
    "src/file_name_manager.cpp",
    "src/idl_gen_text.cpp",
    "src/idl_parser.cpp",
    "src/reflection.cpp",
    "src/util.cpp",
}


def classify(args: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"D": set(), "I": set(), "F": set()}
    skip = False
    for i, a in enumerate(args[1:], 1):
        if skip:
            skip = False
            continue
        if a in ("-o", "-MF", "-MT", "-c", "-isysroot", "-arch"):
            skip = True
            continue
        if a == "-isystem":
            out["I"].add("isystem:" + norm_path(args[i + 1]))
            skip = True
            continue
        if a in ("-MD",) or a.startswith("--target="):
            continue
        if a.startswith("-D"):
            out["D"].add(a.replace(SRC_ABS, "$SRC"))
        elif a.startswith("-I"):
            out["I"].add(norm_path(a[2:]))
        elif a.startswith("-"):
            out["F"].add(a)
    return out


def norm_path(p: str) -> str:
    p = re.sub(r"^(\.\./)+flatbuffers/?", "$SRC/", p)
    return p.rstrip("/") or p


def arg_after(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def cmake_compiles():
    objs, links, archive = {}, {}, []
    for line in (BASE / "commands.txt").read_text().splitlines():
        if " -c $SRC/" in line and line.startswith("/usr/bin/c++"):
            args = shlex.split(line)
            m = re.match(r"CMakeFiles/([^/]+)\.dir/(.+)\.o$", arg_after(args, "-o"))
            objs[(m[1], m[2])] = classify(args)
        elif "/ar qc " in line:
            archive = [
                re.sub(r"CMakeFiles/flatbuffers\.dir/(.+)\.o$", r"\1", a)
                for a in shlex.split(line)
                if a.endswith(".o")
            ]
        elif line.startswith(": && /usr/bin/c++"):
            args = shlex.split(line)
            name = arg_after(args, "-o")
            ins = [
                re.sub(r"CMakeFiles/[^/]+\.dir/(.+)\.o$", r"\1", a)
                for a in args
                if a.endswith((".o", ".a"))
            ]
            links[name] = sorted(ins)
    return objs, links, sorted(archive)


def buildlog_compiles(build: Path, cfg: str):
    text = subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "-t", "commands"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objs, links, archive, flatc_runs = {}, {}, [], []
    for line in text.splitlines():
        args = shlex.split(line)
        tool = Path(args[0]).name
        if tool == "clang++" and "-c" in args:
            m = re.match(rf"obj/{re.escape(cfg)}/([^/]+)/(.+)\.o$", arg_after(args, "-o"))
            if m:
                objs[(m[1], m[2])] = classify(args)
        elif tool == "clang++":
            name = arg_after(args, "-o")
            if name and name.startswith(f"bin/{cfg}/"):
                ins = [
                    re.sub(rf"obj/{re.escape(cfg)}/[^/]+/(.+)\.o$", r"\1", a)
                    for a in args
                    if a.endswith((".o", ".a"))
                ]
                links[Path(name).name] = sorted(ins)
        elif tool == "ar" and f"lib/{cfg}/libflatbuffers.a" in args:
            archive = sorted(
                re.sub(r"obj/[^/]+/flatbuffers/(.+)\.o$", r"\1", a)
                for a in args
                if a.endswith(".o")
            )
        elif tool == "flatc":
            flatc_runs.append(args[0])
    return objs, links, archive, flatc_runs


def main() -> int:
    build = Path(sys.argv[1]).expanduser()
    cfg = sys.argv[2] if len(sys.argv) > 2 else "arm64-macos.release.none"
    c_objs, c_links, c_ar = cmake_compiles()
    b_objs, b_links, b_ar, runs = buildlog_compiles(build, cfg)

    print(f"# objects: cmake {len(c_objs)}, buildlog {len(b_objs)}")
    only_c = sorted(set(c_objs) - set(b_objs))
    only_b = sorted(set(b_objs) - set(c_objs))
    print(f"only in cmake: {only_c or 'none'}")
    print(f"only in buildlog: {only_b or 'none'}")

    diffs: dict[tuple, list[str]] = defaultdict(list)
    for key in sorted(set(c_objs) & set(b_objs)):
        c, b = c_objs[key], b_objs[key]
        for kind in "DIF":
            minus = tuple(sorted(c[kind] - b[kind]))
            plus = tuple(sorted(b[kind] - c[kind]))
            if minus or plus:
                diffs[(kind, minus, plus)].append(f"{key[0]}:{key[1]}")
    print("\n# per-object differences (cmake-only → buildlog-only), grouped")
    for (kind, minus, plus), where in sorted(diffs.items()):
        targets = sorted({w.split(":")[0] for w in where})
        print(f"[{kind}] -{list(minus)} +{list(plus)}: {len(where)} objects in {targets}")

    print("\n# link inputs")
    for name in sorted(set(c_links) | set(b_links)):
        c, b = c_links.get(name), b_links.get(name)
        status = "same" if c == b else f"cmake={c} buildlog={b}"
        print(f"{name}: {status}")

    print("\n# libflatbuffers.a members")
    print("same (order-insensitive)" if c_ar == b_ar else f"cmake={c_ar} buildlog={b_ar}")

    lib = build / "souffle" / "lib" / cfg / "libflatbuffers.a"
    if lib.exists():
        nm = subprocess.run(["nm", "-gU", str(lib)], check=True, capture_output=True, text=True)
        b_syms = {ln.split()[-1] for ln in nm.stdout.splitlines() if ln and not ln.endswith(":")}
        text = (BASE / "exported-symbols.txt").read_text()
        c_syms = set(text.split("(defined globals, sorted)\n", 1)[1].split())
        print("\n# defined global symbols in libflatbuffers.a")
        print(
            f"cmake {len(c_syms)}, buildlog {len(b_syms)}, "
            f"only cmake {len(c_syms - b_syms)}, only buildlog {len(b_syms - c_syms)}"
        )
        for s in sorted(c_syms ^ b_syms)[:20]:
            print(f"  {'-' if s in c_syms else '+'} {s}")

    print(f"\n# flatc runs: {len(runs)} by {sorted(set(runs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
