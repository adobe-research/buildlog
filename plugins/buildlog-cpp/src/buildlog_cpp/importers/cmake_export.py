# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""CMake export file importer (`install(EXPORT)` and `export()` output).

These files are templated output of CMake's export generators
(Source/cmExport{CMakeConfig,InstallCMakeConfig,BuildCMakeConfig,File}
Generator.cxx), not general CMake. This module accepts exactly that template
output and rejects anything else with `file:line: unknown construct`; it
never evaluates CMake.

Grammar (in order; `@X@` is a captured value, `...` a repetition):

    main file   := POLICY_HEADER VERSION_SET [FIND_DEPENDENCIES] EXPECTED
                   [IMPORT_PREFIX]                 # install(EXPORT) only
                   TARGET_BLOCK...
                   (LOAD_CONFIGS | CONFIG_BLOCK...)  # install | export()
                   [CXX_MODULES]                   # rejected
                   [CLEANUP CHECK_LOOP]            # install(EXPORT) only
                   [MISSING_TARGETS] VERSION_UNSET POLICY_POP
    config file := VERSION_SET CONFIG_BLOCK... VERSION_UNSET
    TARGET_BLOCK := add_library(@T@ <KIND> IMPORTED) | add_executable(@T@ IMPORTED)
                    | the SYMBOLIC if/elseif block
                    set_property(TARGET @T@ PROPERTY <FLAG> <v>)...
                    [set_target_properties(@T@ PROPERTIES <prop> <value>...)]
                    [FILE_SET if/else blocks with target_sources(...)]
    CONFIG_BLOCK := set_property(TARGET @T@ APPEND PROPERTY IMPORTED_CONFIGURATIONS @C@)
                    set_target_properties(@T@ PROPERTIES <prop> <value>...)
                    [xcframework if block]           # rejected
                    [list(APPEND _cmake_import_check_targets @T@ )]  # a comment for libraries
                    [list(APPEND _cmake_import_check_files_for_@T@ <files>...)]  # for executables

The boilerplate blocks are matched command by command against templates
transcribed from the generators (version numbers are captures). The
`_IMPORT_PREFIX` computation is evaluated: N parent directories of the
file's path (as found, like CMAKE_CURRENT_LIST_FILE), the /usr-move
REALPATH check, or the configured absolute prefix. The per-config files are
the `file(GLOB)` pattern's matches, each of which must itself be a config
file (write_basic_package_version_file output is skipped). `<Pkg>Config.cmake`
is hand-written CMake and never read: point `package_file` at the targets
file instead.

Property values are CMake quoted arguments (`\\"`, `\\\\`, `\\$` escapes,
`${_IMPORT_PREFIX}` references) whose generator expressions are mapped onto
core conditions (see the genex table below).
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import (
    FALSE,
    PACKAGE_ONLY_CONFIGS,
    TRUE,
    Cond,
    MetadataError,
    Result,
    and_,
    config_cond,
    defined,
    is_mapped_config,
    not_,
    or_,
    os_cond,
    os_domain,
    package_config_cond,
    stat_token,
)

# ---- tokenizer --------------------------------------------------------------


@dataclass
class Arg:
    raw: str  # source text, quotes stripped, escapes undecoded
    quoted: bool
    line: int


@dataclass
class Cmd:
    name: str  # lower-cased
    args: list[Arg]
    line: int

    def texts(self) -> list[str]:
        return [a.raw for a in self.args]


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BRACKET = re.compile(r"\[=*\[")


def tokenize(text: str) -> list[Cmd]:
    """Commands with their arguments; bracket arguments/comments rejected."""
    cmds: list[Cmd] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            if _BRACKET.match(text, i + 1):
                raise MetadataError(line, "unknown construct: bracket comment")
            j = text.find("\n", i)
            i = n if j < 0 else j
        else:
            m = _IDENT.match(text, i)
            if not m:
                raise MetadataError(
                    line, f"unknown construct: {text[i : i + 20].splitlines()[0]!r}"
                )
            name, start = m.group(0), line
            i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i >= n or text[i] != "(":
                raise MetadataError(line, f"unknown construct: {name} without arguments")
            i += 1
            args: list[Arg] = []
            depth = 0
            while True:
                if i >= n:
                    raise MetadataError(start, f"unterminated {name}(")
                c = text[i]
                if c == "\n":
                    line += 1
                    i += 1
                elif c in " \t\r":
                    i += 1
                elif c == "#":
                    if _BRACKET.match(text, i + 1):
                        raise MetadataError(line, "unknown construct: bracket comment")
                    j = text.find("\n", i)
                    i = n if j < 0 else j
                elif c == ")":
                    i += 1
                    if depth == 0:
                        break
                    depth -= 1
                    args.append(Arg(")", False, line))
                elif c == "(":
                    depth += 1
                    i += 1
                    args.append(Arg("(", False, line))
                elif c == '"':
                    j, aline, buf = i + 1, line, []
                    while True:
                        if j >= n:
                            raise MetadataError(aline, "unterminated quoted argument")
                        ch = text[j]
                        if ch == "\\" and j + 1 < n:
                            buf.append(text[j : j + 2])
                            line += text[j + 1] == "\n"
                            j += 2
                            continue
                        if ch == '"':
                            break
                        line += ch == "\n"
                        buf.append(ch)
                        j += 1
                    args.append(Arg("".join(buf), True, aline))
                    i = j + 1
                elif c == "[" and _BRACKET.match(text, i):
                    raise MetadataError(line, "unknown construct: bracket argument")
                else:
                    j = i
                    while j < n and text[j] not in ' \t\r\n()"#':
                        j += 2 if text[j] == "\\" else 1
                    args.append(Arg(text[i:j], False, line))
                    i = j
            cmds.append(Cmd(name.lower(), args, start))
    return cmds


# ---- templates --------------------------------------------------------------

_PH = re.compile(r"@(\*?)([A-Z_]+)@")


class Template:
    """CMake text with `@X@` captures; `@*X@` as the last argument of a
    command captures the remaining arguments."""

    def __init__(self, text: str):
        self.cmds = tokenize(text)

    def match(self, cmds: list[Cmd], at: int) -> tuple[int, dict] | None:
        caps: dict[str, object] = {}
        for k, tc in enumerate(self.cmds):
            if at + k >= len(cmds):
                return None
            c = cmds[at + k]
            if c.name != tc.name:
                return None
            targs, rest = tc.args, None
            if targs and (m := _PH.fullmatch(targs[-1].raw)) and m.group(1):
                rest, targs = m.group(2), targs[:-1]
                if len(c.args) < len(targs):
                    return None
            elif len(c.args) != len(targs):
                return None
            for ta, a in zip(targs, c.args):
                if ta.quoted != a.quoted or not _match_arg(ta.raw, a.raw, caps):
                    return None
            if rest is not None:
                caps[rest] = c.args[len(targs) :]
        return at + len(self.cmds), caps


