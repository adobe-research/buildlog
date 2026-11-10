# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `buildlog` export format: a package as Datalog.

An installed package is facts about its location and usage requirements,
and a built one entails the same facts, so the export states them in
buildlog's own vocabulary (`pkg_*`, below), as ground Soufflé facts: the
file `<prefix>/share/buildlog/<pkg>/<pkg>.facts.dl`. It is a Datalog
program fragment: with the declarations of `souffle_header()` Soufflé reads
it, and `parse_facts` reads it without Soufflé. Nothing is spelled for the
format, so nothing is lossy.

Paths: `pkg_prop_path`, `pkg_location` are relative to the install prefix,
and `pkg_prefix` says how to find the prefix from the file's directory, so
a copied or moved install is still valid. `pkg_prop_abs` is an absolute
path outside the package (not relocatable).

Beside the facts the install writes `manifest.toml` (the rule package
manifest, which imports the facts file) and the layers of the project's own
rule package (`export_rules`), if it declares one.
"""

from __future__ import annotations

import re

FACTS_SUFFIX = ".facts.dl"

# relation -> column names (all symbols). The facts file states these.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "pkg_package": ("pkg",),
    "pkg_prefix": ("rel",),  # from the facts file's directory to the install prefix
    "pkg_version": ("pkg", "version"),
    "pkg_compat": ("pkg", "policy"),  # metadata: not a usage requirement
    "pkg_description": ("pkg", "text"),  # metadata
    "pkg_requires_version": ("pkg", "minimum"),  # metadata: a minimum for a package it requires
    "pkg_component": ("comp", "kind"),  # interface | archive | dylib | module
    "pkg_default": ("comp",),  # in the package's default components
    "pkg_location": ("comp", "rel"),
    "pkg_link_name": ("comp", "name"),
    "pkg_soname": ("comp", "name"),
    "pkg_requires": ("comp", "other"),  # another component of this package
    "pkg_depends": ("comp", "pkg", "other"),  # component `other` of package pkg ("": the package)
    "pkg_runtime_dep": (
        "comp",
        "module",
        "place",
    ),  # comp dlopens module (component of this package)
    "pkg_prop": ("comp", "key", "value"),
    "pkg_prop_path": ("comp", "key", "rel"),
    "pkg_prop_abs": ("comp", "key", "path"),
}
KINDS = ("interface", "archive", "dylib", "module")


class FactsError(Exception):
    def __init__(self, line: int | None, message: str):
        super().__init__(message)
        self.line = line
        self.message = message


def souffle_header() -> str:
    """The declarations the facts file is read against (Soufflé's `.decl`)."""
    lines = []
    for rel, cols in VOCABULARY.items():
        lines.append(f".decl {rel}({', '.join(c + ':symbol' for c in cols)})")
    return "\n".join(lines) + "\n"


def quote(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def fact(rel: str, *values: str) -> str:
    assert len(values) == len(VOCABULARY[rel]), rel
    return f"{rel}({', '.join(quote(v) for v in values)})."


_STMT = re.compile(r"\s*([A-Za-z_][\w]*)\s*\((.*)\)\s*\.\s*(?://.*)?$")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


def _args(text: str, line: int) -> list[str]:
    out: list[str] = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n or text[i] != '"':
            raise FactsError(line, "a fact's arguments are strings")
        i += 1
        cur: list[str] = []
        while i < n and text[i] != '"':
            if text[i] == "\\":
                i += 1
                if i >= n or text[i] not in _ESCAPES:
                    raise FactsError(line, "unknown escape in a string")
                cur.append(_ESCAPES[text[i]])
            else:
                cur.append(text[i])
            i += 1
        if i >= n:
            raise FactsError(line, "unterminated string")
        i += 1
        out.append("".join(cur))
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return out
        if text[i] != ",":
            raise FactsError(line, "expected , between arguments")
        i += 1


def parse_facts(text: str) -> list[tuple[str, tuple[str, ...], int]]:
    """(relation, row, line) of every fact; the file has facts, comments and
    blank lines only, over the relations of VOCABULARY."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        m = _STMT.match(line)
        if not m:
            raise FactsError(no, 'not a ground fact `relation("…", …).`')
        rel = m.group(1)
        if rel not in VOCABULARY:
            raise FactsError(no, f"unknown relation {rel}")
        row = tuple(_args(m.group(2), no))
        if len(row) != len(VOCABULARY[rel]):
            raise FactsError(
                no, f"{rel} takes {len(VOCABULARY[rel])} arguments ({', '.join(VOCABULARY[rel])})"
            )
        out.append((rel, row, no))
    return out


# ---- manifest.toml ----------------------------------------------------------


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return quote(v)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return "[" + ", ".join(quote(x) for x in v) + "]"
    if isinstance(v, list) and all(isinstance(x, dict) for x in v):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        return "{ " + ", ".join(f"{k} = {quote(x)}" for k, x in v.items()) + " }"
    raise ValueError(f"cannot write {v!r} in a manifest")


def write_manifest(data: dict) -> str:
    """The manifest text of `data` (scalars, string lists, string tables)."""
    return "".join(f"{k} = {_toml_value(v)}\n" for k, v in data.items())


def installed_manifest(name: str, version: str, base: dict) -> dict:
    """The installed package's manifest: the project's own rule package
    manifest (`base`, possibly empty) with name and version, and the facts
    file imported (so the content hash covers it)."""
    for key in ("probe", "provider"):
        if key in base:
            raise FactsError(None, f"an exported rule package may not declare [[{key}]]")
    data: dict = {"name": name, "version": version}
    data.update({k: v for k, v in base.items() if k not in ("name", "version", "imports")})
    data["imports"] = [
        {"format": "buildlog", "file": f"{name}{FACTS_SUFFIX}"},
        *base.get("imports", []),
    ]
    return data
