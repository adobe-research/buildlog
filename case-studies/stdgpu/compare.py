#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare a buildlog build of stdgpu with the CMake baseline (the stdgpu case study).

    compare.py BASELINE_COMMANDS BUILDLOG_BUILD_DIR [--src SRC]

BASELINE_COMMANDS is baseline/linux/commands.txt (`ninja -t commands` of
CMake's build, paths normalized to $SRC and $BUILD); BUILDLOG_BUILD_DIR is a
configured and built `buildlog configure -C` directory (its souffle/ is where
ninja runs). For every object both builds compile (keyed by output name, nvcc
and host compiler alike) it reports the defines, include dirs and other flags
one has and the other lacks (as sets: order is not compared), grouped so one
line covers every object with the same difference; then each linked
artifact's inputs. Spelling both sides share by construction (compiler path,
depfile flags, -c/-o) is dropped.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from collections import defaultdict
from pathlib import Path

DROP = {"-MD", "-c", "-x", "cu"}
WITH_ARG = {"-MF", "-MT", "-o", "-ccbin", "-x"}


def norm(p: str, base: str, src: str) -> str:
    q = os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))
    q = q.replace(src, "$SRC")
    # the directory the configured config.h is in: CMake's build tree, buildlog's gen
    if q.endswith("/src/stdgpu/include") or "/gen/" in q:
        return "$GEN"
    return q


def classify(cmd: str, base: str, src: str) -> dict:
    a = shlex.split(cmd)
    out = {
        "define": set(),
        "include": set(),
        "flag": set(),
        "arch": set(),
        "src": "",
        "obj": "",
        "tool": a[0],
    }
    i = 1
    while i < len(a):
        x = a[i]
        if x in {"-MF", "-MT", "-ccbin", "-x"}:
            i += 2
            continue
        if x == "-o":
            out["obj"] = a[i + 1]
            i += 2
            continue
        if x in DROP:
            i += 1
        elif x in {"-I", "-isystem"}:
            out["include"].add(norm(a[i + 1], base, src))
            i += 2
        elif x.startswith("-I"):
            out["include"].add(norm(x[2:], base, src))
            i += 1
        elif x.startswith("-D"):
            out["define"].add(x)
            i += 1
        elif x.startswith("--generate-code"):
            out["arch"].add(x.split("=", 1)[1].strip("'\""))
            i += 1
        elif x.startswith("-Xcompiler="):
            out["flag"].update("-Xcompiler=" + f for f in x.split("=", 1)[1].split(","))
            i += 1
        elif x == "-forward-unknown-to-host-compiler":
            i += 1
        elif x.startswith("-") or x.endswith(".o"):
            out["flag"].add(x)
            i += 1
        else:
            out["src"] = norm(x, base, src)
            i += 1
    out["include"].discard("")
    return out


def objects(cmds: list[str], base: str, src: str) -> dict:
    res = {}
    for line in cmds:
        if " -c " not in f" {line} ":
            continue
        c = classify(line, base, src)
        key = c["src"].replace("$SRC/", "")
        res[key] = c
    return res


def links(cmds: list[str], base: str, src: str) -> dict:
    res = {}
    for line in cmds:
        if " -c " in f" {line} " or "configure_file" in line:
            continue
        if line.startswith(": &&"):
            line = next(s for s in line.split(" && ") if "-o bin/" in s or "ar qc" in s)
        a = shlex.split(line)
        if a[0].endswith(("ar", "gcc-ar")) or "ar qc" in line or " rcs " in line:
            toks = [t for t in a if t.endswith(".o")]
            name = (
                next(t for t in a if t.endswith(".a")) if any(t.endswith(".a") for t in a) else ""
            )
            res[os.path.basename(name)] = ("ar", sorted(os.path.basename(t) for t in toks))
            continue
        if "-o" not in a:
            continue
        o = os.path.basename(a[a.index("-o") + 1])
        ins = []
        for t in a[1:]:
            if t.endswith((".o", ".a")):
                ins.append(os.path.basename(t))
            elif t.startswith("-l"):
                ins.append(t)
        res[o] = ("link", ins)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("build")
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent / "upstream"))
    ns = ap.parse_args()
    src = ns.src
    bdir = str(Path(ns.build) / "souffle")
    bl = subprocess.run(
        ["ninja", "-C", bdir, "-t", "commands"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    cm = (
        Path(ns.baseline)
        .read_text()
        .replace("$SRC", src)
        .replace("$BUILD", "/cmake-build")
        .splitlines()
    )
    cmo, blo = objects(cm, "/cmake-build", src), objects(bl, bdir, src)
    report, groups = [], defaultdict(list)
    for k in sorted(set(cmo) | set(blo)):
        if k not in cmo or k not in blo:
            report.append(f"only in {'CMake' if k in cmo else 'buildlog'}: {k}")
            continue
        c, b = cmo[k], blo[k]
        diff = []
        for what in ("define", "include", "flag", "arch"):
            for x in sorted(c[what] - b[what]):
                diff.append(f"CMake only {what}: {x}")
            for x in sorted(b[what] - c[what]):
                diff.append(f"buildlog only {what}: {x}")
        groups[tuple(diff)].append(k)
    print(f"objects: CMake {len(cmo)}, buildlog {len(blo)}")
    for d, ks in groups.items():
        print(f"\n{len(ks)} object(s) {', '.join(ks) if len(ks) < 4 else ks[0] + ', ...'}:")
        print("  identical" if not d else "\n".join("  " + x for x in d))
    for r in report:
        print(r)
    cl, bll = links(cm, "/cmake-build", src), links(bl, bdir, src)
    print("\nlinks")
    for k in sorted(set(cl) | set(bll)):
        if k not in cl or k not in bll:
            print(f"only in {'CMake' if k in cl else 'buildlog'}: {k}")
            continue
        ca, ba = cl[k][1], bll[k][1]
        if ca == ba:
            print(f"  {k}: identical inputs")
            continue
        only_c, only_b = sorted(set(ca) - set(ba)), sorted(set(ba) - set(ca))
        print(
            f"  {k}: same inputs except CMake only {only_c}, buildlog only {only_b}; order {'same' if [x for x in ca if x in ba] == [x for x in ba if x in ca] else 'differs'}"
        )


if __name__ == "__main__":
    main()