def _match_arg(pattern: str, raw: str, caps: dict) -> bool:
    parts, names, pos = [], [], 0
    for m in _PH.finditer(pattern):
        parts.append(re.escape(pattern[pos : m.start()]))
        name = m.group(2)
        if name in caps:
            parts.append(re.escape(str(caps[name])))
        elif name in names:
            parts.append(f"(?P={name})")
        else:
            parts.append(f"(?P<{name}>.+?)")
            names.append(name)
        pos = m.end()
    parts.append(re.escape(pattern[pos:]))
    m = re.fullmatch("".join(parts), raw, re.DOTALL)
    if not m:
        return False
    caps.update({k: m.group(k) for k in names})
    return True


T = Template
# The first message states the policy version (newer CMake) or 2.8.0 (older
# generators, e.g. the SPIRV-Headers export Halide vendors).
POLICY_HEADER = T(
    """
if("${CMAKE_MAJOR_VERSION}.${CMAKE_MINOR_VERSION}" LESS 2.8)
   message(FATAL_ERROR "CMake >= @VMIN@ required")
endif()
if(CMAKE_VERSION VERSION_LESS "@V@")
   message(FATAL_ERROR "CMake >= @V@ required")
endif()
cmake_policy(PUSH)
cmake_policy(VERSION @V@...@MAX@)
"""
)
VERSION_SET = T("set(CMAKE_IMPORT_FILE_VERSION 1)")
VERSION_UNSET = T("set(CMAKE_IMPORT_FILE_VERSION)")
POLICY_POP = T("cmake_policy(POP)")
FIND_DEP_START = T(
    """
include(CMakeFindDependencyMacro)
if(DEFINED CMAKE_FIND_PACKAGE_NAME)
  set(_cmake_unwind_arg UNWIND_INCLUDE)
endif()
"""
)
FIND_DEP = T(
    """
__find_dependency_no_return(@*ARGS@)
if(NOT @PKG@_FOUND)
  unset(_cmake_unwind_arg)
  cmake_policy(POP)
  return()
endif()
"""
)
FIND_DEP_END = T("unset(_cmake_unwind_arg)")
EXPECTED = T(
    r"""
set(_cmake_targets_defined "")
set(_cmake_targets_not_defined "")
set(_cmake_expected_targets "")
foreach(_cmake_expected_target IN ITEMS @*TARGETS@)
  list(APPEND _cmake_expected_targets "${_cmake_expected_target}")
  if(TARGET "${_cmake_expected_target}")
    list(APPEND _cmake_targets_defined "${_cmake_expected_target}")
  else()
    list(APPEND _cmake_targets_not_defined "${_cmake_expected_target}")
  endif()
endforeach()
unset(_cmake_expected_target)
if(_cmake_targets_defined STREQUAL _cmake_expected_targets)
  unset(_cmake_targets_defined)
  unset(_cmake_targets_not_defined)
  unset(_cmake_expected_targets)
  unset(CMAKE_IMPORT_FILE_VERSION)
  cmake_policy(POP)
  return()
endif()
if(NOT _cmake_targets_defined STREQUAL "")
  string(REPLACE ";" ", " _cmake_targets_defined_text "${_cmake_targets_defined}")
  string(REPLACE ";" ", " _cmake_targets_not_defined_text "${_cmake_targets_not_defined}")
  message(FATAL_ERROR "Some (but not all) targets in this export set were already defined.\nTargets Defined: ${_cmake_targets_defined_text}\nTargets not yet defined: ${_cmake_targets_not_defined_text}\n")
endif()
unset(_cmake_targets_defined)
unset(_cmake_targets_not_defined)
unset(_cmake_expected_targets)
"""
)
PREFIX_ABSOLUTE = T('set(_IMPORT_PREFIX "@PREFIX@")')
PREFIX_START = T('get_filename_component(_IMPORT_PREFIX "${CMAKE_CURRENT_LIST_FILE}" PATH)')
PREFIX_USR_MOVE = T(
    """
get_filename_component(_realCurr "${_IMPORT_PREFIX}" REALPATH)
get_filename_component(_realOrig "@DEST@" REALPATH)
if(_realCurr STREQUAL _realOrig)
  set(_IMPORT_PREFIX "@DEST@")
endif()
unset(_realOrig)
unset(_realCurr)
"""
)
PREFIX_UP = T('get_filename_component(_IMPORT_PREFIX "${_IMPORT_PREFIX}" PATH)')
PREFIX_END = T(
    """
if(_IMPORT_PREFIX STREQUAL "/")
  set(_IMPORT_PREFIX "")
endif()
"""
)
ADD_LIBRARY = T("add_library(@T@ @KIND@ IMPORTED)")
ADD_EXECUTABLE = T("add_executable(@T@ IMPORTED)")
ADD_SYMBOLIC = T(
    """
if(CMAKE_VERSION VERSION_GREATER_EQUAL "4.2.0")
  add_library(@T@ INTERFACE SYMBOLIC IMPORTED)
elseif(CMAKE_VERSION VERSION_GREATER_EQUAL 3.2.0)
  add_library(@T@ INTERFACE IMPORTED)
  set_target_properties(@T@ PROPERTIES SYMBOLIC TRUE)
endif()
"""
)
# CMake before 3.29 or so, for a policy version below 3.0: a guard against
# consumers too old for the INTERFACE targets it created. No information.
CONSUMER_VERSION = T(
    """
if(CMAKE_VERSION VERSION_LESS @V@)
  message(FATAL_ERROR "This file relies on consumers using CMake @V@ or greater.")
endif()
"""
)
FLAG = T("set_property(TARGET @T@ PROPERTY @P@ @*V@)")
PROPS = T("set_target_properties(@T@ PROPERTIES @*KV@)")
FILE_SET_IF = T('if(NOT CMAKE_VERSION VERSION_LESS "@V@")')
TARGET_SOURCES = T("target_sources(@T@ @*ARGS@)")
FILE_SET_PROP = T("set_property(FILE_SET @*ARGS@)")
ELSE = T("else()")
ENDIF = T("endif()")
FILE_SET_ELSE_HEADERS = T(
    "set_property(TARGET @T@ APPEND PROPERTY INTERFACE_INCLUDE_DIRECTORIES @*DIRS@)"
)
FILE_SET_ELSE_MESSAGE = T("message(@KIND@ @*MSG@)")
LOAD_CONFIGS = T(
    """
file(GLOB _cmake_config_files "${CMAKE_CURRENT_LIST_DIR}/@GLOB@")
foreach(_cmake_config_file IN LISTS _cmake_config_files)
  include("${_cmake_config_file}")
endforeach()
unset(_cmake_config_file)
unset(_cmake_config_files)
"""
)
CXX_MODULES = T('include("${CMAKE_CURRENT_LIST_DIR}/@F@")')
CLEANUP = T("set(_IMPORT_PREFIX)")
_CHECK_BODY = r"""
    foreach(_cmake_file IN LISTS "_cmake_import_check_files_for_${_cmake_target}")
      if(NOT EXISTS "${_cmake_file}")
        message(FATAL_ERROR "The imported target \"${_cmake_target}\" references the file
   \"${_cmake_file}\"
but this file does not exist.  Possible reasons include:
* The file was deleted, renamed, or moved to another location.
* An install or uninstall procedure did not complete successfully.
* The installation package was faulty and contained
   \"${CMAKE_CURRENT_LIST_FILE}\"
but not all the files it references.
")
      endif()
    endforeach()
"""
_CHECK_END = """
  unset(_cmake_file)
  unset("_cmake_import_check_files_for_${_cmake_target}")
endforeach()
unset(_cmake_target)
unset(_cmake_import_check_targets)
"""
CHECK_LOOP = T(
    """
foreach(_cmake_target IN LISTS _cmake_import_check_targets)
  if(CMAKE_VERSION VERSION_LESS "3.28"
      OR NOT DEFINED _cmake_import_check_xcframework_for_${_cmake_target}
      OR NOT IS_DIRECTORY "${_cmake_import_check_xcframework_for_${_cmake_target}}")"""
    + _CHECK_BODY
    + "  endif()"
    + _CHECK_END
)
# CMake 3.24 to 3.27: the same loop without the xcframework guard.
CHECK_LOOP_324 = T(
    "foreach(_cmake_target IN LISTS _cmake_import_check_targets)" + _CHECK_BODY + _CHECK_END
)
MISSING = T(
    """
unset(${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets)
foreach(_target @*TARGETS@)
  if(NOT TARGET "${_target}" )
    set(${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets "${${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets} ${_target}")
  endif()
endforeach()
if(DEFINED ${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets)
  if(CMAKE_FIND_PACKAGE_NAME)
    set( ${CMAKE_FIND_PACKAGE_NAME}_FOUND FALSE)
    set( ${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE "The following imported targets are referenced, but are missing: ${${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets}")
  else()
    message(FATAL_ERROR "The following imported targets are referenced, but are missing: ${${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets}")
  endif()
endif()
unset(${CMAKE_FIND_PACKAGE_NAME}_NOT_FOUND_MESSAGE_targets)
"""
)
CFG_START = T("set_property(TARGET @T@ APPEND PROPERTY IMPORTED_CONFIGURATIONS @C@)")
CFG_XCFRAMEWORK = T('if(NOT CMAKE_VERSION VERSION_LESS "3.28" AND IS_DIRECTORY @*X@)')
CFG_CHECK_TARGETS = T("list(APPEND _cmake_import_check_targets @T@ )")
CFG_CHECK_XCFRAMEWORK = T("set(_cmake_import_check_xcframework_for_@T@ @*X@)")
CFG_CHECK_FILES = T("list(APPEND _cmake_import_check_files_for_@T@ @*FILES@)")
del T

