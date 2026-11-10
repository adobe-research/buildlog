# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A seeded configure (guarded seeding after an input edit) derives what a fresh
one does, on real package discovery: a dependency that appears or disappears,
package-selection inputs that change (pin, prefix, the selected file itself),
and any probe order. Compared: the outcome, build.ninja, and every derived
relation that is not a probe result."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from buildlog.discovery import SEED_ENV
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

from .test_importers import PREFIX, consumer

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")

DEP = "Greet::shout"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def _set(project: Path, *, dep: str | None = DEP, user: str = "") -> None:
    """Rewrite the consumer project's spec and user file."""
    spec = (project / "spec.toml").read_text()
    spec = "".join(line + "\n" for line in spec.splitlines() if not line.startswith("dep = "))
    if dep:
        spec += f'dep = [["hello", {json.dumps(dep)}, "private"]]\n'
    (project / "spec.toml").write_text(spec)
    base = "".join(
        line + "\n"
        for line in (project / "user.toml").read_text().splitlines()
        if line.startswith(("toolchain_pin", '["', "'"))
    )
    (project / "user.toml").write_text(base + user)


def _run(project: Path, build: Path, monkeypatch, capsys, *, seed=True, shuffle=None) -> dict:
    monkeypatch.setenv(SEED_ENV, "1" if seed else "0")
    capsys.readouterr()
    try:
        _, metrics = configure_project(
            project,
            build,
            update_lock=True,
            user_files=[project / "user.toml"],
            shuffle_probes=shuffle,
        )
        outcome = "ok"
    except SystemExit as exc:
        metrics, outcome = None, f"exit: {exc}"
    err = sorted(line.replace(str(build), "<B>") for line in capsys.readouterr().err.splitlines())
    errors = sorted(e for e in err if e.startswith("error"))
    out = {"outcome": outcome, "errors": errors, "metrics": metrics}
    ninja = build / "souffle" / "build.ninja"
    out["ninja"] = ninja.read_text().replace(str(build), "<B>") if ninja.exists() else None
    if out["ninja"] is not None:
        # A build-local certificate can sort before or after external inputs
        # depending on the build directory's name; compare the normalized set.
        lines = out["ninja"].splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("build build.ninja: regen"):
                head, sep, inputs = line.partition(" | ")
                lines[i] = head + sep + " ".join(sorted(inputs.split())) + "\n"
        out["ninja"] = "".join(lines)
    facts = build / "souffle" / "facts"
    out["facts"] = {
        f.name: sorted(f.read_text().replace(str(build), "<B>").splitlines())
        for f in sorted(facts.glob("*.csv"))
        if not f.name.startswith("res_")
    }
    return out


def _same(a: dict, b: dict) -> None:
    assert a["outcome"] == b["outcome"]
    assert a["errors"] == b["errors"]
    assert a["ninja"] == b["ninja"]
    assert a["facts"] == b["facts"]


def _compare(project, tmp_path, monkeypatch, capsys, first, second, *, expected_mode="guarded"):
    """Configure in `a` under `first`, edit to `second`, configure again in
    `a` (seeded) and from scratch in `b`, `c` and `d` (fresh, two probe orders)."""
    first()
    _run(project, tmp_path / "a", monkeypatch, capsys)
    second()
    seeded = _run(project, tmp_path / "a", monkeypatch, capsys)
    fresh = _run(project, tmp_path / "b", monkeypatch, capsys, seed=False)
    _same(seeded, fresh)
    for i, shuffle in enumerate((1, 2)):
        _same(
            fresh,
            _run(project, tmp_path / f"s{i}", monkeypatch, capsys, seed=False, shuffle=shuffle),
        )
    if seeded["metrics"] is not None:
        assert seeded["metrics"]["seed"]["mode"] == expected_mode
        assert not seeded["metrics"]["seed"].get("restarted")
    return seeded


def _prefix(*roots: Path) -> str:
    return "package_prefix = [" + ", ".join(f"[{json.dumps(str(r))}]" for r in roots) + "]\n"


def _moved(tmp_path: Path) -> Path:
    moved = tmp_path / "moved"
    shutil.copytree(PREFIX, moved, symlinks=True)
    return moved


def test_dependency_appears(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    user = _prefix(PREFIX)
    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, dep=None, user=user),
        lambda: _set(project, dep=DEP, user=user),
    )
    assert seeded["outcome"] == "ok" and "libgreet" in seeded["ninja"]
    assert seeded["metrics"]["composition_calls"] == 1
    assert seeded["metrics"]["available_bundles"] > 0


def test_dependency_disappears(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    user = _prefix(PREFIX)
    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, dep=DEP, user=user),
        lambda: _set(project, dep=None, user=user),
    )
    assert seeded["outcome"] == "ok" and "libgreet" not in seeded["ninja"]
    # Metadata is static available data, never a seeded package answer.
    assert seeded["metrics"]["executed"] == 0
    assert not any("pkg_find" in r or "pkg_import" in r for r in seeded["metrics"]["rounds"])


def test_pin_moves_to_the_other_candidate(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    moved = _moved(tmp_path)
    other = moved / "lib" / "cmake" / "Greet" / "Greet-targets.cmake"
    first_file = PREFIX / "lib" / "cmake" / "Greet" / "Greet-targets.cmake"
    roots = _prefix(PREFIX, moved)

    def pin(path):
        return roots + f"package_pin = [['Greet::shout', {json.dumps(str(path))}]]\n"

    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, user=pin(first_file)),
        lambda: _set(project, user=pin(other)),
    )
    assert seeded["outcome"] == "ok" and f"{moved}/lib/libshoutd" in seeded["ninja"].replace(
        "<B>", str(tmp_path / "a")
    )


def test_pin_removed_gives_the_same_ambiguity(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    moved = _moved(tmp_path)
    roots = _prefix(PREFIX, moved)
    pin = (
        roots
        + f"package_pin = [['Greet::shout', {json.dumps(str(moved / 'lib/cmake/Greet/Greet-targets.cmake'))}]]\n"
    )
    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, user=pin),
        lambda: _set(project, user=roots),
    )
    assert any("ambiguous package: Greet::shout" in e for e in seeded["errors"]) or (
        "ambiguous package" in seeded["outcome"]
    )


def test_prefix_dropped(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    moved = _moved(tmp_path)
    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, user=_prefix(PREFIX, moved) + "\n"),
        lambda: _set(project, user=_prefix(moved)),
    )
    assert seeded["outcome"] == "ok" and not seeded["errors"]


def test_selected_file_changes_on_disk(tmp_path, monkeypatch, capsys):
    project = consumer(tmp_path, "")
    moved = _moved(tmp_path)
    user = _prefix(moved)
    selected = moved / "lib" / "cmake" / "Greet" / "Greet-targets.cmake"
    seeded = _compare(
        project,
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _set(project, user=user),
        lambda: selected.write_text(selected.read_text() + "\n# edited\n"),
        expected_mode="replay",
    )
    assert seeded["outcome"] == "ok" and seeded["metrics"]["executed"] == 0
    assert seeded["metrics"]["composition_calls"] == 1
