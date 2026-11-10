# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read configured values out of a hand-written CMake package config file.

A candidate generic library (not LLVM-specific): `<Pkg>Config.cmake` files
are hand-written, but the values a consumer needs are usually
configure_file() output, i.e. top-level `set(NAME "literal")` lines. This
reader returns exactly those, for the names asked, with file:line, and
refuses everything else: a name set inside if()/foreach()/function(), set
twice, or computed from another variable is an error, never a guess. The one
computation it evaluates is the install-prefix idiom

    get_filename_component(P "${CMAKE_CURRENT_LIST_FILE}" REALPATH)
    get_filename_component(P "${P}" PATH)          # N times

so `${P}` may appear in a value. It never runs CMake and reads no other file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from buildlog_cpp.importers.cmake_export import tokenize

_OPEN = {"if", "foreach", "while", "function", "macro", "block"}
_CLOSE = {"endif", "endforeach", "endwhile", "endfunction", "endmacro", "endblock"}
_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class ConfigError(Exception):
    pass


def read(path: Path, names: list[str]) -> dict[str, tuple[str, int]]:
    """{name: (value, line)} for every requested name, or ConfigError."""
    cmds = tokenize(path.read_text())
    depth = 0
    prefixes: dict[str, str] = {}
    found: dict[str, list[tuple[int, int, list[str]]]] = {}
    for c in cmds:
        if c.name in _OPEN:
            depth += 1
        elif c.name in _CLOSE:
            depth -= 1
        elif c.name == "get_filename_component" and depth == 0 and len(c.args) == 3:
            var, src, mode = (a.raw for a in c.args)
            if src == "${CMAKE_CURRENT_LIST_FILE}" and mode == "REALPATH":
                prefixes[var] = os.path.realpath(path)
            elif src == f"${{{var}}}" and mode == "PATH" and var in prefixes:
                prefixes[var] = os.path.dirname(prefixes[var])
        elif c.name == "set" and c.args and c.args[0].raw in names:
            found.setdefault(c.args[0].raw, []).append((c.line, depth, [a.raw for a in c.args[1:]]))
    out = {}
    for n in names:
        sets = found.get(n, [])
        if not sets:
            raise ConfigError(f"{path}: {n} is not set")
        if len(sets) > 1 or sets[0][1] != 0:
            raise ConfigError(f"{path}:{sets[0][0]}: {n} is set conditionally or more than once")
        line, _, args = sets[0]
        if args[-1:] == ["PARENT_SCOPE"] or "CACHE" in args:
            raise ConfigError(f"{path}:{line}: {n}: only plain set() is supported")

        def sub(m: re.Match[str], line: int = line, n: str = n) -> str:
            if m.group(1) not in prefixes:
                raise ConfigError(f"{path}:{line}: {n} reads ${{{m.group(1)}}}")
            return prefixes[m.group(1)]

        out[n] = (";".join(_REF.sub(sub, a) for a in args), line)
    return out
