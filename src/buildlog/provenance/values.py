# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Soufflé values: parse the text the engine prints, render them back.

Two surface syntaxes occur. Explain proofs (and query literals) quote
symbols: `"x86_64-macos"`, `["a", nil]`, `$Eq("os", "linux")`. Fact and CSV
files leave symbols bare inside records and ADTs: `[a, nil]`,
`$Eq(os, linux)`. `parse_value` reads both; `explain_text` and `facts_text`
write them.

Python representation: symbols are `str`, numbers `int` or `float`, `NIL` is
the empty record, and `Record` and `Adt` are the structured values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ValueSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Nil:
    def __repr__(self) -> str:
        return "nil"


NIL = Nil()


@dataclass(frozen=True)
class Record:
    fields: tuple


@dataclass(frozen=True)
class Adt:
    branch: str
    args: tuple = ()


NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
IDENT = re.compile(r"[A-Za-z_][\w.]*")


class _Reader:
    def __init__(self, text: str, bare: bool):
        self.text = text
        self.pos = 0
        self.bare = bare

    def error(self, message: str) -> ValueSyntaxError:
        return ValueSyntaxError(f"{message} at offset {self.pos} in {self.text!r}")

    def ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\n":
            self.pos += 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def items(self, close: str) -> list:
        out = []
        self.ws()
        if self.peek() == close:
            self.pos += 1
            return out
        while True:
            out.append(self.value())
            self.ws()
            ch = self.peek()
            if ch == close:
                self.pos += 1
                return out
            if ch != ",":
                raise self.error(f"expected ',' or {close!r}")
            self.pos += 1

    def string(self) -> str:
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "\\" and self.pos + 1 < len(self.text):
                out.append(self.text[self.pos + 1])
                self.pos += 2
                continue
            if ch == '"':
                self.pos += 1
                return "".join(out)
            out.append(ch)
            self.pos += 1
        raise self.error("unterminated string")

    def bare_symbol(self) -> str:
        start = self.pos
        depth = 0
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            self.pos += 1
        return self.text[start : self.pos].strip()

    def _ends_value(self, pos: int) -> bool:
        rest = self.text[pos:].lstrip(" \t")
        return not rest or rest[0] in ",)]" or not self.bare

    def value(self):
        self.ws()
        ch = self.peek()
        if ch == '"':
            return self.string()
        if ch == "[":
            self.pos += 1
            return Record(tuple(self.items("]")))
        if ch == "$":
            self.pos += 1
            m = IDENT.match(self.text, self.pos)
            if not m:
                raise self.error("expected an ADT branch name")
            self.pos = m.end()
            if self.peek() == "(":
                self.pos += 1
                return Adt(m.group(0), tuple(self.items(")")))
            return Adt(m.group(0))
        m = NUMBER.match(self.text, self.pos)
        if m and not IDENT.match(self.text, m.end()) and self._ends_value(m.end()):
            self.pos = m.end()
            token = m.group(0)
            return float(token) if any(c in token for c in ".eE") else int(token)
        end = self.pos + 3
        if (
            self.text.startswith("nil", self.pos)
            and self._ends_value(end)
            and not IDENT.match(self.text, end)
        ):
            self.pos = end
            return NIL
        if self.bare:
            return self.bare_symbol()
        raise self.error("expected a value")


def parse_value(text: str, *, bare: bool = False):
    """Parse one value. With `bare`, unquoted symbols are allowed (fact syntax)."""
    reader = _Reader(text, bare)
    value = reader.value()
    reader.ws()
    if reader.pos != len(text):
        raise reader.error("trailing text")
    return value


ATOM = re.compile(r"([A-Za-z_+@][\w.@+]*)\s*\(")


