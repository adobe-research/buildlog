# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tomllib
from pathlib import Path


class BuildlogError(RuntimeError):
    pass


class SpecError(BuildlogError, ValueError):
    """A spec, user or package fact file is malformed or asserts what it may not."""


class LoaderError(BuildlogError):
    """The layer set failed the loader checks; nothing was probed or derived."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("layer check failed:\n" + "\n".join(f"  {e}" for e in self.errors))


def toml_error(path: Path, exc: tomllib.TOMLDecodeError) -> str:
    """Format Python's TOMLDecodeError with an explicit file:line:column."""
    line = getattr(exc, "lineno", None)
    col = getattr(exc, "colno", None)
    if line is not None and col is not None:
        return f"{path}:{line}:{col}: TOML: {exc.msg}"
    return f"{path}: TOML: {exc}"


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise BuildlogError(toml_error(path, exc)) from exc
