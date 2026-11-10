# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Answer-independent identities and observations for dependency preparation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LoaderError


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _links(path: Path) -> list[list[str]]:
    out = []
    for part in reversed((path, *path.parents)):
        try:
            if part.is_symlink():
                out.append([str(part), os.readlink(part)])
        except OSError as exc:
            raise LoaderError([f"{part}: cannot observe symlink: {exc}"]) from exc
    return out


def _kind(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _observe(path: Path, directory: bool | None) -> tuple[dict, bytes | list[Path] | None]:
    try:
        links = _links(path)
        kind = _kind(path)
        value: bytes | list[Path] | None = None
        token: object = None
        if directory and kind == "directory":
            value = sorted(path.iterdir())
            token = [[p.name, _kind(p), os.readlink(p) if p.is_symlink() else None] for p in value]
        elif directory is False and kind == "file":
            value = path.read_bytes()
            token = hashlib.sha256(value).hexdigest()
        return {"kind": kind, "links": links, "value": token}, value
    except OSError as exc:
        raise LoaderError([f"{path}: cannot observe preparation input: {exc}"]) from exc


@dataclass
class Certificate:
    """Content and negative observations; no timestamps or probe answers."""

    files: dict[str, dict] = field(default_factory=dict)
    directories: dict[str, dict] = field(default_factory=dict)
    paths: dict[str, dict] = field(default_factory=dict)

    def _read(self, path: Path, *, directory: bool):
        path = path.absolute()
        records = self.directories if directory else self.files
        token, value = _observe(path, directory)
        old = records.setdefault(str(path), token)
        if old != token:
            raise LoaderError([f"{path}: preparation input changed while being read"])
        return value

    def file(self, path: Path) -> bytes | None:
        return self._read(path, directory=False)

    def directory(self, path: Path) -> list[Path]:
        return self._read(path, directory=True) or []

    def is_file(self, path: Path) -> bool:
        """A lookup observation, without reading a potentially large artifact."""
        path = path.absolute()
        token, _ = _observe(path, None)
        if self.paths.setdefault(str(path), token) != token:
            raise LoaderError([f"{path}: preparation lookup changed while being read"])
        return token["kind"] == "file"

    def valid(self) -> bool:
        try:
            return (
                all(_observe(Path(p), False)[0] == t for p, t in self.files.items())
                and all(_observe(Path(p), True)[0] == t for p, t in self.directories.items())
                and all(_observe(Path(p), None)[0] == t for p, t in self.paths.items())
            )
        except LoaderError:
            return False

    def confirm(self) -> None:
        if not self.valid():
            raise LoaderError(["preparation inputs changed during traversal; retry configuration"])

    def to_json(self) -> dict:
        return {
            "version": 2,
            "files": self.files,
            "directories": self.directories,
            "paths": self.paths,
        }

    @classmethod
    def from_json(cls, raw: object) -> Certificate | None:
        if not isinstance(raw, dict) or raw.get("version") != 2:
            return None
        if not all(isinstance(raw.get(k), dict) for k in ("files", "directories", "paths")):
            return None
        for records in (raw["files"], raw["directories"], raw["paths"]):
            if not all(
                isinstance(p, str)
                and Path(p).is_absolute()
                and isinstance(t, dict)
                and set(t) == {"kind", "links", "value"}
                for p, t in records.items()
            ):
                return None
        return cls(raw["files"], raw["directories"], raw["paths"])

    @property
    def digest(self) -> str:
        return digest(self.to_json())


@dataclass(frozen=True, order=True)
class File:
    provider: str
    path: Path
    namespace: str = ""

    @property
    def identity(self) -> str:
        # Keep the lookup spelling: relative metadata paths are interpreted there.
        return "bundle:" + digest([self.provider, str(self.path), self.namespace])