def parse_atom(text: str) -> tuple[str, tuple, bool]:
    """`!rel(v, ...)` in explain syntax -> (relation, values, negated)."""
    text = text.strip()
    negated = text.startswith("!")
    if negated:
        text = text[1:].lstrip()
    m = ATOM.match(text)
    if not m:
        raise ValueSyntaxError(f"not an atom: {text!r}")
    reader = _Reader(text, False)
    reader.pos = m.end()
    values = tuple(reader.items(")"))
    reader.ws()
    if reader.pos != len(text):
        raise reader.error("trailing text after atom")
    return m.group(1), values, negated


def parse_constraint(text: str) -> tuple[str, object, object] | None:
    """`lhs op rhs` with engine-printed operands, or None if not binary."""
    for m in re.finditer(r"\s(!=|<=|>=|=|<|>)\s", text):
        try:
            lhs = parse_value(text[: m.start()])
            rhs = parse_value(text[m.end() :])
        except ValueSyntaxError:
            continue
        return m.group(1), lhs, rhs
    m = re.match(r"(not_)?(contains|match)\((.*)\)\Z", text.strip())
    if m:
        args = split_top(m.group(3))
        if len(args) == 2:
            try:
                op = (m.group(1) or "") + m.group(2)
                return op, parse_value(args[0]), parse_value(args[1])
            except ValueSyntaxError:
                return None
    return None


def split_top(text: str, sep: str = ",") -> list[str]:
    """Split on top-level `sep`, respecting strings and brackets."""
    parts: list[str] = []
    depth, cur, i, in_str = 0, [], 0, False
    while i < len(text):
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    last = "".join(cur).strip()
    if last or parts:
        parts.append(last)
    return parts


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def explain_text(v, rename=None) -> str:
    """Engine (explain) syntax, symbols quoted. `rename` maps ADT branch names."""
    if isinstance(v, str):
        return quote(v)
    if isinstance(v, float):
        return f"{v:.6f}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Nil):
        return "nil"
    if isinstance(v, Record):
        return "[" + ", ".join(explain_text(f, rename) for f in v.fields) + "]"
    if isinstance(v, Adt):
        name = rename(v.branch) if rename else v.branch
        if not v.args:
            return f"${name}"
        return f"${name}(" + ", ".join(explain_text(a, rename) for a in v.args) + ")"
    raise TypeError(f"not a value: {v!r}")


def facts_text(v) -> str:
    """Fact-file syntax: symbols bare."""
    if isinstance(v, str):
        return v
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Nil):
        return "nil"
    if isinstance(v, Record):
        return "[" + ", ".join(facts_text(f) for f in v.fields) + "]"
    if isinstance(v, Adt):
        if not v.args:
            return f"${v.branch}"
        return f"${v.branch}(" + ", ".join(facts_text(a) for a in v.args) + ")"
    raise TypeError(f"not a value: {v!r}")


def from_json(j):
    """The inverse of `to_json`: a protocol value back to a value."""
    if isinstance(j, dict):
        if "record" in j:
            return Record(tuple(from_json(f) for f in j["record"]))
        if j.get("nil"):
            return NIL
        return Adt(j.get("qualified", j["adt"]), tuple(from_json(a) for a in j["args"]))
    return j


def to_json(v, branch_info=None):
    """Protocol rendering (docs/proof-protocol.md, "Values").

    Symbols are JSON strings and numbers JSON numbers; `{"record": [...]}`,
    `{"nil": true}` and `{"adt": name, "args": [...]}` are the structured
    values. `branch_info(name)` returns (display name, package) so qualified
    branch names map back to the name their package wrote."""
    if isinstance(v, str | int | float):
        return v
    if isinstance(v, Nil):
        return {"nil": True}
    if isinstance(v, Record):
        return {"record": [to_json(f, branch_info) for f in v.fields]}
    if isinstance(v, Adt):
        out: dict = {"adt": v.branch, "args": [to_json(a, branch_info) for a in v.args]}
        if branch_info:
            display, package = branch_info(v.branch)
            if display != v.branch:
                out["adt"] = display
                out["qualified"] = v.branch
                out["package"] = package
        return out
    raise TypeError(f"not a value: {v!r}")
