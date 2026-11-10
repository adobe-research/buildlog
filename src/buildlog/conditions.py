# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Infix condition syntax: the loader's only condition sugar.

A string in a `Cond` column, or an inline expression in a condition-name
column, parses to core's `Cond` ADT (types.dl), written as Soufflé fact text:

    WITH_OPTIM & WITH_ARMV8 & (cc.check.HAVE_A | cc.check.HAVE_B)
    !ENABLE_RTTI
    native.opt == debug & platform.os != macos
    defined(ZLIB::ZLIB)

- a bare name is a named condition (`$Named(n)`); options and configure
  checks name their own (core L1_options.dl, buildlog-cpp L4_checks.dl);
- `k == v` (or `k = v`) tests a configuration attribute (`$Eq(k, v)`) and
  `k != v` its complement;
- `!`, `&` and `|` are n-ary sugar for `$Not`, `$And` and `$Or`, with the
  usual precedence (`!` over `&` over `|`) and parentheses;
- `defined(n)` is `$Defined(n)`;
- `k >= v` and `k > v` compare a version-valued attribute (`cxx.version`,
  `package.<name>.version`) with a version: `$Ge(k, v)` and `$Gt(k, v)`;
  `k < v` and `k <= v` are their negations.

A string that starts with `$` is already ADT text and is passed through.
"""

from __future__ import annotations

import re

from .errors import SpecError

_TOKEN = re.compile(r"\s*(?:(==|!=|>=|<=|[=!&|()<>])|([^\s=!&|()<>$,\"\\]+))")
# A condition name as written in a name column: anything without an
# operator, parenthesis, blank or `$` (names may hold dots and `=`, like
# buildlog-cpp's `cc.check.X=yes`).
_NAME = re.compile(r"[^\s!&|()<>$,\"\\]+\Z")


def is_name(text: str) -> bool:
    """True when a condition-name column holds a name, not an inline expression."""
    return bool(_NAME.match(text))


def _tokens(text: str, origin: str) -> list[str]:
    out: list[str] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise SpecError(f"{origin}: condition {text!r}: unexpected {text[pos:].strip()[:10]!r}")
        out.append(m.group(1) or m.group(2))
        pos = m.end()
    return out


def parse(text: str, origin: str) -> str:
    """The `Cond` ADT fact text of an infix condition (or ADT text unchanged)."""
    if text.lstrip().startswith("$"):
        return text
    toks = _tokens(text, origin)
    pos = 0

    def fail(msg: str) -> SpecError:
        return SpecError(f"{origin}: condition {text!r}: {msg}")

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(toks):
            raise fail("unexpected end")
        pos += 1
        return toks[pos - 1]

    def word(what: str) -> str:
        tok = take()
        if tok in {"==", "!=", "=", "!", "&", "|", "(", ")", ">=", "<=", ">", "<"}:
            raise fail(f"expected {what}, found {tok!r}")
        return tok

    def chain(ctor: str, parts: list[str]) -> str:
        out = parts[-1]
        for part in reversed(parts[:-1]):
            out = f"${ctor}({part}, {out})"
        return out

    def disj() -> str:
        parts = [conj()]
        while peek() == "|":
            take()
            parts.append(conj())
        return chain("Or", parts)

    def conj() -> str:
        parts = [unary()]
        while peek() == "&":
            take()
            parts.append(unary())
        return chain("And", parts)

    def unary() -> str:
        if peek() == "!":
            take()
            return f"$Not({unary()})"
        if peek() == "(":
            take()
            inner = disj()
            if take() != ")":
                raise fail("expected ')'")
            return inner
        name = word("a name")
        if name == "defined" and peek() == "(":
            take()
            target = word("a target name")
            if take() != ")":
                raise fail("expected ')'")
            return f"$Defined({target})"
        if peek() in {"==", "=", "!="}:
            op = take()
            value = word("a value")
            eq = f"$Eq({name}, {value})"
            return f"$Not({eq})" if op == "!=" else eq
        if peek() in {">=", ">", "<=", "<"}:
            op = take()
            value = word("a version")
            ge, gt = f"$Ge({name}, {value})", f"$Gt({name}, {value})"
            return {">=": ge, ">": gt, "<": f"$Not({ge})", "<=": f"$Not({gt})"}[op]
        return f"$Named({name})"

    if not toks:
        raise fail("empty")
    out = disj()
    if pos != len(toks):
        raise fail(f"unexpected {toks[pos]!r}")
    return out
