# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Extract and compare the commands of two Ninja build trees of one app.

The Halide apps' CMake builds are several Ninja trees (a super-build's
sub-builds; Gradle's NDK build and its nested host build), the buildlog build
is one. What the case study compares is what ran, so both sides are reduced
to the same records, from `ninja -t commands`:

  compile   (target, source, flags)          flags as an unordered set
  generate  (-n name, target string, params) one per generator invocation
  runtime   (target string)                  gengen -r
  link      (output stem, flags, inputs)     the program or shared library
  archive   (output stem, members)           static libraries

  commands.py extract [--root PH=PATH ...] -o OUT.json NINJA_DIR...
  commands.py compare [--root ...] [--variants FILE] BASELINE.json NINJA_DIR... [--details]

A baseline that unrolled one target into variants (HelloWasm's index_simd,
index_threads, ...) is compared with buildlog's configurations by --variants:
a JSON file {"configs": {<configuration directory>: <variant suffix>},
"rename": [[<regex>, <template with {v}>], ...]}. A target, library or
program built in a listed configuration is renamed by the first matching
rule, as the baseline names it.

Paths are replaced by placeholders (--root, longest first); a path under
$BUILD keeps only its last component. Machine-independent, standard library
only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict

DROP_FLAGS = {"-c", "-MD", "-MMD"}
DROP_WITH_ARG = {"-MT", "-MF", "-o"}
SOURCE = re.compile(r"\.(cpp|cc|c|cxx)$")


PREFIXES = ("-I", "-L", "-Wl,-force_load,", "-Wl,-rpath,", "--sysroot=")


def absolutize(ninja_dir: str, tok: str) -> str:
    """Tokens that are paths relative to the build directory (a Ninja
    command runs there) are made absolute, so both sides compare alike."""
    for pre in PREFIXES:
        if tok.startswith(pre):
            return pre + absolutize(ninja_dir, tok[len(pre) :])
    if (
        "/" in tok
        and not tok.startswith(("/", "-", "$"))
        and "=" not in tok
        and not tok.startswith("target")
    ):
        return os.path.normpath(os.path.join(ninja_dir, tok))
    return tok


def commands(ninja_dir: str) -> list[list[str]]:
    out = subprocess.run(
        ["ninja", "-C", ninja_dir, "-t", "commands"], text=True, capture_output=True, check=True
    ).stdout
    res = []
    ndir = os.path.abspath(ninja_dir)
    for ln in out.splitlines():
        if ln.startswith("ninja:"):
            continue
        res.append([absolutize(ndir, t) for t in split(ln)])
    return res


def make_sub(roots: list[tuple[str, str]], build_roots: list[str]):
    roots = sorted(roots, key=lambda r: -len(r[1]))
    builds = sorted(build_roots, key=len, reverse=True)

    def sub(s: str) -> str:
        # What is under a build tree is a generated file: keep its name.
        for b in builds:
            s = re.sub(
                re.escape(b) + r"/?(?:[^\s:]*/)?([^\s/:]*)", lambda m: "$BUILD/" + m.group(1), s
            )
        for ph, path in roots:
            s = s.replace(path, ph)
        return s

    return sub


def split(cmd: str) -> list[str]:
    """The command's main step: not the cd, cmake -E bookkeeping or the leading ':'."""
    segs = [s.strip() for s in re.split(r"\s&&\s", cmd)]
    for seg in segs:
        seg = seg.removeprefix(": ").strip()
        if seg in (":", "") or seg.startswith("cd ") or "cmake -E" in seg:
            continue
        return shlex.split(seg)
    return []


def stem(p: str) -> str:
    b = os.path.basename(p)
    return (
        re.sub(r"(\.js|\.a|\.so|\.o)$", "", re.sub(r"^lib(?=.)", "", b))
        if b.endswith((".js", ".a", ".so"))
        else b
    )


