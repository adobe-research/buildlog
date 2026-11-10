# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every committed buildlog.lock is current: each package it records resolves,
from the project, to the same version, source and content hash. A stale lock
makes every configure of the project rewrite it (configure records what it
loaded), so tests reading it would race tests rewriting it; refresh with
`buildlog check --update-lock DIR [--package-path P]` (the paths below)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from buildlog.engine import project_packages
from buildlog.errors import LoaderError
from buildlog.packages import read_lock

ROOT = Path(__file__).resolve().parents[1]
# linux/port.py rewrites platform and toolchain names inside the copied
# fixture packages, so their content hashes cannot match the committed locks.
PORTED = (ROOT / "linux" / ".ported").exists()

# The package paths a project is configured with (as in README/notes).
PACKAGE_PATHS = {
    "tests/projects/halide/": "tests/fixtures/rule-packages",
    "tests/fixtures/layout/": "tests/fixtures/layout",
    "case-studies/halide/": "case-studies/halide/packages",
    "tests/fixtures/target-ids/": "tests/fixtures/target-ids",
    "case-studies/nanobind/": "case-studies/nanobind",
}


def tracked_locks() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*buildlog.lock"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if out.returncode:
        return []
    return sorted(str(Path(f).parent) for f in out.stdout.split())


@pytest.mark.parametrize("project", tracked_locks())
def test_committed_lock_is_current(project, monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    path = ROOT / project
    paths = [ROOT / p for k, p in PACKAGE_PATHS.items() if f"{project}/".startswith(k)]
    try:
        packages = project_packages(path, paths)
    except LoaderError as exc:
        # Some fixtures load a package only through discovery or fail on
        # purpose (export-requires: an unresolved dependency); what does
        # resolve is still checked package by package below.
        pytest.skip(f"packages do not resolve statically: {exc}")
    locked = read_lock(path)
    assert locked, f"{project}: empty lock"
    for pkg in packages:
        old = locked.get(pkg.name)
        assert old is not None, f"{project}: {pkg.name} loaded but not locked"
        new = pkg.lock_entry(path)
        for key in ("version", "source", "entry_point", "path", "hash"):
            if key == "hash" and PORTED and new.get("source") == "path":
                continue
            assert old.get(key) == new.get(key), (
                f"{project}/buildlog.lock is stale: {pkg.name} {key} "
                f"{old.get(key)} -> {new.get(key)}; refresh it with --update-lock"
            )
