# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configured `<Pkg>Config.cmake` values: a strict literal reader.

A package's `<Pkg>Config.cmake` (and `<Pkg>ConfigVersion.cmake`) is
hand-written CMake, configured at the package's build time. Many of them
(LLVM's, Clang's, LLD's) state their configuration as plain top-level
`set(VAR value)` lines. This reader returns one such value, typed and
normalized, with its `file:line`; it never evaluates CMake. Anything else
is an error, never a guess:

- the variable must be set exactly once, by a top-level `set(VAR ...)`
  (not inside if/foreach/function/macro/..., not with CACHE or
  PARENT_SCOPE);
- no other command may name the variable as an argument, except the
  read-only `if`/`elseif`/`while`/`message` and a `foreach` that does not
  loop over it (a `list(APPEND VAR ...)`, `string(... VAR)` and the like
  make the value computed);
- the value may reference exactly one variable: the file's install prefix,
  computed by the relocation idiom

      get_filename_component(P "${CMAKE_CURRENT_LIST_FILE}" REALPATH)
      get_filename_component(P "${P}" PATH)      # N times

  which is evaluated (the realpath of the file, N directories up). The
  idiom's variable itself can be read (form `path`).

Forms (the caller, a discovery package, states the type it expects):

  string         field value
  bool           field value = on | off (CMake's constants; others are errors)
  version        fields value, major, minor, patch, tweak (as present)
  list           items (pos, "item", x): the CMake list (`;` and arguments)
  path           field value: one absolute path, normalized
  paths          items (pos, "path", p): absolute paths, normalized
  compile_flags  items (pos, "define", X[=Y]) and (pos, "include_dir", p):
                 whitespace-separated -D/-I flags; any other flag is an error

A Python helper library for discovery plugins (formerly the cmake_config_find/_value probes of
buildlog-cpp's L3_config.dl). `find_configs(name, prefix)` lists the config
files CMake's config mode would consider for find_package(name) under one
prefix (a subset of its search procedure: `<prefix>/(lib|share)/cmake/<name>*/`
and `<prefix>/<name>*/(lib|share)/cmake/<name>*/`, files `<Name>Config.cmake`
or `<lower-name>-config.cmake`), realpathed, each with its version file
when there is one. It does not pick one: selection (unique or pinned) is
the core's. `value(file, var, form)` reads one value with the `file:line`
of every part; `find_watched` and `value_watched` are what they read, for
a probe's cache fingerprint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .cmake_export import Arg, Cmd, tokenize
from .common import MetadataError, stat_token

FORMS = ("string", "bool", "version", "list", "path", "paths", "compile_flags")
READERS = {"if", "elseif", "while", "message", "else", "endif", "endwhile"}
BLOCK_OPEN = {"if", "foreach", "while", "function", "macro", "block"}
BLOCK_CLOSE = {"endif", "endforeach", "endwhile", "endfunction", "endmacro", "endblock"}
TRUE = {"1", "ON", "YES", "TRUE", "Y"}
FALSE = {"0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND", ""}
_REF = re.compile(r"\$\{([^}]*)\}")
_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")
VERSION_FIELDS = ("major", "minor", "patch", "tweak")
# Bump when the reader's output changes, to invalidate cached reads.
READER_VERSION = "2"


def _is_ref_to(arg: Arg, var: str) -> bool:
    return arg.raw == "${" + var + "}"


def _idiom(cmds: list[Cmd], path: Path) -> tuple[str, str, int, set[int]] | None:
    """(variable, value, line, command indexes) of the relocation idiom."""
    for i, c in enumerate(cmds):
        a = c.args
        if (
            c.name == "get_filename_component"
            and len(a) == 3
            and a[1].raw == "${CMAKE_CURRENT_LIST_FILE}"
            and a[2].raw == "REALPATH"
        ):
            var, value, used, line = a[0].raw, os.path.realpath(path), {i}, c.line
            for j in range(i + 1, len(cmds)):
                b = cmds[j].args
                if (
                    cmds[j].name == "get_filename_component"
                    and len(b) == 3
                    and b[0].raw == var
                    and _is_ref_to(b[1], var)
                    and b[2].raw == "PATH"
                ):
                    value, line = os.path.dirname(value), cmds[j].line
                    used.add(j)
                else:
                    break
            return var, value, line, used
    return None


def _decode(arg: Arg, refs: dict[str, str]) -> str:
    raw, out, i = arg.raw, [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == ";":
                raise MetadataError(arg.line, "escaped list separator is not supported")
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
        elif raw.startswith("${", i) or raw.startswith("$ENV{", i) or raw.startswith("$CACHE{", i):
            m = _REF.match(raw, i)
            if not m or m.group(1) not in refs:
                raise MetadataError(
                    arg.line, f"computed value: variable reference {raw[i : i + 40]!r}"
                )
            out.append(refs[m.group(1)])
            i = m.end()
        elif raw.startswith("$<", i):
            raise MetadataError(arg.line, "computed value: generator expression")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def read_value(path: Path, var: str) -> tuple[list[tuple[str, int]], int]:
    """The literal value of `var`: its CMake list elements with their lines,
    and the line of the defining command."""
    text = path.read_text()
    cmds = tokenize(text)
    idiom = _idiom(cmds, path)
    refs = {idiom[0]: idiom[1]} if idiom else {}
    if idiom and var == idiom[0]:
        # The idiom's own variable: must not be modified anywhere else.
        for i, c in enumerate(cmds):
            if (
                i not in idiom[3]
                and c.name not in READERS
                and any(not a.quoted and a.raw == var for a in c.args)
            ):
                raise MetadataError(c.line, f"{var} is also modified by {c.name}()")
        return [(idiom[1], idiom[2])], idiom[2]
    found: list[Cmd] = []
    depth = 0
    for i, c in enumerate(cmds):
        if c.name in BLOCK_CLOSE:
            depth -= 1
        names_var = [a for a in c.args if not a.quoted and a.raw == var]
        if c.name == "set" and c.args and not c.args[0].quoted and c.args[0].raw == var:
            if depth > 0:
                raise MetadataError(c.line, f"{var} is set conditionally (inside a block)")
            found.append(c)
        elif names_var and c.name not in READERS:
            if c.name == "foreach" and c.args[0].raw != var:
                pass
            else:
                raise MetadataError(c.line, f"computed value: {var} is modified by {c.name}()")
        if c.name in BLOCK_OPEN:
            depth += 1
        elif c.name == "else" or c.name == "elseif":
            pass
    if not found:
        raise MetadataError(None, f"{var} is not set")
    if len(found) > 1:
        lines = ", ".join(str(c.line) for c in found)
        raise MetadataError(found[1].line, f"{var} is set more than once (lines {lines})")
    cmd = found[0]
    args = cmd.args[1:]
    for a in args:
        if not a.quoted and a.raw in ("CACHE", "PARENT_SCOPE"):
            raise MetadataError(cmd.line, f"set({var} ... {a.raw}) is not a literal value")
    items: list[tuple[str, int]] = []
    for a in args:
        value = _decode(a, refs)
        if a.quoted or ";" in value:
            items += [(x, a.line) for x in value.split(";")] if value else [("", a.line)]
        elif value:
            items.append((value, a.line))
    return items, cmd.line


def _norm_path(p: str, line: int) -> str:
    if not os.path.isabs(p):
        raise MetadataError(line, f"not an absolute path: {p!r}")
    return os.path.normpath(p)


def typed(items: list[tuple[str, int]], line: int, form: str):
    """(fields, items) of one value: [(key, value, line)], [(pos, key, value, line)]."""
    joined = ";".join(x for x, _ in items)
    if form == "string":
        return [("value", joined, line)], []
    if form == "bool":
        u = joined.upper()
        if u in TRUE:
            return [("value", "on", line)], []
        if u in FALSE or u.endswith("-NOTFOUND"):
            return [("value", "off", line)], []
        raise MetadataError(line, f"not a CMake boolean constant: {joined!r}")
    if form == "version":
        if not _VERSION.fullmatch(joined):
            raise MetadataError(line, f"not a version: {joined!r}")
        parts = joined.split(".")
        return [("value", joined, line)] + [
            (k, str(int(v)), line) for k, v in zip(VERSION_FIELDS, parts, strict=False)
        ], []
    if form == "path":
        if len(items) != 1:
            raise MetadataError(line, f"expected one path, got {len(items)} values")
        return [("value", _norm_path(items[0][0], items[0][1]), line)], []
    if form == "list":
        return [], [(i, "item", x, ln) for i, (x, ln) in enumerate(items) if x]
    if form == "paths":
        return [], [(i, "path", _norm_path(x, ln), ln) for i, (x, ln) in enumerate(items) if x]
    if form == "compile_flags":
        out = []
        for x, ln in items:
            if '"' in x or "'" in x or "\\" in x:
                raise MetadataError(ln, f"quoted compile flags are not supported: {x!r}")
            for flag in x.split():
                if flag.startswith("-D") and len(flag) > 2:
                    out.append((len(out), "define", flag[2:], ln))
                elif flag.startswith("-I") and len(flag) > 2:
                    out.append((len(out), "include_dir", _norm_path(flag[2:], ln), ln))
                else:
                    raise MetadataError(ln, f"compile flag {flag!r} is not a -D or -I flag")
        return [], out
    raise MetadataError(None, f"unknown form {form!r}")


@dataclass(frozen=True)
class Value:
    """One typed value with the `file:line` origin of every part: fields
    [(key, value, origin)] and items [(pos, key, value, origin)]; `origin`
    is the set() line."""

    origin: str
    fields: list[tuple[str, str, str]]
    items: list[tuple[int, str, str, str]]

    def field(self, key: str) -> str | None:
        return next((v for k, v, _ in self.fields if k == key), None)


def value(path: Path, var: str, form: str) -> Value:
    """Read and type one value; MetadataError (with the offending line) or
    OSError otherwise. `error_text` spells an error as the probes did."""
    items, line = read_value(path, var)
    fields, elems = typed(items, line, form)
    return Value(
        f"{path}:{line}",
        [(k, v, f"{path}:{ln}") for k, v, ln in fields],
        [(pos, k, v, f"{path}:{ln}") for pos, k, v, ln in elems],
    )


def error_text(path: Path, var: str, exc: Exception) -> tuple[str, str]:
    """(message, origin) of a failed `value`."""
    if isinstance(exc, MetadataError):
        where = f"{path}:{exc.line}" if exc.line else str(path)
        return f"{where}: {var}: {exc.message}", where
    return f"{path}: cannot read: {getattr(exc, 'strerror', None) or exc}", str(path)


def value_watched(path: Path) -> list:
    """What `value` reads (for a cache fingerprint)."""
    return [READER_VERSION, stat_token(path)]


# ---- config-mode search -----------------------------------------------------


def _dirs_named(parent: Path, name: str) -> list[Path]:
    try:
        return sorted(
            p for p in parent.iterdir() if p.is_dir() and p.name.lower().startswith(name.lower())
        )
    except OSError:
        return []


def search_dirs(name: str, prefix: Path) -> list[Path]:
    """The package directories under one prefix (the documented subset)."""
    out: list[Path] = []
    for lib in ("lib", "share"):
        out += _dirs_named(prefix / lib / "cmake", name)
    for top in _dirs_named(prefix, name):
        for lib in ("lib", "share"):
            out += _dirs_named(top / lib / "cmake", name)
    return out


def _config_files(name: str, d: Path) -> list[Path]:
    try:
        names = set(os.listdir(d))
    except OSError:
        return []
    return [d / f for f in (f"{name}Config.cmake", f"{name.lower()}-config.cmake") if f in names]


def _version_file(cfg: Path) -> str:
    stem = cfg.name.removesuffix(".cmake")
    for v in (f"{stem}Version.cmake", f"{stem}-version.cmake"):
        if (cfg.parent / v).is_file():
            return os.path.realpath(cfg.parent / v)
    return ""


def find_configs(name: str, prefix: Path) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for d in search_dirs(name, prefix):
        for cfg in _config_files(name, d):
            found.setdefault(os.path.realpath(cfg), _version_file(cfg))
    return sorted(found.items())


def find_watched(name: str, prefix: Path) -> list:
    """What `find_configs` reads (for a cache fingerprint)."""
    dirs = search_dirs(name, prefix)
    return [
        READER_VERSION,
        [str(d) for d in _dirs_named(prefix, name)],
        [[str(d), sorted(os.listdir(d)), os.path.realpath(d)] for d in dirs],
    ]
