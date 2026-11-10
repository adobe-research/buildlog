# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Port the container's copy of the worktree to the Linux host's names.

The tests, examples and case studies name the macOS host platform and
toolchain as data: configurations on `arm64-macos`, toolchain pins and
presets for `apple-clang`. On Linux the same projects are built for
`<cpu>-linux` with `gcc` or `clang` (buildlog-cpp's Linux inventory), so
this rewrites those names in the copy (never in the worktree itself:
linux/run.sh copies it first). File and directory names are renamed the
same way. Everything else (Mach-O spellings, .dylib names, universal2) is
left as is: what fails or skips because of it is reported, not hidden.

    python3 linux/port.py <toolchain>     # gcc | clang
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREES = ("tests", "examples", "case-studies")
SUFFIXES = {".toml", ".py", ".json", ".dl", ".txt", ".cmake", ".cps", ".pc", ".sh"}


def main() -> None:
    toolchain = sys.argv[1] if len(sys.argv) > 1 else "gcc"
    cpu = {"aarch64": "arm64"}.get(platform.machine(), platform.machine())
    subs = [(f"{cpu}-macos", f"{cpu}-linux"), ("apple-clang", toolchain)]
    changed = 0
    for tree in TREES:
        paths = sorted((ROOT / tree).rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for path in paths:
            if {"buildlog-build", "__pycache__", "upstream"} & set(path.relative_to(ROOT).parts):
                continue  # upstream: the case studies' source submodules, kept as they are
            if path == ROOT / "tests" / "host.py":  # names the host itself
                continue
            if path.is_file() and path.suffix in SUFFIXES:
                text = path.read_text(errors="surrogateescape")
                new = text
                for old, rep in subs:
                    new = new.replace(old, rep)
                if new != text:
                    path.write_text(new, errors="surrogateescape")
                    changed += 1
            name = path.name
            for old, rep in subs:
                name = name.replace(old, rep)
            if name != path.name:
                path.rename(path.with_name(name))
    (ROOT / "linux" / ".ported").write_text("")
    print(f"port: {cpu}-macos -> {cpu}-linux, apple-clang -> {toolchain}: {changed} files")


if __name__ == "__main__":
    main()
