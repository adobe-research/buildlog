#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""CMAKE_CROSSCOMPILING_EMULATOR for the macOS -> Linux Halide cross build (M3 phase 7c).

    host-twin.py --sysroot S --host-build H --target-build T [--host-bin DIR]... -- PROG ARGS...

This is not an emulator. CMake prepends it to every build-time command that runs an
executable of the target build (binary2cpp, build_halide_h, regexp_replace, the
weights tools) and Halide prepends it to its clang/llvm-as runtime commands. It runs
the macOS *twin* of PROG instead:

  - PROG under the sysroot (Linux clang-22, llvm-as, flatc ...): the executable of the
    same name in the first --host-bin directory that has it (Homebrew llvm@22, ...);
  - PROG under the target build T: the same relative path under the host build H,
    which must have been built beforehand (CMake does not know about that dependency);
  - `-p PLUGIN[,PLUGIN]` arguments under T (autoscheduler plugins handed to a macOS
    generator) are mapped to their H twins in the same way.

On Linux (ctest inside the container) it execs PROG unchanged.
"""

import os
import shutil
import sys


def main() -> int:
    argv = sys.argv[1:]
    opts: dict[str, list[str]] = {
        "--sysroot": [],
        "--host-build": [],
        "--target-build": [],
        "--host-bin": [],
    }
    while argv and argv[0] != "--":
        opts[argv[0]].append(argv[1])
        argv = argv[2:]
    argv = argv[1:]
    if sys.platform.startswith("linux"):
        os.execv(argv[0], argv)

    sysroot = os.path.realpath(opts["--sysroot"][0])
    host = opts["--host-build"][0]
    target = opts["--target-build"][0]

    def twin(path: str) -> str:
        real = os.path.realpath(path)
        if real.startswith(sysroot + "/"):
            name = os.path.basename(path)
            for d in opts["--host-bin"]:
                cand = os.path.join(d, name)
                if os.access(cand, os.X_OK):
                    return cand
            found = shutil.which(name)
            if found:
                return found
            sys.exit(f"host-twin: no host executable named {name} for {path}")
        if os.path.abspath(path).startswith(target + "/"):
            cand = host + os.path.abspath(path)[len(target) :]
            if not os.path.exists(cand):
                sys.exit(f"host-twin: {cand} does not exist; build it in the host tree first")
            return cand
        return path

    out = [twin(argv[0])]
    it = iter(argv[1:])
    for a in it:
        out.append(a)
        if a == "-p":
            v = next(it, "")
            out.append(",".join(twin(x) if x else x for x in v.split(",")))
    os.execv(out[0], out)


if __name__ == "__main__":
    sys.exit(main())