LIB_KINDS = {
    "STATIC": "library",
    "SHARED": "library",
    "UNKNOWN": "library",
    "MODULE": "module",
    "INTERFACE": "interface",
}
BASIC_VERSION_FILE = "# This is a basic version file for the Config-mode of find_package()."
MAIN_MARK = "# Generated CMake target import file.\n"
CONFIG_MARK = "# Generated CMake target import file for configuration "


# ---- values -----------------------------------------------------------------

_REF = re.compile(r"\$\{([^}]*)\}")


def decode(arg: Arg, prefix: str | None) -> str:
    """The value of an argument, with `${_IMPORT_PREFIX}` bound."""
    raw, out, i = arg.raw, [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
        elif raw.startswith("${", i):
            m = _REF.match(raw, i)
            if not m or m.group(1) != "_IMPORT_PREFIX" or prefix is None:
                raise MetadataError(arg.line, f"unsupported variable reference {raw[i : i + 40]!r}")
            out.append(prefix)
            i = m.end()
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---- generator expressions --------------------------------------------------

# The genexes export files emit, and their meaning:
#   $<CONFIG:a,b>          Eq over native.opt via common.CONFIG_SETTINGS
#   $<PLATFORM_ID:ids>     Eq over platform.os (Darwin -> macos, ...)
#   $<COMPILE_LANGUAGE:l>  true if l lists CXX (buildlog compiles C++),
#                          false for the other known languages
#   $<NOT:>, $<AND:>, $<OR:>, $<BOOL:const>, 0, 1   folded logic
#   $<TARGET_EXISTS:t>, $<TARGET_NAME_IF_EXISTS:t>  Defined(t) (core L1)
#   $<LINK_ONLY:x>         x as a private (link-only) edge
#   $<LINK_LIBRARY:WHOLE_ARCHIVE,t>   an edge plus whole-archive linking
#   $<BUILD_INTERFACE:x>, $<INSTALL_INTERFACE:x>   kept per the file's kind
#   $<ANGLE-R>, $<COMMA>, $<SEMICOLON>             literals
# Anything else is an error naming the expression.
PLATFORMS = {"darwin": "macos", "linux": "linux", "windows": "windows"}
LANGUAGES = {
    "C",
    "CXX",
    "OBJC",
    "OBJCXX",
    "CUDA",
    "Fortran",
    "ASM",
    "HIP",
    "ISPC",
    "Swift",
    "CSharp",
}
BOOL_GENEXES = (
    "NOT",
    "AND",
    "OR",
    "BOOL",
    "CONFIG",
    "PLATFORM_ID",
    "COMPILE_LANGUAGE",
    "TARGET_EXISTS",
)
FALSY = ("", "0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND")


class GenexError(Exception):
    pass


@dataclass
class Genex:
    name: str  # text before the first top-level ':' (may hold genexes)
    args: str | None  # text after it


def _close(text: str, i: int) -> int:
    """Index after the genex starting at text[i] ('$<')."""
    depth, j = 1, i + 2
    while j < len(text) and depth:
        if text.startswith("$<", j):
            depth += 1
            j += 2
            continue
        if text[j] == ">":
            depth -= 1
        j += 1
    if depth:
        raise GenexError(f"unterminated generator expression in {text!r}")
    return j


def _scan(text: str) -> list[str | Genex]:
    out: list[str | Genex] = []
    i, lit = 0, 0
    while i < len(text):
        if text.startswith("$<", i):
            if lit < i:
                out.append(text[lit:i])
            j = _close(text, i)
            body = text[i + 2 : j - 1]
            k = _top_index(body, ":")
            out.append(Genex(body, None) if k < 0 else Genex(body[:k], body[k + 1 :]))
            i = lit = j
        else:
            i += 1
    if lit < len(text):
        out.append(text[lit:])
    return out


def _top_index(text: str, ch: str) -> int:
    i = 0
    while i < len(text):
        if text.startswith("$<", i):
            i = _close(text, i)
            continue
        if text[i] == ch:
            return i
        i += 1
    return -1


def _top_split(text: str, ch: str) -> list[str]:
    out, rest = [], text
    while (k := _top_index(rest, ch)) >= 0:
        out.append(rest[:k])
        rest = rest[k + 1 :]
    out.append(rest)
    return out


def _show(g: Genex) -> str:
    return f"$<{g.name}{'' if g.args is None else ':' + g.args}>"


@dataclass
class Env:
    kind: str  # "install" | "build"
    os_domain: set[str] = field(default_factory=set)

    def keeps(self, name: str) -> bool:
        return name == ("INSTALL_INTERFACE" if self.kind == "install" else "BUILD_INTERFACE")


def eval_bool(text: str, env: Env) -> Cond:
    if text in ("0", "1"):
        return TRUE if text == "1" else FALSE
    parts = _scan(text)
    if len(parts) != 1 or not isinstance(parts[0], Genex):
        raise GenexError(f"{text!r} is not a condition")
    g = parts[0]
    name, args = g.name, g.args or ""
    if name == "NOT":
        return not_(eval_bool(args, env))
    if name in ("AND", "OR"):
        xs = [eval_bool(a, env) for a in _top_split(args, ",")]
        return and_(*xs) if name == "AND" else or_(*xs)
    if name == "BOOL":
        if "$<" in args:
            raise GenexError(f"{_show(g)} of a non-constant")
        up = args.upper()
        return FALSE if up in FALSY or up.endswith("-NOTFOUND") else TRUE
    if name == "CONFIG" and g.args is not None:
        return or_(*[config_cond(c) for c in _top_split(args, ",")])
    if name == "PLATFORM_ID" and g.args is not None:
        out = FALSE
        for pid in _top_split(args, ","):
            os_name = PLATFORMS.get(pid.lower())
            out = or_(out, os_cond(os_name, env.os_domain) if os_name else FALSE)
        return out
    if name == "COMPILE_LANGUAGE" and g.args is not None:
        langs = _top_split(args, ",")
        if any(lang not in LANGUAGES for lang in langs):
            raise GenexError(f"{_show(g)} names an unknown language")
        return TRUE if "CXX" in langs else FALSE
    if name == "TARGET_EXISTS" and g.args is not None:
        if "$<" in args or not args:
            raise GenexError(f"{_show(g)} of a non-constant")
        return defined(args)
    raise GenexError(f"{_show(g)} is not a supported condition")


def eval_text(text: str, env: Env) -> list[tuple[Cond, str]]:
    """Cases (condition, string) of a genex-bearing text."""
    cases: list[tuple[Cond, str]] = [(TRUE, "")]
    for part in _scan(text):
        alts = [(TRUE, part)] if isinstance(part, str) else _eval_genex(part, env)
        cases = [
            (and_(c1, c2), s1 + s2) for c1, s1 in cases for c2, s2 in alts if and_(c1, c2) != FALSE
        ]
    return cases


def _eval_genex(g: Genex, env: Env) -> list[tuple[Cond, str]]:
    name, args = g.name, g.args
    if name in ("ANGLE-R", "COMMA", "SEMICOLON") and args is None:
        return [(TRUE, {"ANGLE-R": ">", "COMMA": ",", "SEMICOLON": ";"}[name])]
    if name.startswith("$<") or name in ("0", "1"):
        c = eval_bool(name, env)
        body = [(and_(c, c2), s) for c2, s in eval_text(args or "", env)]
        return [x for x in body if x[0] != FALSE] + ([(not_(c), "")] if c != TRUE else [])
    if name in ("BUILD_INTERFACE", "INSTALL_INTERFACE", "BUILD_LOCAL_INTERFACE"):
        return eval_text(args or "", env) if env.keeps(name) else [(TRUE, "")]
    if name in BOOL_GENEXES:
        c = eval_bool(_show(g), env)
        return [x for x in ((c, "1"), (not_(c), "0")) if x[0] != FALSE]
    if name == "LINK_ONLY":
        return eval_text(args or "", env)
    raise GenexError(f"{_show(g)} is not a supported generator expression")


@dataclass
class Item:
    cond: Cond
    text: str
    private: bool = False
    whole_archive: bool = False


def eval_list(text: str, env: Env, link: bool = False) -> list[Item]:
    """The items of a CMake list value; `link` enables the link genexes."""
    return [i for elem in _top_split(text, ";") for i in _eval_elem(elem, env, TRUE, False, link)]


def _eval_elem(elem: str, env: Env, cond: Cond, private: bool, link: bool) -> list[Item]:
    if not elem:
        return []
    parts = _scan(elem)
    if len(parts) == 1 and isinstance(parts[0], Genex):
        g = parts[0]
        inner = _top_split(g.args or "", ";")
        if g.name.startswith("$<") or g.name in ("0", "1"):
            c = and_(cond, eval_bool(g.name, env))
            return (
                []
                if c == FALSE
                else [i for e in inner for i in _eval_elem(e, env, c, private, link)]
            )
        if g.name in ("BUILD_INTERFACE", "INSTALL_INTERFACE", "BUILD_LOCAL_INTERFACE"):
            return (
                [i for e in inner for i in _eval_elem(e, env, cond, private, link)]
                if env.keeps(g.name)
                else []
            )
        if link and g.name == "LINK_ONLY":
            return [i for e in inner for i in _eval_elem(e, env, cond, True, link)]
        if link and g.name == "TARGET_NAME_IF_EXISTS":
            t = g.args or ""
            if "$<" in t or not t:
                raise GenexError(f"{_show(g)} of a non-constant")
            return [Item(and_(cond, defined(t)), t, private)]
        if link and g.name == "LINK_LIBRARY":
            feature, _, libs = (g.args or "").partition(",")
            if feature != "WHOLE_ARCHIVE":
                raise GenexError(f"{_show(g)}: only the WHOLE_ARCHIVE feature is supported")
            items = [
                i for e in _top_split(libs, ",") for i in _eval_elem(e, env, cond, private, link)
            ]
            for i in items:
                i.whole_archive = True
            return items
    out = []
    for c, s in eval_text(elem, env):
        c = and_(cond, c)
        if c != FALSE:
            out += [Item(c, x, private) for x in s.split(";") if x]
    return out


# ---- the parse --------------------------------------------------------------


@dataclass
class TargetInfo:
    name: str
    kind: str
    line: int
    props: list[tuple[str, Arg]] = field(default_factory=list)
    flags: list[tuple[str, int]] = field(default_factory=list)
    base_dirs: list[Arg] = field(default_factory=list)
    errors: list[tuple[str, int]] = field(default_factory=list)
    # configuration (upper case) -> its properties and the file stating them
    configs: dict[str, tuple[list[tuple[str, Arg]], Path]] = field(default_factory=dict)


@dataclass
class ExportFile:
    path: Path
    kind: str  # install | build
    prefix: str | None
    targets: dict[str, TargetInfo]
    missing: list[str]
    config_files: list[Path]
    # The per-import namespace option: own (un-namespaced) target names to
    # their ids, e.g. LLVM's `LLVMSupport` -> `LLVM::LLVMSupport`.
    rename: dict[str, str] = field(default_factory=dict)


class _Cursor:
    def __init__(self, cmds: list[Cmd]):
        self.cmds, self.i = cmds, 0

    def peek(self) -> Cmd | None:
        return self.cmds[self.i] if self.i < len(self.cmds) else None

    def line(self) -> int:
        c = self.peek()
        return c.line if c else (self.cmds[-1].line if self.cmds else 1)

    def take(self, tpl: Template) -> dict | None:
        m = tpl.match(self.cmds, self.i)
        if m is None:
            return None
        self.i, caps = m
        return caps

    def need(self, tpl: Template, what: str) -> dict:
        caps = self.take(tpl)
        if caps is None:
            raise self.unknown(f"expected {what}")
        return caps

    def unknown(self, why: str = "") -> MetadataError:
        c = self.peek()
        shown = f"{c.name}({' '.join(c.texts())[:60]})" if c else "end of file"
        return MetadataError(
            self.line(), f"unknown construct {shown}" + (f"; {why}" if why else "")
        )


def _kv(args: list[Arg], line: int) -> list[tuple[str, Arg]]:
    if len(args) % 2:
        raise MetadataError(line, "set_target_properties with an odd number of arguments")
    return [(args[k].raw, args[k + 1]) for k in range(0, len(args), 2)]


def parse_main(path: Path) -> ExportFile:
    cur = _Cursor(tokenize(path.read_text()))
    cur.need(POLICY_HEADER, "the export file's policy header")
    cur.need(VERSION_SET, "set(CMAKE_IMPORT_FILE_VERSION 1)")
    if cur.take(FIND_DEP_START) is not None:
        while (caps := cur.take(FIND_DEP)) is not None:
            if not caps["ARGS"] or caps["ARGS"][0].raw != caps["PKG"]:
                raise MetadataError(cur.line(), "malformed find_dependency block")
        cur.need(FIND_DEP_END, "unset(_cmake_unwind_arg)")
    expected = [a.raw for a in cur.need(EXPECTED, "the expected-targets block")["TARGETS"]]

    prefix: str | None = None
    kind = "build"
    if (caps := cur.take(PREFIX_ABSOLUTE)) is not None:
        kind, prefix = "install", decode(Arg(caps["PREFIX"], True, cur.line()), None)
    elif cur.take(PREFIX_START) is not None:
        kind, prefix = "install", os.path.dirname(os.path.abspath(path))
        caps = cur.take(PREFIX_USR_MOVE)
        if caps is not None and os.path.realpath(prefix) == os.path.realpath(caps["DEST"]):
            prefix = caps["DEST"]
        ups = 0
        while cur.take(PREFIX_UP) is not None:
            prefix = os.path.dirname(prefix)
            ups += 1
        if ups == 0:
            raise cur.unknown("expected the import prefix computation")
        cur.need(PREFIX_END, "the import prefix root check")
        prefix = "" if prefix == "/" else prefix

    targets: dict[str, TargetInfo] = {}
    while (info := _target_block(cur)) is not None:
        if info.name in targets:
            raise MetadataError(info.line, f"target {info.name} created twice")
        targets[info.name] = info
    if (nxt := cur.peek()) is not None and nxt.name in ("add_library", "add_executable"):
        raise cur.unknown()
    if sorted(targets) != sorted(expected):
        raise MetadataError(cur.line(), "the created targets differ from the expected-targets list")
    cur.take(CONSUMER_VERSION)

    config_files: list[Path] = []
    if kind == "install":
        if (caps := cur.take(LOAD_CONFIGS)) is not None:
            config_files = _config_glob(path, caps["GLOB"], cur.line())
    else:
        while _config_block(cur, targets, path):
            pass
    if cur.take(CXX_MODULES) is not None:
        raise MetadataError(cur.line(), "C++ module export information is not supported")
    if kind == "install":
        cur.need(CLEANUP, "set(_IMPORT_PREFIX)")
        if cur.take(CHECK_LOOP) is None:
            cur.need(CHECK_LOOP_324, "the imported-file check loop")
    missing: list[str] = []
    if (caps := cur.take(MISSING)) is not None:
        missing = [decode(a, None) for a in caps["TARGETS"]]
    cur.need(VERSION_UNSET, "set(CMAKE_IMPORT_FILE_VERSION)")
    cur.need(POLICY_POP, "cmake_policy(POP)")
    if cur.peek() is not None:
        raise cur.unknown()
    return ExportFile(path, kind, prefix, targets, missing, config_files)


def _target_block(cur: _Cursor) -> TargetInfo | None:
    line = cur.line()
    if (caps := cur.take(ADD_LIBRARY)) is not None:
        k = caps["KIND"]
        if k == "OBJECT":
            info = TargetInfo(
                caps["T"], "library", line, errors=[("object libraries are not supported", line)]
            )
        elif k in LIB_KINDS:
            info = TargetInfo(caps["T"], LIB_KINDS[k], line)
        else:
            raise MetadataError(line, f"unknown construct add_library({caps['T']} {k} IMPORTED)")
    elif (caps := cur.take(ADD_EXECUTABLE)) is not None:
        info = TargetInfo(caps["T"], "executable", line)
    elif (caps := cur.take(ADD_SYMBOLIC)) is not None:
        info = TargetInfo(caps["T"], "interface", line)
    else:
        return None
    t = info.name
    while True:
        fline = cur.line()
        caps = cur.take(FLAG)
        if caps is None:
            break
        if caps["T"] != t:
            raise MetadataError(fline, f"property of {caps['T']} in the block of {t}")
        info.flags.append((caps["P"], fline))
    pline = cur.line()
    if (caps := cur.take(PROPS)) is not None:
        if caps["T"] != t:
            raise MetadataError(pline, f"properties of {caps['T']} in the block of {t}")
        info.props = _kv(caps["KV"], pline)
    while _file_set(cur, info):
        pass
    return info


def _file_set(cur: _Cursor, info: TargetInfo) -> bool:
    start, line = cur.i, cur.line()
    guard = cur.take(FILE_SET_IF)
    caps = cur.take(TARGET_SOURCES)
    if caps is None:
        cur.i = start
        return False
    if caps["T"] != info.name:
        raise MetadataError(line, f"target_sources of {caps['T']} in the block of {info.name}")
    groups: list[list[Arg]] = []
    for a in caps["ARGS"]:
        if a.raw == "INTERFACE" and not a.quoted:
            groups.append([])
        elif not groups:
            raise MetadataError(a.line, "unknown construct: target_sources without INTERFACE")
        else:
            groups[-1].append(a)
    types = set()
    for g in groups:
        sec: dict[str, list[Arg]] = {}
        key = None
        for a in g:
            if not a.quoted and a.raw in ("FILE_SET", "TYPE", "BASE_DIRS", "FILES"):
                key = a.raw
                sec[key] = []
            elif key is None:
                raise MetadataError(a.line, f"unknown construct in target_sources: {a.raw}")
            else:
                sec[key].append(a)
        if set(sec) != {"FILE_SET", "TYPE", "BASE_DIRS", "FILES"} or len(sec["TYPE"]) != 1:
            raise MetadataError(line, "unknown construct: malformed FILE_SET")
        ftype = sec["TYPE"][0].raw
        types.add(ftype)
        if ftype == "HEADERS":
            info.base_dirs += sec["BASE_DIRS"]
        else:
            info.errors.append((f"FILE_SET of type {ftype} is not supported", line))
    while True:
        fline = cur.line()
        if cur.take(FILE_SET_PROP) is None:
            break
        info.errors.append(("file set properties are not supported", fline))
    if guard is not None:
        cur.need(ELSE, "else() of the FILE_SET guard")
        if (fs := cur.take(FILE_SET_ELSE_HEADERS)) is not None:
            if types != {"HEADERS"} or fs["T"] != info.name:
                raise MetadataError(cur.line(), "unknown construct: FILE_SET fallback")
        elif cur.take(FILE_SET_ELSE_MESSAGE) is None:
            raise cur.unknown("expected the FILE_SET fallback")
        cur.need(ENDIF, "endif() of the FILE_SET guard")
    return True


def _config_block(cur: _Cursor, targets: dict[str, TargetInfo], path: Path) -> bool:
    line = cur.line()
    caps = cur.take(CFG_START)
    if caps is None:
        return False
    t, cfg = caps["T"], caps["C"]
    if t not in targets:
        raise MetadataError(line, f"configuration of unknown target {t}")
    pline = cur.line()
    props = cur.need(PROPS, f"set_target_properties({t} PROPERTIES ...)")
    if props["T"] != t:
        raise MetadataError(pline, f"properties of {props['T']} in the block of {t}")
    if cur.take(CFG_XCFRAMEWORK) is not None:
        raise MetadataError(line, "xcframework locations are not supported")
    # One of the two check lines is a comment in CMake's output (the targets
    # line for libraries, the files line for executables), which the reader skips.
    if cur.take(CFG_CHECK_TARGETS) is not None and cur.take(CFG_CHECK_XCFRAMEWORK) is not None:
        raise MetadataError(line, "xcframework locations are not supported")
    cur.take(CFG_CHECK_FILES)
    info = targets[t]
    if cfg in info.configs:
        raise MetadataError(line, f"configuration {cfg} of {t} given twice")
    info.configs[cfg] = (_kv(props["KV"], pline), path)
    return True


def _config_glob(path: Path, glob: str, line: int) -> list[Path]:
    if "/" in glob or not glob.endswith(".cmake"):
        raise MetadataError(line, f"unknown construct: configuration glob {glob!r}")
    out = []
    for p in sorted(path.parent.iterdir()):
        if (
            p != path
            and fnmatch.fnmatchcase(p.name, glob)
            and BASIC_VERSION_FILE not in p.read_text()[:400]
        ):
            out.append(p)
    return out


def parse_config(path: Path, targets: dict[str, TargetInfo]) -> None:
    text = path.read_text()
    if CONFIG_MARK not in text[:300]:
        raise MetadataError(
            1, "not a per-configuration export file, but the export's glob includes it"
        )
    cur = _Cursor(tokenize(text))
    cur.need(VERSION_SET, "set(CMAKE_IMPORT_FILE_VERSION 1)")
    while _config_block(cur, targets, path):
        pass
    cur.need(VERSION_UNSET, "set(CMAKE_IMPORT_FILE_VERSION)")
    if cur.peek() is not None:
        raise cur.unknown()


# ---- facts ------------------------------------------------------------------

# Properties without build information for buildlog.
IGNORED = {
    "INTERFACE_POSITION_INDEPENDENT_CODE",  # every macOS object is PIC
    "INTERFACE_AUTOUIC_OPTIONS",
    "INTERFACE_AUTOMOC_MACRO_NAMES",
    "SPDX_LICENSE",
    "SYMBOLIC",
}
CONFIG_IGNORED = {
    "IMPORTED_LINK_INTERFACE_LANGUAGES",  # buildlog links with the C++ driver
    "IMPORTED_LINK_DEPENDENT_LIBRARIES",  # runtime deps of shared libraries
    "IMPORTED_LINK_INTERFACE_MULTIPLICITY",
    "IMPORTED_NO_SONAME",
    "IMPORTED_COMMON_LANGUAGE_RUNTIME",
}
CONFIG_REJECTED = {
    "IMPORTED_IMPLIB": "import libraries",
    "IMPORTED_OBJECTS": "object libraries",
    "IMPORTED_LINK_INTERFACE_LIBRARIES": "old-style link interfaces",
    "IMPORTED_LIBNAME": "IMPORTED_LIBNAME",
}
# FRAMEWORK: the location is the binary inside the bundle, linked by path;
# its include dirs name the bundle (see _props).
FLAGS_IGNORED = {"ENABLE_EXPORTS", "DEPRECATION", "IMPORTED_NO_SYSTEM", "SYSTEM", "FRAMEWORK"}
FLAGS_REJECTED = {"MACOSX_BUNDLE", "BUNDLE", "AIX_SHARED_LIBRARY_ARCHIVE"}
PROP_KEYS = {
    "INTERFACE_INCLUDE_DIRECTORIES": "cc.include_dir",
    "INTERFACE_SYSTEM_INCLUDE_DIRECTORIES": "cc.system_include_dir",
    "INTERFACE_COMPILE_DEFINITIONS": "cc.define",
    "INTERFACE_COMPILE_OPTIONS": "cc.compile_flag",
    "INTERFACE_LINK_OPTIONS": "native.link_flag",
}
REJECTED = {
    "INTERFACE_SOURCES": "INTERFACE_SOURCES (sources compiled into consumers)",
    "INTERFACE_PRECOMPILE_HEADERS": "INTERFACE_PRECOMPILE_HEADERS",
    "INTERFACE_LINK_DEPENDS": "INTERFACE_LINK_DEPENDS",
}
CXX_STDS = {"11", "14", "17", "20", "23", "26"}
INCLUDE_KEYS = {"cc.include_dir", "cc.system_include_dir"}


def import_file(res: Result, results=None, namespace: str = "") -> None:
    ex = parse_main(res.file)
    for cf in ex.config_files:
        res.current = cf
        parse_config(cf, ex.targets)
    res.current = res.file
    if namespace:
        _apply_namespace(res, ex, namespace)
    else:
        _package_names(res, ex)
        _package_identity(res, ex)
    env = Env(ex.kind, os_domain(results or {}))
    for info in ex.targets.values():
        _emit(res, ex, env, info)
    res.current = res.file


def _package_names(res: Result, ex: ExportFile) -> None:
    """A namespaced target `P::comp` is also the package component `P:comp`,
    and `P::P` is also the bare package name `P`: the names a downstream
    writes for a package's components whichever route binds it (built
    in-tree, CPS, CMake). With the per-import namespace option the
    namespace is the user's, not the package's, so no names are stated."""
    for name, info in ex.targets.items():
        ns, _, comp = name.partition("::")
        if not ns or not _BARE.fullmatch(ns) or not _BARE.fullmatch(comp):
            continue
        res.alias(f"{ns}:{comp}", name, info.line)
        if comp == ns:
            res.alias(ns, name, info.line)


_CONFIG_FILE = re.compile(r"(.+?)(?:Config|-config)\.cmake")
_VERSION_FILE = "{}ConfigVersion.cmake", "{}-config-version.cmake"
_PACKAGE_VERSION = re.compile(r'^\s*set\(\s*PACKAGE_VERSION\s+"([^"$]+)"\s*\)', re.MULTILINE)


def _package_identity(res: Result, ex: ExportFile) -> None:
    """The package the targets belong to, by its own name: the one
    `<Pkg>Config.cmake` (or `<pkg>-config.cmake`) beside the export file,
    which is what find_package(<Pkg>) finds; its version is the
    `set(PACKAGE_VERSION "...")` of the version file beside it. Neither file
    is evaluated, only matched; with no single config file, or no plain
    version, nothing is stated (an export requiring the target then names
    the package itself: export_requires_name)."""
    d = res.file.parent
    names = {m.group(1) for f in d.glob("*.cmake") if (m := _CONFIG_FILE.fullmatch(f.name))}
    if len(names) != 1:
        return
    pkg = names.pop()
    version = None
    for pattern in _VERSION_FILE:
        vf = d / pattern.format(pkg)
        if vf.is_file():
            text = vf.read_text(errors="replace")
            m = _PACKAGE_VERSION.search(text)
            if m:
                version = (m.group(1), text[: m.start()].count("\n") + 1, vf)
            break
    for name, info in ex.targets.items():
        _, sep, comp = name.partition("::")
        if not sep:
            continue
        res.package(name, pkg, comp, info.line)
        if version:
            res.current = version[2]
            res.version_string(name, version[0], version[1])
            res.current = res.file


def _apply_namespace(res: Result, ex: ExportFile, ns: str) -> None:
    """The per-import namespace option, for export files whose targets are
    un-namespaced (LLVM's `LLVMSupport`, lld's `lldWasm`): every target the
    file defines becomes `<ns>::<name>`, and so do the file's references to
    its own targets. A file whose targets are all `<ns>::...` already is left as is. A reference to a name the file checks for but does not
    define (its missing-targets list, e.g. Clang's `LLVMSupport`) stays a
    bare name: a `res_pkg_bare_dep` that L3_import resolves against the
    names other namespaced imports export (`res_pkg_export`), never by
    suffix search."""
    if "::" in ns or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]*", ns):
        raise MetadataError(None, f"invalid import namespace {ns!r}")
    for name, info in ex.targets.items():
        if "::" in name:
            if all(n.partition("::")[0] == ns and "::" in n for n in ex.targets):
                return  # already in the namespace (Qt6::Core under `Qt6`): it states what the manifest owns
            raise MetadataError(
                info.line,
                f"target {name} is already namespaced; the namespace option applies "
                "to export files of un-namespaced targets (or of this namespace)",
            )
    ex.rename = {name: f"{ns}::{name}" for name in ex.targets}
    targets = {}
    for name, info in ex.targets.items():
        info.name = ex.rename[name]
        targets[info.name] = info
        res.export(name, info.name, info.line)
    ex.targets = targets


