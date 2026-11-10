#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare a buildlog build of zlib-ng with the CMake baseline, per TU.

    compare.py BASELINE_DIR BUILDLOG_BUILD_DIR PLATFORM

BASELINE_DIR is baseline/native or baseline/x86_64 (its commands.txt);
BUILDLOG_BUILD_DIR is where `buildlog configure -C` wrote build.ninja;
PLATFORM is the buildlog platform whose objects to compare (arm64-macos,
x86_64-macos; universal2's slices are those configurations' objects).

A TU is (target, source). Its flags are compared as sets, after dropping
what differs by construction: output and dependency-file arguments, the
architecture spelling (-arch vs --target, -isysroot), include directories
(each side's build tree; the source tree is compared as $SRC), and
buildlog-cpp's release -O3 (the project's -O2 comes later and wins, as in
CMake). Prints the TUs only one side compiles and the flag differences;
exits 1 on any difference.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

DROP_WITH_ARG = {"-o", "-MT", "-MF", "-arch", "-isysroot", "-c"}
DROP = {"-MD", "-O3"}


def flags(argv: list[str]) -> frozenset[str]:
    out: set[str] = set()
    skip = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a in DROP_WITH_ARG:
            skip = True
            continue
        if a in DROP or a.startswith(("--target=", "-I")):
            continue
        out.add(a)
    return frozenset(out)


def baseline(path: Path) -> dict[tuple[str, str], frozenset[str]]:
    tus = {}
    for line in path.read_text().splitlines():
        argv = shlex.split(line)
        if " -c " not in line or "gtest" in line:
            continue
        o = argv[argv.index("-o") + 1]
        m = re.match(r"(test/)?CMakeFiles/([^/]+)\.dir/(__/)?(.*)\.o$", o)
        assert m, o
        src = m.group(4) if m.group(3) or not m.group(1) else f"test/{m.group(4)}"
        tus[(m.group(2), src)] = flags(argv)
    return tus


# The library's two build instances (static: zlib-ng, shared: zlib-ng@shared)
# are CMake's zlib-ng-static and zlib-ng.
CMAKE_TARGET = {"zlib-ng": "zlib-ng-static", "zlib-ng@shared": "zlib-ng"}


def buildlog(build: Path, platform: str) -> dict[tuple[str, str], frozenset[str]]:
    cmds = subprocess.run(
        ["ninja", "-C", str(build), "-t", "commands"], capture_output=True, text=True, check=True
    ).stdout
    tus = {}
    for line in cmds.splitlines():
        argv = shlex.split(line)
        if "-c" not in argv:
            continue
        o = argv[argv.index("-o") + 1]
        m = re.match(rf"obj/{re.escape(platform)}\.[^/]+/([^/.]+)/(.*)\.o$", o)
        if not m:
            continue
        tus[(CMAKE_TARGET.get(m.group(1), m.group(1)), m.group(2))] = flags(argv)
    return tus


def main() -> int:
    base_dir, build, platform = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    a = baseline(base_dir / "commands.txt")
    b = buildlog(build, platform)
    bad = 0
    for k in sorted(a.keys() - b.keys()):
        print(f"only CMake:    {k[0]} {k[1]}")
        bad += 1
    for k in sorted(b.keys() - a.keys()):
        print(f"only buildlog: {k[0]} {k[1]}")
        bad += 1
    for k in sorted(a.keys() & b.keys()):
        if a[k] != b[k]:
            print(f"flags differ:  {k[0]} {k[1]}")
            print(f"    CMake only:    {' '.join(sorted(a[k] - b[k]))}")
            print(f"    buildlog only: {' '.join(sorted(b[k] - a[k]))}")
            bad += 1
    print(f"{len(a.keys() & b.keys())} TUs in common, {bad} differences")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
