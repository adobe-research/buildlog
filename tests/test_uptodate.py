# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The up-to-date check (uptodate): an unchanged configure is skipped (no
composition, no Soufflé), and any change to what it read forces a full one."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from buildlog import discovery, engine, uptodate
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
TRIVIAL = ROOT / "tests" / "projects" / "trivial"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(sys.platform != "darwin", reason="the trivial project pins Apple clang"),
]


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv(uptodate.ENV, "1")


class Project:
    def __init__(self, tmp_path):
        self.dir = tmp_path / "proj"
        shutil.copytree(TRIVIAL, self.dir)
        toml = self.dir / "buildlog.toml"
        toml.write_text(toml.read_text().replace('"${REPO_ROOT}/tests/projects/trivial"', '"."'))
        self.preset = self.dir / "presets" / "apple-clang-arm64.toml"
        self.build = tmp_path / "build"
        self.ninja = self.build / "souffle" / "build.ninja"

    def configure(self, **kw):
        kw.setdefault("user_files", [self.preset])
        return configure_project(self.dir, self.build, **kw)[1]


@pytest.fixture
def p(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDLOG_CACHE_DIR", str(tmp_path / "cache"))
    proj = Project(tmp_path)
    cold = proj.configure()
    assert not cold.get("up_to_date") and cold["executed"] > 0
    return proj


def forbid(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a skipped configure composed or ran Souffle")

    monkeypatch.setattr(engine, "compose_program", boom)
    monkeypatch.setattr(engine.SouffleRounds, "__init__", boom)


def full(p):
    """Whether a rerun is a full configure (it composes and starts Souffle)."""
    return not p.configure().get("up_to_date")


def test_unchanged_rerun_skips_and_leaves_the_outputs(p, monkeypatch, capsys):
    before = p.ninja.read_bytes(), p.ninja.stat().st_mtime_ns
    facts = {f.name: f.stat().st_mtime_ns for f in (p.build / "souffle" / "facts").iterdir()}
    forbid(monkeypatch)
    for _ in range(2):
        metrics = p.configure()
        assert metrics["up_to_date"] and metrics["executed"] == 0
        assert (p.ninja.read_bytes(), p.ninja.stat().st_mtime_ns) == before
    assert facts == {
        f.name: f.stat().st_mtime_ns for f in (p.build / "souffle" / "facts").iterdir()
    }
    assert json.loads((p.build / "metrics.json").read_text())["up_to_date"] is True


def test_spec_edit_forces_a_full_configure(p):
    spec = p.dir / "spec.toml"
    spec.write_text(spec.read_text() + "\n# edited\n")
    assert full(p)
    assert p.configure()["up_to_date"]


def test_preset_edit_forces_a_full_configure(p):
    p.preset.write_text(p.preset.read_text() + "\n# edited\n")
    assert full(p)


def test_lock_edit_forces_a_full_configure(p):
    lock = p.dir / "buildlog.lock"
    lock.write_text(lock.read_text() + "\n# edited\n")
    assert full(p)


def test_declared_variable_in_the_environment_forces_a_full_configure(p, monkeypatch):
    monkeypatch.setenv("BUILDLOG_SOMETHING", "1")
    assert full(p)


def test_cli_options_force_a_full_configure(p):
    assert not p.configure(var_overrides={"X": "1"}).get("up_to_date")
    assert p.configure(var_overrides={"X": "1"}).get("up_to_date")
    assert not p.configure(keep_going=True).get("up_to_date")
    assert not p.configure(configs=["release"]).get("up_to_date")
    assert not p.configure(package_paths=[p.dir / "none"]).get("up_to_date")


def test_user_files_change_forces_a_full_configure(p):
    other = p.dir / "presets" / "other.toml"
    other.write_text(p.preset.read_text())
    assert not p.configure(user_files=[other]).get("up_to_date")


def test_tool_environment_forces_a_full_configure(p, monkeypatch, tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    assert full(p)
    assert p.configure()["up_to_date"]


def test_a_changed_watch_set_forces_a_full_configure(p, tmp_path):
    """The watch sets the probes record (a tool binary, a header) are checked."""
    watched = tmp_path / "tool"
    watched.write_text("v1")
    cache_path = p.build / "probe-cache.json"
    cache = json.loads(cache_path.read_text())
    record = uptodate.pickle.loads((p.build / uptodate.FILE).read_bytes())
    key = record["answers"][0][3]
    cache[key]["__watch__"] = [discovery.watch_identity(str(watched))]
    cache_path.write_text(json.dumps(cache))
    record["answers"][0][4] = uptodate._jsha(cache[key])
    (p.build / uptodate.FILE).write_bytes(uptodate.pickle.dumps(record))
    assert p.configure()["up_to_date"]
    watched.write_text("v2 longer")
    assert full(p)


@pytest.mark.parametrize("what", ["build.ninja", "facts", "tests", "record", "program"])
def test_damaged_outputs_force_a_full_configure(p, what):
    if what == "build.ninja":
        p.ninja.unlink()
    elif what == "facts":
        (p.build / "souffle" / "facts" / "target.csv").write_text("x\n")
    elif what == "tests":
        (p.build / "souffle" / "tests.json").write_text("{}")
    elif what == "record":
        (p.build / uptodate.FILE).write_bytes(b"garbage")
    else:
        (p.build / "probe-cache.json").write_text("{}")
    assert full(p)
    assert p.ninja.exists()


def test_a_kind_without_a_fingerprint_is_never_skipped(p, monkeypatch):
    from dataclasses import replace

    real = engine.kinds_from_probes

    def unfingerprinted(probes):
        kinds = real(probes)
        return [replace(k, fingerprint=None) if k.name == kinds[0].name else k for k in kinds]

    monkeypatch.setattr(engine, "kinds_from_probes", unfingerprinted)
    p.configure(var_overrides={"Y": "1"})
    assert not (p.build / uptodate.FILE).exists()
    assert full(p)


def test_a_check_run_invalidates_the_record(p):
    configure_project(p.dir, p.build, emit=False, check_only=True, user_files=[p.preset])
    assert not (p.build / uptodate.FILE).exists()
    assert full(p)


def test_fresh_and_update_lock_run_in_full(p):
    assert not p.configure(fresh=True).get("up_to_date")
    assert not p.configure(update_lock=True).get("up_to_date")


def test_the_status_lines_are_printed_again(p, capsys):
    data = uptodate.pickle.loads((p.build / uptodate.FILE).read_bytes())
    data["printed"] = ["warning: demo: a warning"]
    (p.build / uptodate.FILE).write_bytes(uptodate.pickle.dumps(data))
    capsys.readouterr()
    assert p.configure()["up_to_date"]
    assert "warning: demo: a warning" in capsys.readouterr().err


def test_touched_inputs_touch_the_build_file_so_ninja_settles(p):
    spec = p.dir / "spec.toml"
    later = p.ninja.stat().st_mtime + 0.001
    os.utime(spec, (later, later))
    assert p.configure()["up_to_date"]
    assert p.ninja.stat().st_mtime >= later


def test_opt_out(p, monkeypatch):
    monkeypatch.setenv(uptodate.ENV, "0")
    assert full(p)
    assert not (p.build / uptodate.FILE).exists()