def _guard(res: Result, t: str, line: int, fn) -> None:
    try:
        fn()
    except GenexError as exc:
        res.target_error(t, str(exc), line)


def _emit(res: Result, ex: ExportFile, env: Env, info: TargetInfo) -> None:
    t = info.name
    res.current = ex.path
    res.target(t, info.kind, info.line)
    for msg, line in info.errors:
        res.target_error(t, msg, line)
    for flag, line in info.flags:
        if flag in FLAGS_REJECTED:
            res.target_error(t, f"{flag} targets are not supported", line)
        elif flag not in FLAGS_IGNORED:
            raise MetadataError(line, f"unknown construct set_property(TARGET {t} PROPERTY {flag})")
    for d in info.base_dirs:
        _props(res, ex, env, t, "cc.include_dir", d)
    for prop, arg in info.props:
        _interface_prop(res, ex, env, info, prop, arg)
    _configs(res, ex, info)


def _props(res, ex: ExportFile, env: Env, t: str, key: str, arg: Arg) -> None:
    value = decode(arg, ex.prefix)

    def run():
        for i in eval_list(value, env):
            text = os.path.normpath(i.text) if key in INCLUDE_KEYS else i.text
            if key in INCLUDE_KEYS and text.endswith(".framework"):
                # How CMake passes a framework include dir: its parent as a
                # framework search dir (<Name/header.h>); imported, so system.
                text = f"-iframework{os.path.dirname(text)}"
                res.prop(t, "cc.compile_flag", text, i.cond, arg.line)
                continue
            res.prop(t, key, text, i.cond, arg.line)

    _guard(res, t, arg.line, run)


