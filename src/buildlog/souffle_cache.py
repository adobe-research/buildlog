# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A cache of Soufflé's program analyses (the loader's layer check).

The loader runs Soufflé on the qualified layer files without evaluating them:
`--show=initial-ast` per package (layers.ast_dump), and either `--serve`
startup or `--show=type-analysis` on the composed program. Each check is a pure
function of the Soufflé binary, its arguments and the files it reads, so its
result (exit status, stdout, stderr, errors included) is memoized under a
hash of exactly those.

The cache is shared by every build directory of the user (`cache_dir()`):
the loader writes the qualified files into the build directory, so the build
directory's path is replaced by a token in the key and in the stored output
and restored on a hit. Entries are written atomically and never updated, so
concurrent configures may share the directory; a check is stored only if it
completed normally (a serve process reported ready) and neither the binary nor
any file it read changed while it ran (another checkout may rebuild Soufflé in
place). A file that includes a file
the caller did not name bypasses the cache, since the key would not cover it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .paths import require_souffle

# Bump when the stored format or what the key covers changes.
FORMAT = "1"
CACHE_ENV = "BUILDLOG_CACHE_DIR"
_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^">]+)[">]', re.MULTILINE)


def cache_dir() -> Path | None:
    """The user's buildlog cache directory; None when BUILDLOG_CACHE_DIR=off."""
    env = os.environ.get(CACHE_ENV)
    if env is not None:
        return None if env in ("", "off", "0") else Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "buildlog"
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "buildlog"


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str
    hit: bool = False


def _identity(binary: Path) -> list:
    real = binary.resolve()
    st = real.stat()
    return [str(real), st.st_size, st.st_mtime_ns, st.st_ino]


def _covered(reads: list[Path], texts: dict[Path, str]) -> bool:
    """Every file a read file includes is itself one of the reads."""
    known = {f.resolve() for f in reads}
    for f, text in texts.items():
        for inc in _INCLUDE.findall(text):
            target = Path(inc)
            if not target.is_absolute():
                target = f.parent / target
            if target.resolve() not in known:
                return False
    return True


def _stable(returncode: int, souffle: Path, identity: list, reads: list[Path], files, norm) -> bool:
    """Whether a completed check's result may be stored: it did not fail from
    a signal, and neither the binary (rebuilt in place by another checkout) nor
    any file it read (rewritten by a concurrent configure of the same build
    directory) changed while it ran, so the result is the key's."""
    if returncode < 0:
        return False
    try:
        if _identity(souffle) != identity:
            return False
        now = [hashlib.sha256(norm(f.read_text()).encode()).hexdigest() for f in reads]
    except OSError:
        return False
    return now == [h for _, h in files]


def run(
    args: list[str],
    reads: list[Path],
    root: Path,
    *,
    keep_stdout: bool = True,
    runner: Callable[[], Result] | None = None,
) -> Result:
    """Run `souffle *args`, which reads exactly `reads` (all under `root`).
    With keep_stdout=False a successful run's stdout is not stored (it is
    returned empty on a hit). A runner can perform the check in a persistent
    process; args still identify the check in the cache."""
    souffle = require_souffle()

    def execute() -> Result:
        if runner is not None:
            return runner()
        cp = subprocess.run([str(souffle), *args], text=True, capture_output=True, check=False)
        return Result(cp.returncode, cp.stdout, cp.stderr)

    base = cache_dir()
    texts = {f: f.read_text() for f in reads} if base is not None else {}
    if base is None or not _covered(reads, texts):
        return execute()
    roots = sorted({str(root), str(root.resolve())}, key=len, reverse=True)
    tokens = [f"\x00ROOT{i}\x00" for i in range(len(roots))]

    def norm(s: str) -> str:
        for r, t in zip(roots, tokens, strict=True):
            s = s.replace(r, t)
        return s

    def denorm(s: str) -> str:
        for r, t in zip(roots, tokens, strict=True):
            s = s.replace(t, r)
        return s

    files = [[norm(str(f)), hashlib.sha256(norm(texts[f]).encode()).hexdigest()] for f in reads]
    key = hashlib.sha256(
        json.dumps([FORMAT, _identity(souffle), [norm(a) for a in args], files]).encode()
    ).hexdigest()
    entry = base / "souffle" / key[:2] / f"{key}.json"
    try:
        data = json.loads(entry.read_text())
        return Result(data["returncode"], denorm(data["stdout"]), denorm(data["stderr"]), True)
    except (OSError, ValueError, KeyError):
        pass
    identity = _identity(souffle)
    cp = execute()
    if not _stable(cp.returncode, souffle, identity, reads, files, norm):
        return Result(cp.returncode, cp.stdout, cp.stderr)
    stdout = cp.stdout if keep_stdout or cp.returncode else ""
    data = {"returncode": cp.returncode, "stdout": norm(stdout), "stderr": norm(cp.stderr)}
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=entry.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, entry)
    except OSError:
        pass
    return Result(cp.returncode, cp.stdout, cp.stderr)