def units(args: list[str]) -> list[str]:
    out, i = [], 0
    with_arg = {
        "-I",
        "-isystem",
        "-iquote",
        "-D",
        "-include",
        "-Xlinker",
        "-target",
        "--target",
        "-arch",
    }
    while i < len(args):
        a = args[i]
        if a in DROP_FLAGS or SOURCE.search(a) and not a.startswith("-"):
            i += 1
        elif a in DROP_WITH_ARG:
            i += 2
        elif a in with_arg and i + 1 < len(args):
            out.append(f"{a} {args[i + 1]}")
            i += 2
        else:
            out.append(a)
            i += 1
    return out


VARIANTS: dict = {"configs": {}, "rename": []}


def variant_name(path: str, name: str) -> str:
    """name as the baseline spells it, when path is in a listed configuration."""
    for cfg, suffix in VARIANTS["configs"].items():
        if f"/{cfg}/" in path:
            for pattern, template in VARIANTS["rename"]:
                if re.fullmatch(pattern, name):
                    return re.sub(pattern, template.replace("{v}", suffix), name)
    return name


def target_of(obj: str) -> str:
    m = re.search(r"CMakeFiles/([^/]+)\.dir/", obj) or re.search(r"obj/[^/]+/([^/]+)/", obj)
    return variant_name(obj, m.group(1)) if m else "?"


HOST_DRIVERS = {"c++", "clang++", "g++", "cc", "clang", "gcc"}
# Spelled by CMake and by buildlog differently, or by one only, with no
# effect on what is built: the macOS arch/SDK selection and ld defaults.
NOISE = re.compile(
    r"-arch arm64|-isysroot|/.*/MacOSX[0-9.]*\.sdk|--target=arm64-apple-macos[0-9.]+|"
    r"-Wl,-search_paths_first|-Wl,-headerpad_max_install_names|\$BUILD/.*"
)


def records(cmds: list[list[str]], sub) -> dict[str, list]:
    rec: dict[str, list] = defaultdict(list)
    for argv in cmds:
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog in HOST_DRIVERS:
            prog = "c++"
        if "-g" in argv and any(a.startswith("target=") for a in argv) and "-n" in argv:
            tgt = next(a for a in argv if a.startswith("target="))
            params = sorted(a for a in argv[1:] if "=" in a and not a.startswith(("target=", "-")))
            rec["generate"].append(
                {
                    "name": argv[argv.index("-n") + 1],
                    "generator": argv[argv.index("-g") + 1],
                    "function": argv[argv.index("-f") + 1],
                    "emit": argv[argv.index("-e") + 1],
                    "target": tgt[7:],
                    "params": params,
                }
            )
        elif prog == "gengen" and "-r" in argv:
            tgt = next(a for a in argv if a.startswith("target="))
            rec["runtime"].append({"name": argv[argv.index("-r") + 1], "target": tgt[7:]})
        elif "-c" in argv and any(SOURCE.search(a) for a in argv if not a.startswith("-")):
            src = next(a for a in reversed(argv) if SOURCE.search(a) and not a.startswith("-"))
            obj = argv[argv.index("-o") + 1] if "-o" in argv else ""
            rec["compile"].append(
                {
                    "target": target_of(obj),
                    "source": sub(src),
                    "compiler": prog,
                    "flags": clean(sub, units(argv[1:])),
                }
            )
        elif prog in ("ar", "llvm-ar", "emar", "emar.py") or (
            len(argv) > 1 and argv[1] in ("qc", "rcs", "cr")
        ):
            i = 1
            while (
                i < len(argv)
                and argv[i].startswith(("-", "q", "r", "c"))
                and not argv[i].endswith(".a")
            ):
                i += 1
            rec["archive"].append(
                {
                    "output": variant_name(argv[i], stem(argv[i])),
                    "members": sorted(os.path.basename(m) for m in argv[i + 1 :]),
                }
            )
        elif "-o" in argv and not any(a == "-c" for a in argv):
            out = argv[argv.index("-o") + 1]
            ins = [a for a in argv[1:] if a.endswith((".o", ".a", ".dylib", ".so")) and a != out]
            fl = [a for a in argv[1:] if a not in ins and a != out and a != "-o"]
            rec["link"].append(
                {
                    "output": variant_name(out, stem(out)),
                    "compiler": prog,
                    "flags": clean(sub, units(fl)),
                    "inputs": sorted(os.path.basename(i) for i in ins),
                }
            )
    return rec