def _interface_prop(res, ex: ExportFile, env: Env, info: TargetInfo, prop: str, arg: Arg) -> None:
    t, line = info.name, arg.line
    if prop in PROP_KEYS:
        _props(res, ex, env, t, PROP_KEYS[prop], arg)
    elif prop == "INTERFACE_LINK_DIRECTORIES":
        value = decode(arg, ex.prefix)

        def run():
            for i in eval_list(value, env):
                res.prop(t, "native.link_flag", f"-L{i.text}", i.cond, line)

        _guard(res, t, line, run)
    elif prop == "INTERFACE_COMPILE_FEATURES":
        value = decode(arg, ex.prefix)

        def run():
            for i in eval_list(value, env):
                std = i.text.removeprefix("cxx_std_")
                if i.text.startswith("cxx_std_") and std in CXX_STDS:
                    res.prop(t, "cxx.std", std, i.cond, line)
                else:
                    res.target_error(t, f"compile feature {i.text} has no buildlog mapping", line)

        _guard(res, t, line, run)
    elif prop == "INTERFACE_LINK_LIBRARIES":
        value = decode(arg, ex.prefix)
        _guard(
            res, t, line, lambda: _link_items(res, ex, t, eval_list(value, env, link=True), line)
        )
    elif prop in IGNORED or prop.startswith("COMPATIBLE_INTERFACE_"):
        pass
    elif not prop.startswith("INTERFACE_") or prop.removeprefix("INTERFACE_") in _compatible(info):
        # EXPORT_PROPERTIES are read only by explicit get_property(); an
        # INTERFACE_ property named by COMPATIBLE_INTERFACE_* is a
        # consistency check between a consumer's dependencies.
        pass
    elif prop == "INTERFACE_SOURCES" and _data_files(decode(arg, ex.prefix)):
        pass  # metatypes lists for a consumer's tooling (Qt): nothing is compiled
    elif prop in REJECTED:
        res.target_error(t, f"{REJECTED[prop]} is not supported", line)
    elif "CXX_MODULE" in prop:
        res.target_error(t, "C++ modules are not supported", line)
    else:
        res.target_error(t, f"property {prop} is not supported", line)


