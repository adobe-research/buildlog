# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The up-to-date check: a configure whose every input is unchanged since the
last successful one is skipped, and its outputs are left as they are.

A configure is a function of what it reads. After a successful emit configure
this module records that (`uptodate.pkl` in the build directory):

- the invocation: the effective CLI state, as the regeneration facts
  (configure_input paths, configure_arg argv, config_select) state it, the
  BUILDLOG_* and HOME environment, the buildlog source, the Soufflé binary
  and the Python;
- the content hash of every configure_input file (specs, user and preset
  files, the lock, rule/layer/package files) and of the Python files of each
  package directory, and the existence of the files that pick the project
  configuration;
- the package set the last pass resolved (resolution is repeated and
  compared, so a package appearing in a search path is a change);
- every probe answer of the final pass with its cache key and a digest of
  its cache entry. The key (recomputed here, so fingerprints re-read the
  environment and tool identities as the probes recorded them) and the watch
  set must still hold, the conditions under which a replay accepts an answer;
- the outputs it left (build.ninja and tests.json by content, the facts by
  listing) and what it printed.

Same program, same input facts and same answers give the same model, so the
outputs are the ones already there. Anything missing, unreadable or
different, and any project with a probe kind that declares no fingerprint
(its answer is never known to be current), is a full configure. A full
configure removes the record first and writes it only when it succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import compose_cache, providers, souffle_cache
from .discovery import (
    Answer,
    _cached_rows,
    _records,
    _result_state,
    cache_key,
    kinds_from_probes,
    watch_holds,
)
from .layers import ProbeDecl, ProviderDecl

FORMAT = "1"
ENV = "BUILDLOG_UPTODATE"
FILE = "uptodate.pkl"


def enabled() -> bool:
    return os.environ.get(ENV, "1") != "0"


def invalidate(builddir: Path) -> None:
    (builddir / FILE).unlink(missing_ok=True)


def _sha(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "-"


def _jsha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _environment() -> str:
    from .paths import require_souffle

    env = {k: v for k, v in os.environ.items() if k.startswith("BUILDLOG_") or k == "HOME"}
    return _jsha(
        [
            FORMAT,
            env,
            compose_cache.source_digest(),
            sys.version,
            souffle_cache._identity(require_souffle()),
        ]
    )


def _python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def snapshot(project: Path, packages, driver_rows: dict, lock: Path) -> dict:
    """What the configure about to run reads, as of now (the lock apart: the
    configure rewrites it, so it is taken afterwards)."""
    inputs = sorted(p for (p,) in driver_rows["configure_input"] if p != str(lock.resolve()))
    files = {p: _sha(p) for p in inputs}
    for pkg in packages:
        directory = pkg.candidate.manifest.parent
        for f in _python_files(directory):
            files.setdefault(str(f), _sha(f))
    from .emit import _workspace_root

    return {
        "files": files,
        "driver": _jsha({k: sorted(v) for k, v in driver_rows.items()}),
        "extras": [
            [(project / name).exists() for name in ("buildlog.toml", "pyproject.toml")],
            (project / "project").exists(),
            (project / "project" / "project").exists(),
            _workspace_root(project),
        ],
    }


def _outputs(builddir: Path) -> dict:
    ninja = builddir / "souffle"
    facts = ninja / "facts"
    listing = []
    if facts.is_dir():
        for f in sorted(facts.iterdir()):
            st = f.stat()
            listing.append([f.name, st.st_size, st.st_mtime_ns])
    return {
        "ninja": _sha(ninja / "build.ninja"),
        "tests": _sha(ninja / "tests.json"),
        "facts": listing,
    }


def store(
    builddir: Path,
    *,
    lock: Path,
    snap: dict,
    discovered: list,
    packages,
    program_digest: str,
    probes: list[ProbeDecl],
    provider_decls: list[ProviderDecl],
    kinds,
    metrics: dict,
    printed: list[str],
) -> None:
    """Record a successful configure, if it can be told up to date later."""
    if any(k.fingerprint is None for k in kinds):
        return
    record = _records(builddir).get(program_digest)
    if not record or record.get("program") != program_digest:
        return
    try:
        cache = json.loads((builddir / "probe-cache.json").read_text())
    except (OSError, ValueError):
        return
    answers = []
    for name, rid, args, key in record["answers"]:
        if key is None or key not in cache:
            return
        answers.append([name, rid, list(args), key, _jsha(cache[key])])
    data = {
        "format": FORMAT,
        "environment": _environment(),
        "snapshot": snap,
        "lock": _sha(lock),
        "discovered": [[str(p), reason] for p, reason in discovered],
        "refs": [[str(p.ref.manifest), p.ref.source] for p in packages],
        "program": program_digest,
        "probes": probes,
        "providers": provider_decls,
        "answers": answers,
        "metrics": metrics,
        "printed": printed,
        "outputs": _outputs(builddir),
    }
    builddir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=builddir, prefix=".uptodate-")
    with os.fdopen(fd, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, builddir / FILE)


def _answers_hold(builddir: Path, data: dict) -> bool:
    kinds = kinds_from_probes(data["probes"])
    if any(k.fingerprint is None for k in kinds):
        return False
    providers.activate(data["providers"])
    by_name = {k.name: k for k in kinds}
    try:
        cache = json.loads((builddir / "probe-cache.json").read_text())
    except (OSError, ValueError):
        return False
    memo: dict = {}
    answers: dict = {}
    for name, rid, args, key, entry_sha in data["answers"]:
        entry = cache.get(key)
        if name not in by_name or entry is None or _jsha(entry) != entry_sha:
            return False
        if not watch_holds(entry, memo):
            return False
        answers[(name, rid)] = Answer(by_name[name], tuple(args), key, _cached_rows(entry), False)
    view = _result_state(answers, confirmed_only=False)
    return all(cache_key(a.kind, a.args, view) == a.key for a in answers.values())


def check(
    project: Path, builddir: Path, resolve: Callable[[list], tuple[list, dict]], lock: Path
) -> dict | None:
    """The stored record when the configure about to run would reproduce the
    outputs there, else None. `resolve(discovered)` re-resolves the packages
    and returns them with the regeneration facts."""
    try:
        data = pickle.loads((builddir / FILE).read_bytes())
        if data.get("format") != FORMAT or data["environment"] != _environment():
            return None
        discovered = [(Path(p), reason) for p, reason in data["discovered"]]
        packages, driver_rows = resolve(discovered)
        if [[str(p.ref.manifest), p.ref.source] for p in packages] != data["refs"]:
            return None
        snap = snapshot(project, packages, driver_rows, lock)
        if snap != data["snapshot"]:
            return None
        if _sha(lock) != data["lock"] or data["outputs"] != _outputs(builddir):
            return None
        return data if _answers_hold(builddir, data) else None
    except Exception:  # noqa: BLE001 (whatever fails, the configure runs in full)
        return None
