# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""pkg-config `.pc` importer.

Parsed here, not by shelling out to pkg-config, so every fact keeps the file
and line it came from. Supported: variables (`name=value`, `${var}`, `$$`,
the builtin `${pcfiledir}`, backslash line continuation, `#` comments),
`Cflags`/`Cflags.private`, `Libs`/`Libs.private`, `Requires`/
`Requires.private` (edges by module name; version constraints are not
checked), `Provides` (pkgconf: the module also answers to these names, as
aliases), and the variable `buildlog_rules` (a rule package directory,
relative to the `.pc` file, fed to the outer loop). Descriptive fields
(`Name`, `Description`, `URL`, `License`, `Maintainer`, `Copyright`,
`Source`, `Conflicts`) are ignored; any other field is an error. The
module is its own package (`res_pkg_package`), and `Version` is its
version.

Names. The module `libpng` (`libpng.pc`) is the target `pc:libpng`, and
`Requires: zlib` is an edge to `pc:zlib`: pkg-config names are pkg-config's
own namespace. The file states the module's name, so the import also
states the binding `alias("libpng", "pc:libpng")`; `Provides: other` states
`alias("pc:other", "pc:libpng")` and `alias("other", "pc:libpng")`. A
search for the bare name `libpng` looks for `libpng.pc`.

Flag mapping. buildlog has no separate static link mode, so the private
fields are read as `pkg-config --static` reads them (union with the public
ones). `-I`/`-isystem` become `cc.include_dir`, `-D` `cc.define`, other
cflags `cc.compile_flag`. `-l<name>` resolves against the `-L` directories
of the same file, in order (`.tbd`, `.dylib`, `.so`, `.a`, as ld64 searches),
to a concrete link file (a link input and an `import_file` for coverage);
an unresolved `-l<name>` stays a link flag for the linker's default search.
`-L` itself is never emitted. `-framework X`, `-Wl,...`, `-Xlinker x` and
other options are link flags; absolute library paths are link files.

A file under a platform's sysroot (core: platform_sysroot) describes the
target's file system: its absolute -I and -L directories are prefixed with the
sysroot, as pkg-config does with PKG_CONFIG_SYSROOT_DIR (unless already there).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .common import (
    LIB_SUFFIXES,
    PC_PREFIX,
    TRUE,
    MetadataError,
    Result,
    exact,
    is_library_path,
    pc_id,
    resolve_lib,
    stat_token,
    under_sysroot,
)

LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*([:=])\s*(.*?)\s*$")
VAR_RE = re.compile(r"\$\$|\$\{([^}]*)\}")
IGNORED_FIELDS = {
    "Name",
    "Description",
    "Version",
    "URL",
    "License",
    "Maintainer",
    "Copyright",
    "Source",
    "Conflicts",
}
FIELDS = {
    "Requires",
    "Requires.private",
    "Provides",
    "Cflags",
    "Cflags.private",
    "Libs",
    "Libs.private",
}
RULES_VAR = "buildlog_rules"
VERSION_OPS = {"=", "<", ">", "<=", ">=", "!="}


@dataclass
class PcFile:
    path: Path
    variables: dict[str, tuple[str, int]] = field(default_factory=dict)
    fields: dict[str, tuple[str, int]] = field(default_factory=dict)


def _logical_lines(text: str):
    """(line number, text) with continuations joined and comments stripped."""
    pending, start = "", 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = no
        if raw.endswith("\\"):
            pending += raw[:-1]
            continue
        yield start, (pending + raw).split("#", 1)[0]
        pending = ""
    if pending:
        yield start, pending.split("#", 1)[0]


def parse(path: Path) -> PcFile:
    pc = PcFile(path)
    env = {"pcfiledir": os.path.dirname(os.path.abspath(path))}
    for no, line in _logical_lines(path.read_text()):
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if not m:
            raise MetadataError(no, f"not a variable or field line: {line.strip()!r}")
        key, sep, value = m.groups()
        expanded = _expand(value, env, no)
        if sep == "=":
            if key in pc.variables:
                raise MetadataError(no, f"variable {key} defined twice")
            pc.variables[key] = (expanded, no)
            env[key] = expanded
        else:
            if key in pc.fields:
                raise MetadataError(no, f"field {key} given twice")
            if key not in FIELDS and key not in IGNORED_FIELDS:
                raise MetadataError(no, f"unknown field {key}")
            pc.fields[key] = (expanded, no)
    return pc