def _data_files(text: str) -> bool:
    """INTERFACE_SOURCES that name only .json files."""
    return set(re.findall(r"\.(\w+)(?=[;>]|$)", text)) == {"json"}


def _compatible(info: TargetInfo) -> set[str]:
    names: set[str] = set()
    for prop, arg in info.props:
        if prop.startswith("COMPATIBLE_INTERFACE_"):
            names |= set(decode(arg, None).split(";"))
    return names


def _link_items(res: Result, ex: ExportFile, t: str, items: list[Item], line: int) -> None:
    known = set(ex.targets) | set(ex.missing)
    for i in items:
        vis = "private" if i.private else "public"
        x = i.text
        if x in ex.rename:
            x = ex.rename[x]
        elif ex.rename and x in ex.missing and "::" not in x:
            if i.whole_archive:
                res.target_error(
                    t, f"$<LINK_LIBRARY:WHOLE_ARCHIVE,{x}> of a foreign bare name", line
                )
            else:
                res.bare_dep(t, x, vis, i.cond, line)
            continue
        if "::" in x or x in known:
            res.dep(t, x, vis, i.cond, line)
            if i.whole_archive:
                res.whole_archive(t, x, i.cond, line)
        elif i.whole_archive:
            res.target_error(t, f"$<LINK_LIBRARY:WHOLE_ARCHIVE,{x}> of a non-target", line)
        elif x.startswith("-"):
            res.prop(t, "native.link_flag", x, i.cond, line)
        elif os.path.isabs(x) and x.endswith(".framework"):
            # How CMake links a framework path: its directory and its name.
            res.prop(t, "native.link_flag", f"-F{os.path.dirname(x)}", i.cond, line)
            res.prop(
                t,
                "native.link_flag",
                f"-framework {os.path.basename(x)[: -len('.framework')]}",
                i.cond,
                line,
            )
        elif os.path.isabs(x):
            res.link(t, os.path.normpath(x), i.cond, line)
        elif os.path.isabs(x) or "/" in x or "$" in x:
            res.target_error(t, f"link item {x!r} is not supported", line)
        else:
            res.prop(t, "native.link_flag", f"-l{x}", i.cond, line)


