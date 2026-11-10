# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test-suite hooks. With BUILDLOG_NINJA_SNAPSHOT set (scripts/ninja_snapshot.py),
every build.ninja a test leaves under its tmp_path is copied there, keyed by
the test id, so refactors can check that emitted graphs stay byte-identical."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from . import host
from .halide_env import halide_define

# Tests that rerun a configure assert what a rerun does (rounds, probes run);
# the up-to-date skip would answer first. tests/test_uptodate.py turns it on.
os.environ.setdefault("BUILDLOG_UPTODATE", "0")

SNAPSHOT = os.environ.get("BUILDLOG_NINJA_SNAPSHOT")

if SNAPSHOT:

    @pytest.fixture(autouse=True)
    def _ninja_snapshot(request, tmp_path):
        yield
        key = re.sub(r"[^\w.-]+", "_", request.node.nodeid)
        for f in sorted(tmp_path.rglob("build.ninja")):
            dest = Path(SNAPSHOT) / key / f.relative_to(tmp_path)
            dest = dest.with_suffix(".ninja")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dest)


# --- the suite never writes into the checkout -------------------------------
# Tests configure into tmp_path; a configure records the packages it loaded in
# the project's buildlog.lock, which for an in-repo project is a tracked file.
# Rewriting it races every parallel test that reads it, so a test that would
# change a tracked lock fails here (tests/test_locks.py checks they are
# current), and the session fails if any tracked or unignored file changed.


def _tree_state() -> str | None:
    """git status of the checkout, with a hash of each changed file's content."""
    import hashlib
    import subprocess

    cp = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"], cwd=ROOT, capture_output=True, check=False
    )
    if cp.returncode:
        return None
    lines = []
    for entry in sorted(x for x in cp.stdout.decode().split("\0") if x):
        f = ROOT / entry[3:]
        digest = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "-"
        lines.append(f"{entry} {digest}")
    return "\n".join(lines)


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):  # the controller (or no xdist)
        session.config._buildlog_tree = _tree_state()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_buildlog_tree", None)
    if before is None:
        return
    after = _tree_state()
    if after != before:
        old, new = set(before.splitlines()), set((after or "").splitlines())
        changed = sorted({x.rsplit(" ", 1)[0] for x in old ^ new})
        session.config.get_terminal_writer().line(
            "\nerror: the checkout changed during the test session (a test wrote into "
            "the repo, or the worktree was updated while the suite ran):\n  "
            + "\n  ".join(changed),
            red=True,
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True, scope="session")
def _no_tracked_lock_writes(tmp_path_factory):
    # Session scope, so module- and session-scoped configuring fixtures are guarded too.
    import buildlog.engine
    from buildlog.packages import LOCKFILE, lock_text

    real = buildlog.engine.write_lock
    basetemp = tmp_path_factory.getbasetemp().resolve()

    def guarded(project, packages):
        lock = Path(project).resolve() / LOCKFILE
        if (
            lock.is_relative_to(ROOT)
            and not lock.is_relative_to(basetemp)
            and lock_text(project, packages) != (lock.read_text() if lock.exists() else None)
        ):
            pytest.fail(
                f"a configure would rewrite {lock.relative_to(ROOT)}: it is stale (refresh it "
                "with --update-lock) or the test should copy the project to tmp_path"
            )
        real(project, packages)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(buildlog.engine, "write_lock", guarded)
        yield


# --- configured build directories shared by read-only tests -----------------
# Session-scoped, so under pytest-xdist each worker configures a project at
# most once, in its own basetemp. `why`, `query` and `why-not` only read a
# configured directory (they write scratch files under <builddir>/why, which
# is private to the worker). A test that reconfigures, builds or edits a
# directory configures its own instead.

ROOT = Path(__file__).resolve().parents[1]
CROSS = ROOT / "examples" / "cross"
HALIDE_PROJECTS = ROOT / "tests" / "projects" / "halide"
HALIDE_PACKAGE_PATH = ROOT / "tests" / "fixtures" / "rule-packages"


def _configure_shared(argv: list[str]) -> None:
    from buildlog.cli import main

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
        assert main(argv) == 0


@pytest.fixture(scope="session")
def cross_build(tmp_path_factory) -> Path:
    """examples/cross configured with its Apple clang preset."""
    host.require_macos()
    build = tmp_path_factory.mktemp("shared") / "cross"
    user = CROSS / "presets" / "apple-clang.toml"
    _configure_shared(["configure", "--user", str(user), "-C", str(build), str(CROSS)])
    return build


@pytest.fixture(scope="session")
def bilateral_grid_build(tmp_path_factory) -> Path:
    """tests/projects/halide/bilateral_grid with the Homebrew preset."""
    build = tmp_path_factory.mktemp("shared") / "bilateral_grid"
    _configure_shared(
        [
            "configure",
            "--package-path",
            str(HALIDE_PACKAGE_PATH),
            "--user",
            str(HALIDE_PROJECTS / "presets" / "homebrew-arm64.toml"),
            *halide_define(),
            "-C",
            str(build),
            str(HALIDE_PROJECTS / "bilateral_grid"),
        ]
    )
    return build