def _expand(value: str, env: dict[str, str], line: int) -> str:
    def repl(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group(1)
        if name not in env:
            raise MetadataError(line, f"undefined variable ${{{name}}}")
        return env[name]

    return VAR_RE.sub(repl, value)


def _split(value: str, line: int) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise MetadataError(line, f"cannot split {value!r}: {exc}") from exc


def requires(value: str, line: int) -> list[str]:
    """Package names of a Requires list (`a >= 1.0, b c`)."""
    tokens = [t for t in re.split(r"[\s,]+", value) if t]
    names: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in VERSION_OPS:
            if not names or i + 1 >= len(tokens):
                raise MetadataError(line, f"dangling version constraint in {value!r}")
            i += 2
            continue
        names.append(tok)
        i += 1
    return names


def _pairs(tokens: list[str], line: int, joined: tuple[str, ...]):
    """Tokens with options that take a separate argument (`-I dir`) rejoined."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in joined:
            if i + 1 >= len(tokens):
                raise MetadataError(line, f"option {tok} needs an argument")
            yield tok, tokens[i + 1]
            i += 2
        else:
            yield tok, None
            i += 1


def import_file(res: Result, results=None) -> None:
    pc = parse(res.file)
    t = pc_id(res.file.stem)
    res.target(t, "interface", 1)
    res.alias(res.file.stem, t, 1)
    res.package(t, res.file.stem, "", 1)
    if "Version" in pc.fields and pc.fields["Version"][0]:
        res.version_string(t, pc.fields["Version"][0], pc.fields["Version"][1])
    for key in ("Requires", "Requires.private"):
        if key in pc.fields:
            value, line = pc.fields[key]
            for d in requires(value, line):
                res.dep(t, pc_id(d), "public", TRUE, line)
    if "Provides" in pc.fields:
        value, line = pc.fields["Provides"]
        for other in requires(value, line):
            if pc_id(other) != t:
                res.alias(pc_id(other), t, line)
                res.alias(other, t, line)
    for key in ("Cflags", "Cflags.private"):
        if key in pc.fields:
            value, line = pc.fields[key]
            for tok, arg in _pairs(_split(value, line), line, ("-I", "-isystem", "-D")):
                if arg is not None:
                    if tok == "-D":
                        res.prop(t, "cc.define", arg, TRUE, line)
                    else:
                        res.prop(t, "cc.include_dir", res.under_sysroot(arg), TRUE, line)
                elif tok.startswith("-isystem"):
                    d = res.under_sysroot(tok[len("-isystem") :])
                    res.prop(t, "cc.include_dir", d, TRUE, line)
                elif tok.startswith("-I"):
                    res.prop(t, "cc.include_dir", res.under_sysroot(tok[2:]), TRUE, line)
                elif tok.startswith("-D"):
                    res.prop(t, "cc.define", tok[2:], TRUE, line)
                else:
                    res.prop(t, "cc.compile_flag", tok, TRUE, line)
    dirs = _lib_dirs(res.sysroot, pc)
    for key in ("Libs", "Libs.private"):
        if key in pc.fields:
            value, line = pc.fields[key]
            _libs(res, t, _split(value, line), line, dirs)
    if RULES_VAR in pc.variables:
        value, line = pc.variables[RULES_VAR]
        path = Path(value) if Path(value).is_absolute() else res.file.parent / value
        res.rule_package(str(path.resolve()), line)


LIBS_JOINED = ("-L", "-l", "-framework", "-weak_framework", "-Xlinker")


def _lib_dirs(sysroot: str, pc) -> list[str]:
    """Every -L of Libs and Libs.private: like ld, the directories apply to
    every -l, wherever it appears."""
    dirs: list[str] = []
    for key in ("Libs", "Libs.private"):
        if key in pc.fields:
            value, line = pc.fields[key]
            for tok, arg in _pairs(_split(value, line), line, LIBS_JOINED):
                if tok == "-L" or (arg is None and tok.startswith("-L")):
                    dirs.append(under_sysroot(sysroot, arg if arg is not None else tok[2:]))
    return dirs


def _libs(res: Result, t: str, tokens: list[str], line: int, dirs: list[str]) -> None:
    for tok, arg in _pairs(tokens, line, LIBS_JOINED):
        if tok == "-L" or (arg is None and tok.startswith("-L")):
            continue
        elif tok == "-l" or (arg is None and tok.startswith("-l")):
            lib = arg if arg is not None else tok[2:]
            if res.symbolic_paths:
                expression = res.path_expression("library", [lib, dirs, LIB_SUFFIXES], line)
                res.link(t, expression, TRUE, line)
                res.prop(t, "native.link_flag", expression, TRUE, line)
                continue
            found = resolve_lib(lib, dirs, res.observations)
            if found:
                res.link(t, found, TRUE, line)
            else:
                res.prop(t, "native.link_flag", f"-l{lib}", TRUE, line)
        elif arg is not None:
            res.prop(t, "native.link_flag", f"{tok} {arg}", TRUE, line)
        elif tok.startswith("-"):
            res.prop(t, "native.link_flag", tok, TRUE, line)
        elif is_library_path(tok):
            res.link(t, res.under_sysroot(tok), TRUE, line)
        else:
            raise MetadataError(line, f"unrecognized Libs item {tok!r}")


def module_of(name: str) -> str | None:
    """The module a `pc:<module>` id or a bare name names."""
    module = name.removeprefix(PC_PREFIX)
    if not module or "/" in module or ":" in module:
        return None
    return module


def by_alias(name: str) -> bool:
    """A bare module name is the alias of `pc:<module>`."""
    return not name.startswith("pc:")


def find(name: str, directory: Path) -> list[Path]:
    module = module_of(name)
    if module is None:
        return []
    p = directory / f"{module}.pc"
    return [p] if p.is_file() and exact(p) else []


def offers(file: Path, directory: Path, names, observations) -> set[str]:
    if (
        file.parent != directory
        or file.suffix != ".pc"
        or module_of(file.stem) != file.stem
        or not exact(file)
    ):
        return set()
    return {file.stem, pc_id(file.stem)}


def enumerate_files(directory: Path, observations) -> list[Path]:
    return [p for p in observations.directory(directory) if p.suffix == ".pc" and p.is_file()]


def metadata_links(file: Path, observations):
    from buildlog.providers import MetadataLinks

    observations.file(file)
    try:
        pc = parse(file)
    except (MetadataError, OSError, UnicodeError) as exc:
        return MetadataLinks(error=str(exc))
    if RULES_VAR not in pc.variables:
        return MetadataLinks()
    value = pc.variables[RULES_VAR][0]
    path = Path(value) if Path(value).is_absolute() else file.parent / value
    return MetadataLinks(rules=(Path(os.path.abspath(path)),))


def find_watched(name: str, directory: Path) -> list:
    module = module_of(name)
    if module is None:
        return []
    return [stat_token(directory), stat_token(directory / f"{module}.pc")]


def watched(path: Path, sysroot: str = "") -> list:
    """What an import of `path` reads: the file and the -l candidates."""
    out = [stat_token(path)]
    try:
        pc = parse(path)
        dirs = _lib_dirs(sysroot, pc)
        libs = []
        for key in ("Libs", "Libs.private"):
            if key in pc.fields:
                value, line = pc.fields[key]
                for tok, arg in _pairs(_split(value, line), line, LIBS_JOINED):
                    if tok == "-l" or (arg is None and tok.startswith("-l")):
                        libs.append(arg if arg is not None else tok[2:])
    except (MetadataError, OSError):
        return out
    for lib in libs:
        for d in dirs:
            out += [stat_token(Path(d) / f"lib{lib}{s}") for s in LIB_SUFFIXES]
    return out