def clean(sub, us: list[str]) -> list[str]:
    return sorted(u for u in (sub(x) for x in us) if not NOISE.fullmatch(u))


def key(kind: str, r: dict) -> str:
    if kind == "compile":
        return f"{r['target']} <- {r['source']}"
    if kind == "generate":
        return f"{r['name']} {r['target']} {' '.join(r['params'])}"
    if kind == "runtime":
        return f"{r['name']} {r['target']}"
    return r["output"]


def build(ninja_dirs: list[str], roots: list[tuple[str, str]]) -> dict:
    sub = make_sub(roots, [os.path.abspath(d) for d in ninja_dirs])
    allcmds = [c for d in ninja_dirs for c in commands(d)]
    return {k: sorted(v, key=lambda r: key(k, r)) for k, v in records(allcmds, sub).items()}


def parse_roots(items):
    return [(i.split("=", 1)[0], i.split("=", 1)[1]) for i in items or []]


def compare(a: dict, b: dict, details: bool) -> int:
    bad = 0
    for kind in ("compile", "generate", "runtime", "archive", "link"):
        ka = defaultdict(list)
        kb = defaultdict(list)
        for r in a.get(kind, []):
            ka[key(kind, r)].append(r)
        for r in b.get(kind, []):
            kb[key(kind, r)].append(r)
        both = set(ka) & set(kb)
        diff = [k for k in sorted(both) if ka[k] != kb[k]]
        print(
            f"{kind:9} baseline {sum(map(len, ka.values())):3}  buildlog {sum(map(len, kb.values())):3}  "
            f"same key {len(both):3}  identical {len(both) - len(diff):3}  differing {len(diff):3}  "
            f"only-baseline {len(set(ka) - set(kb))}  only-buildlog {len(set(kb) - set(ka))}"
        )
        for k in sorted(set(ka) - set(kb)):
            print(f"  only in baseline: {k}")
        for k in sorted(set(kb) - set(ka)):
            print(f"  only in buildlog: {k}")
        bad += len(set(ka) ^ set(kb))
        if details:
            for k in diff:
                if len(ka[k]) != len(kb[k]):
                    print(f"  {kind} {k}: {len(ka[k])} records -> {len(kb[k])}")
                shown = False
                for fld in (
                    "flags",
                    "inputs",
                    "members",
                    "compiler",
                    "emit",
                    "function",
                    "generator",
                    "target",
                    "params",
                    "name",
                ):
                    x = ka[k][0].get(fld)
                    y = kb[k][0].get(fld)
                    if x != y:
                        shown = True
                        if isinstance(x, list):
                            print(
                                f"  {kind} {k}: {fld}: -{sorted(set(x) - set(y or []))} +{sorted(set(y or []) - set(x))}"
                            )
                        else:
                            print(f"  {kind} {k}: {fld}: {x!r} -> {y!r}")
                if not shown:
                    print(f"  {kind} {k}: baseline {ka[k]} -> buildlog {kb[k]}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("extract")
    e.add_argument("--root", action="append")
    e.add_argument("-o", required=True)
    e.add_argument("dirs", nargs="+")
    c = sp.add_parser("compare")
    c.add_argument("--root", action="append")
    c.add_argument("--details", action="store_true")
    c.add_argument("--variants")
    c.add_argument("baseline")
    c.add_argument("dirs", nargs="+")
    a = ap.parse_args()
    roots = parse_roots(a.root)
    if a.cmd == "extract":
        with open(a.o, "w") as f:
            json.dump(build(a.dirs, roots), f, indent=1, sort_keys=True)
            f.write("\n")
        return 0
    if a.variants:
        with open(a.variants) as f:
            VARIANTS.update(json.load(f))
    with open(a.baseline) as f:
        base = json.load(f)
    compare(base, build(a.dirs, roots), a.details)
    return 0


if __name__ == "__main__":
    sys.exit(main())
