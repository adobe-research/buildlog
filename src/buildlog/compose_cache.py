# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A memo of `layers.compose_program` in the build directory.

Composition is a pure function of what it reads: the manifests, the layer,
stratum and project files (by content), the existence of the facts, imports
and provider rule packages a manifest lists, the Soufflé binary that checks
the result, the loader's own source, and the arguments. The key hashes all of
that, so a hit is a composition that would give the same program, and a
change to any input is a miss. Only successful compositions are stored: a
loader error is always recomputed, so its messages are the loader's.

An entry holds the Program (layers, probes, outputs) and every file the
composition wrote under its output directory; a hit writes them back, so the
program directory (and its digest) is what a miss would have produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import platform
import sys
import tempfile
from pathlib import Path

from .errors import BuildlogError

# Bump when the stored format or what the key covers changes.
FORMAT = "1"
ENV = "BUILDLOG_COMPOSE_CACHE"
KEEP = 8
_SOURCE: str | None = None


def enabled() -> bool:
    return os.environ.get(ENV, "1") != "0"


def source_digest() -> str:
    """A digest of the loader's own source (the buildlog package)."""
    global _SOURCE
    if _SOURCE is None:
        root = Path(__file__).resolve().parent
        h = hashlib.sha256()
        for f in sorted(root.rglob("*.py")):
            h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
        _SOURCE = h.hexdigest()
    return _SOURCE


def _file(path: Path, memo: dict[Path, str]) -> str:
    """The content hash of a file, or a marker for one that is not a file."""
    out = memo.get(path)
    if out is None:
        try:
            out = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "-"
        except OSError:
            out = "-"
        memo[path] = out
    return out


def key(
    packages: list,
    outdir: Path,
    work: Path,
    required_outputs: tuple[str, ...],
    required_inputs: tuple[str, ...],
    project_files: list[Path] | None,
    explicit_manifests: frozenset[Path] | None = None,
) -> str | None:
    """The memo key of a composition, or None when it cannot be keyed (a
    manifest that does not parse: the loader reports that, uncached)."""
    from . import souffle_cache
    from .layers import PackageRef, parse_manifest
    from .paths import require_souffle

    memo: dict[Path, str] = {}
    parts: list = [
        FORMAT,
        source_digest(),
        sys.version,
        platform.machine(),
        pickle.HIGHEST_PROTOCOL,
        souffle_cache._identity(require_souffle()),
        str(outdir),
        str(work),
        list(required_outputs),
        list(required_inputs),
        None if explicit_manifests is None else sorted(str(p) for p in explicit_manifests),
    ]
    for pkg in packages:
        ref = pkg if isinstance(pkg, PackageRef) else PackageRef(pkg)
        errors: list[str] = []
        try:
            data = parse_manifest(ref.manifest, errors)
        except BuildlogError:
            return None
        if errors:
            return None
        base = ref.manifest.parent
        parts.append(
            [
                ref.source,
                str(ref.manifest),
                str(ref.manifest.resolve()),
                _file(ref.manifest, memo),
                [[f, _file(base / f, memo)] for f in data["layers"]],
                [[f, (base / f).is_file()] for f in data["facts"]],
                [[i["file"], (base / i["file"]).is_file()] for i in data["imports"]],
                [[str(st.program), _file(st.program, memo)] for st in data["stratum"]],
                [
                    [str(p.rules), p.rules is not None and (p.rules / "manifest.toml").is_file()]
                    for p in data["provider"]
                ],
            ]
        )
    parts.append([[str(f), _file(f, memo)] for f in project_files or []])
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _entry(cache_dir: Path, k: str) -> Path:
    return cache_dir / f"compose-{k}.pkl"


def load(cache_dir: Path, k: str, outdir: Path, work: Path):
    """The cached Program with its files rewritten under `outdir`, or None."""
    try:
        data = pickle.loads(_entry(cache_dir, k).read_bytes())
        program, files = data["program"], data["files"]
        if data["key"] != k or program.build is None or program.errors:
            return None
        files = {str(rel): bytes(blob) for rel, blob in files.items()}
    except Exception:  # noqa: BLE001 - any unreadable entry is a miss
        return None
    import shutil

    for d in (outdir, work):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    for rel, blob in files.items():
        dst = outdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
    program.cached = True
    try:
        os.utime(_entry(cache_dir, k))
    except OSError:
        pass
    return program


def store(cache_dir: Path, k: str, program, outdir: Path) -> None:
    files = {
        f.relative_to(outdir).as_posix(): f.read_bytes() for f in outdir.rglob("*") if f.is_file()
    }
    try:
        blob = pickle.dumps({"key": k, "program": program, "files": files})
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, _entry(cache_dir, k))
        entries = sorted(cache_dir.glob("compose-*.pkl"), key=lambda p: p.stat().st_mtime)
        for old in entries[:-KEEP]:
            old.unlink(missing_ok=True)
    except (OSError, pickle.PicklingError):
        pass