_ELF_SHARED = re.compile(r"\.so(\.\d+)*$")
# The directories gcc and clang search by themselves, which CMake leaves out of an rpath.
_SYSTEM_LIBDIR = re.compile(r"^(/usr(/local)?)?/lib(64)?(/[a-z0-9_]+-linux-[a-z0-9]+)?$")


def _configs(res: Result, ex: ExportFile, info: TargetInfo) -> None:
    """Per-configuration properties. A NOCONFIG block (no build type) applies
    where none of the target's mapped configurations does, and so does a
    RelWithDebInfo or MinSizeRel block (apt.llvm.org's LLVM has only that)."""
    t = info.name
    mapped = [
        package_config_cond(c) for c in info.configs if c != "NOCONFIG" and is_mapped_config(c)
    ]
    for cfg, (props, path) in info.configs.items():
        res.current = path
        if cfg == "NOCONFIG" or cfg.lower() in PACKAGE_ONLY_CONFIGS:
            cond = not_(or_(*mapped))
        else:
            cond = package_config_cond(cfg)
        suffix = f"_{cfg}"
        values = {}
        for prop, arg in props:
            if not prop.endswith(suffix):
                raise MetadataError(
                    arg.line, f"property {prop} in the block of configuration {cfg}"
                )
            values[prop[: -len(suffix)]] = arg
        for base, arg in values.items():
            if base == "IMPORTED_LOCATION":
                res.location(t, os.path.normpath(decode(arg, ex.prefix)), cond, arg.line)
            elif base == "IMPORTED_SONAME":
                # An @rpath install name needs the library's directory on the
                # consumer's runtime search path.
                loc = values.get("IMPORTED_LOCATION")
                name = decode(arg, ex.prefix)
                if name.startswith("@rpath/") and loc is not None:
                    path = os.path.normpath(decode(loc, ex.prefix))
                    # The install name is relative to that directory
                    # (@rpath/QtCore.framework/Versions/A/QtCore: a framework).
                    tail = "/" + os.path.normpath(name.removeprefix("@rpath/"))
                    where = (
                        path.removesuffix(tail) if path.endswith(tail) else os.path.dirname(path)
                    )
                    res.prop(t, "native.link_flag", f"-Wl,-rpath,{where}", cond, arg.line)
                elif loc is not None and _ELF_SHARED.search(decode(loc, ex.prefix)):
                    # An ELF shared library outside the compiler's own directories is
                    # found at run time through the consumer's rpath (CMake's build RPATH).
                    where = os.path.dirname(os.path.normpath(decode(loc, ex.prefix)))
                    res.rpath(t, where, _SYSTEM_LIBDIR.pattern, cond, arg.line)
            elif base in CONFIG_IGNORED:
                pass
            elif base in CONFIG_REJECTED:
                res.target_error(t, f"{CONFIG_REJECTED[base]} ({base}) are not supported", arg.line)
            else:
                res.target_error(t, f"property {base}{suffix} is not supported", arg.line)
    res.current = ex.path


