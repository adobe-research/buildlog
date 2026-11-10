# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A strict JSON reader that remembers the line of every value.

Objects are `LDict` (a dict with `.line` and `.lines[key]`), arrays `LList`
(a list with `.line` and `.lines[index]`); scalars are plain Python values.
Duplicate object keys are an error.
"""

from __future__ import annotations

import bisect
import json
import re


class LDict(dict):
    line: int = 0
    lines: dict[str, int]


class LList(list):
    line: int = 0
    lines: list[int]


class JsonError(ValueError):
    def __init__(self, line: int, message: str):
        super().__init__(message)
        self.line = line
        self.message = message


_WS = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def loads(text: str):
    p = _Parser(text)
    value = p.value()
    p.ws()
    if p.i != len(text):
        raise JsonError(p.line(), "trailing data after the JSON value")
    return value


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line(self) -> int:
        return bisect.bisect_left(self.newlines, self.i) + 1

    def ws(self) -> None:
        self.i = _WS.match(self.text, self.i).end()

    def value(self):
        self.ws()
        if self.i >= len(self.text):
            raise JsonError(self.line(), "unexpected end of JSON")
        c = self.text[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            return self.string()
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(word, self.i):
                self.i += len(word)
                return val
        m = _NUMBER.match(self.text, self.i)
        if m:
            self.i = m.end()
            return json.loads(m.group(0))
        raise JsonError(self.line(), f"unexpected character {c!r}")

    def string(self) -> str:
        try:
            s, end = json.decoder.scanstring(self.text, self.i + 1)
        except json.JSONDecodeError as exc:
            raise JsonError(self.line(), exc.msg) from exc
        self.i = end
        return s

    def obj(self) -> LDict:
        out = LDict()
        out.line, out.lines = self.line(), {}
        self.i += 1
        self.ws()
        if self.text.startswith("}", self.i):
            self.i += 1
            return out
        while True:
            self.ws()
            if not self.text.startswith('"', self.i):
                raise JsonError(self.line(), "expected an object key")
            key_line = self.line()
            key = self.string()
            if key in out:
                raise JsonError(key_line, f"duplicate key {key!r}")
            self.ws()
            if not self.text.startswith(":", self.i):
                raise JsonError(self.line(), "expected ':'")
            self.i += 1
            out[key] = self.value()
            out.lines[key] = key_line
            self.ws()
            if self.text.startswith(",", self.i):
                self.i += 1
                continue
            if self.text.startswith("}", self.i):
                self.i += 1
                return out
            raise JsonError(self.line(), "expected ',' or '}'")

    def arr(self) -> LList:
        out = LList()
        out.line, out.lines = self.line(), []
        self.i += 1
        self.ws()
        if self.text.startswith("]", self.i):
            self.i += 1
            return out
        while True:
            self.ws()
            out.lines.append(self.line())
            out.append(self.value())
            self.ws()
            if self.text.startswith(",", self.i):
                self.i += 1
                continue
            if self.text.startswith("]", self.i):
                self.i += 1
                return out
            raise JsonError(self.line(), "expected ',' or ']'")