# ---- search -----------------------------------------------------------------

_DEFINES = re.compile(r"^\s*add_(?:library|executable)\(\s*([^\s)]+)", re.MULTILINE)


_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*")


def _cmake_target(name: str) -> str | None:
    """The CMake target a wanted name denotes: `Ns::name` itself; a package
     component `P:comp` is `P::comp`; a bare package name `P` is `P::P`
    ."""
    if "/" in name:
        return None
    if "::" in name:
        return name if name.split("::", 1)[0] else None
    if ":" in name:
        pkg, comp = name.split(":", 1)
        ok = _BARE.fullmatch(pkg) and _BARE.fullmatch(comp)
        return f"{pkg}::{comp}" if ok else None
    return f"{name}::{name}" if _BARE.fullmatch(name) else None


def _namespace(name: str) -> str | None:
    target = _cmake_target(name)
    return target.split("::", 1)[0] if target else None


def _package_dirs(ns: str, directory: Path) -> list[Path]:
    """CMake's `<prefix>/lib/cmake/<name>*/` rule, with the namespace as the
    name, plus one subdirectory level."""
    try:
        tops = sorted(
            p for p in directory.iterdir() if p.is_dir() and p.name.lower().startswith(ns.lower())
        )
    except OSError:
        return []
    out = []
    for d in tops:
        out.append(d)
        out += sorted(p for p in d.iterdir() if p.is_dir())
    return out


def _main_text(path: Path) -> str | None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return text if MAIN_MARK in text[:1500] else None


def by_alias(name: str) -> bool:
    """`P:comp` and `P` are aliases of `P::comp` and `P::P`."""
    return "::" not in name


def find(name: str, directory: Path) -> list[Path]:
    ns = _namespace(name)
    if ns is None:
        return []
    target = _cmake_target(name)
    out = []
    for d in _package_dirs(ns, directory):
        for f in sorted(d.glob("*.cmake")):
            text = _main_text(f)
            if text is not None and target in _DEFINES.findall(text):
                out.append(f)
    return out


def offers(file: Path, directory: Path, names, observations) -> set[str]:
    try:
        relative = file.relative_to(directory)
    except ValueError:
        return set()
    if len(relative.parts) not in (2, 3):
        return set()
    content = observations.file(file)
    if content is None:
        return set()
    text = content.decode(errors="replace")
    if MAIN_MARK not in text[:1500]:
        return set()
    found = set()
    for target in _DEFINES.findall(text):
        namespace = _namespace(target)
        if (
            namespace is None
            or _cmake_target(target) != target
            or not relative.parts[0].lower().startswith(namespace.lower())
        ):
            continue
        found.add(target)
        alias = target.replace("::", ":", 1)
        if _cmake_target(alias) == target:
            found.add(alias)
        if target == f"{namespace}::{namespace}":
            found.add(namespace)
    return found


def enumerate_files(directory: Path, observations) -> list[Path]:
    # Match _package_dirs: package directories, plus one subdirectory level.
    tops = [p for p in observations.directory(directory) if p.is_dir()]
    directories = tops + [p for d in tops for p in observations.directory(d) if p.is_dir()]
    files = []
    for d in directories:
        for path in observations.directory(d):
            if path.suffix != ".cmake":
                continue
            content = observations.file(path)
            if content is not None and MAIN_MARK in content.decode(errors="replace")[:1500]:
                files.append(path)
    return sorted(set(files))


def metadata_links(file: Path, observations):
    from buildlog.providers import MetadataLinks

    observations.file(file)
    for path in observations.directory(file.parent):
        if path.suffix == ".cmake":
            observations.file(path)
    return MetadataLinks()


def find_watched(name: str, directory: Path) -> list:
    ns = _namespace(name)
    if ns is None:
        return []
    return [
        [str(d), [stat_token(f) for f in sorted(d.glob("*.cmake"))]]
        for d in _package_dirs(ns, directory)
    ]


def watched(path: Path) -> list:
    return [stat_token(path), [stat_token(p) for p in sorted(path.parent.glob("*.cmake"))]]
