# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""How the driver runs Soufflé's rounds (engine.SouffleRounds) must not change
what is derived: one `souffle --serve` process per fixpoint against a run per
round (BUILDLOG_SOUFFLE_SERVE=0), and the round memo (a round whose inputs
equal an earlier round's reuses its outputs) against evaluating every round
(BUILDLOG_ROUND_MEMO=0)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from buildlog import engine, layers, souffle_cache
from buildlog.engine import ROUND_MEMO_ENV, SERVE_ENV, SouffleRounds, configure_project
from buildlog.errors import LoaderError
from buildlog.paths import souffle_path

from .host import CROSS_REASON, MACOS
from .test_loader import make_project
from .test_probe_seeding import CROSS, USER, assert_same_derivation

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not MACOS, reason=CROSS_REASON
    ),  # examples/cross configures x86_64-macos, universal2-macos
]


def _configure(build: Path, monkeypatch, *, serve: str = "1", memo: str = "1") -> list:
    monkeypatch.setenv(SERVE_ENV, serve)
    monkeypatch.setenv(ROUND_MEMO_ENV, memo)
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    engine.SOUFFLE_TIMES.clear()
    configure_project(CROSS, build, user_files=[USER])
    return [purpose for purpose, _ in engine.SOUFFLE_TIMES]


@pytest.mark.integration
def test_serve_and_memo_derive_what_separate_runs_do(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    calls = _configure(plain, monkeypatch, serve="0", memo="0")
    assert "translate" not in calls and "reused" not in calls
    served = tmp_path / "served"
    calls = _configure(served, monkeypatch)
    assert calls.count("translate") >= 1 and "reused" not in calls
    assert_same_derivation(served, plain)
    # Warm: round 0's inputs are the last configure's final round's.
    assert _configure(served, monkeypatch) == ["reused"]
    assert_same_derivation(served, plain)
    assert "reused" not in _configure(served, monkeypatch, memo="0")
    assert_same_derivation(served, plain)


@pytest.mark.integration
def test_memo_ignores_changed_outputs_and_another_program(tmp_path, monkeypatch):
    build = tmp_path / "b"
    _configure(build, monkeypatch)
    plain = tmp_path / "plain"
    _configure(plain, monkeypatch, serve="0", memo="0")
    # An output touched since it was written is evaluated again.
    final_round = json.loads((build / "metrics.json").read_text())["final_round"]
    final = build / "rounds" / f"round-{final_round}"
    assert (build / "rounds" / f"round-{final_round}.key").exists()
    victim = min(final.iterdir())
    victim.write_text(victim.read_text() + "\n")
    calls = _configure(build, monkeypatch)
    assert "reused" not in calls
    assert_same_derivation(build, plain)
    # Same facts, another program digest: no reuse.
    rounds = SouffleRounds(build / "program" / "build.dl", "another")
    out = build / "rounds" / "round-0"
    facts = build / "facts-in" / "round-0"
    assert not rounds._reuse(rounds._inputs_key(facts), out)


def _dir_bytes(d: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in d.iterdir()}


def test_materialization_reuses_and_invalidates_source_map(tmp_path):
    env = tmp_path / "env"
    env.mkdir()
    schema = SimpleNamespace(inputs=set())
    static = ({}, {"static": ["spec:1"]}, {})
    first = tmp_path / "facts" / "round-0"
    static, previous = engine.materialize_static(tmp_path, schema, env, first, static=static)
    before = (first / "source_map.json").stat().st_mtime_ns
    second = first.with_name("round-1")
    static, previous = engine.materialize_static(
        tmp_path, schema, env, second, static=static, previous=previous
    )
    assert (second / "source_map.json").stat().st_mtime_ns == before
    (env / "source_map.json").write_text('{"probe": ["import:2"]}\n')
    third = first.with_name("round-2")
    static, previous = engine.materialize_static(
        tmp_path, schema, env, third, static=static, previous=previous
    )
    assert json.loads((third / "source_map.json").read_text()) == {
        "static": ["spec:1"],
        "probe": ["import:2"],
    }
    (third / "source_map.json").unlink()
    fourth = first.with_name("round-3")
    static, previous = engine.materialize_static(
        tmp_path, schema, env, fourth, static=static, previous=previous
    )
    assert json.loads((fourth / "source_map.json").read_text()) == {
        "static": ["spec:1"],
        "probe": ["import:2"],
    }
    carried = first.with_name("next-pass")
    engine.materialize_static(
        tmp_path,
        schema,
        env,
        carried,
        static=({}, {"different": ["spec:3"]}, {}),
        carried=(static, previous),
    )
    assert json.loads((carried / "source_map.json").read_text()) == {
        "different": ["spec:3"],
        "probe": ["import:2"],
    }


@pytest.mark.integration
def test_round_dir_is_complete_and_only_the_last_round_remains(tmp_path, monkeypatch):
    """facts-in holds the final round's dir alone, without empty files, and
    its files equal those of a dir written in one go (a warm configure's
    round 0 is the same facts, written from nothing)."""
    build = tmp_path / "b"
    _configure(build, monkeypatch)
    final_round = json.loads((build / "metrics.json").read_text())["final_round"]
    assert final_round > 0
    assert [d.name for d in (build / "facts-in").iterdir()] == [f"round-{final_round}"]
    incremental = _dir_bytes(build / "facts-in" / f"round-{final_round}")
    assert incremental and all(
        data or name == "source_map.json" for name, data in incremental.items()
    )
    _configure(build, monkeypatch)
    assert json.loads((build / "metrics.json").read_text())["final_round"] == 0
    assert _dir_bytes(build / "facts-in" / "round-0") == incremental


def test_configure_reuses_startup_checks_but_standalone_composition_still_checks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SERVE_ENV, "1")
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')
    calls = []
    real_run = engine.subprocess.run

    def counting(argv, *args, **kwargs):
        calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", counting)
    engine.SOUFFLE_TIMES.clear()
    configure_project(project, tmp_path / "build", emit=False)
    deferred = engine._defer_supported(engine.require_souffle())
    assert [purpose for purpose, _ in engine.SOUFFLE_TIMES] == [
        "translate",
        "round",
        *(["dump"] if deferred else []),
    ]
    assert not any("--show=type-analysis" in argv for argv in calls)
    engine.SOUFFLE_TIMES.clear()
    configure_project(project, tmp_path / "build", emit=False)
    assert engine.SOUFFLE_TIMES == [("reused", 0.0)]
    assert not any("--show=type-analysis" in argv for argv in calls)

    program = layers.compose_program(
        [p.ref for p in engine.project_packages(project)],
        tmp_path / "standalone/program",
        tmp_path / "standalone/ast",
        required_outputs=engine.DRIVER_RELATIONS,
        required_inputs=engine.DRIVER_INPUTS,
    )
    assert program.errors == []
    assert sum("--show=type-analysis" in argv for argv in calls) == 1


@pytest.mark.parametrize("serve", ["0", "1"])
@pytest.mark.parametrize(
    "rule",
    [
        ".decl value(x:symbol)\nvalue(1).\n",
        "target(t) :- needed(t, _).\n",
    ],
)
def test_composed_errors_are_reported_before_discovery_and_cached(
    tmp_path, monkeypatch, serve, rule
):
    monkeypatch.setenv(SERVE_ENV, serve)
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')
    config = project / "buildlog.toml"
    config.write_text(config.read_text().replace('["spec.toml"]', '["spec.toml", "rules.dl"]'))
    rules = project / "rules.dl"
    rules.write_text(rule)
    started = []
    real_popen = engine.subprocess.Popen

    def counting(argv, *args, **kwargs):
        proc = real_popen(argv, *args, **kwargs)
        if "--serve" in argv:
            started.append(proc)
        return proc

    monkeypatch.setattr(engine.subprocess, "Popen", counting)
    errors = []
    for name in ("one", "two"):
        build = tmp_path / name
        with pytest.raises(LoaderError) as exc:
            configure_project(project, build, emit=False)
        errors.append(exc.value.errors)
        prefix = (
            "declaration/selection stratum depends on demand or condition truth:"
            if rule.startswith("target(")
            else "composed program:"
        )
        assert any(e.startswith(prefix) for e in exc.value.errors)
        assert not (build / "env-facts").exists()
        assert not (build / "rounds").exists()
        assert not (project / "buildlog.lock").exists()
    assert errors[1] == [e.replace("/one/", "/two/") for e in errors[0]]
    checked_startups = int(serve == "1" and not rule.startswith("target("))
    assert len(started) == checked_startups
    assert all(proc.poll() is not None for proc in started)

    rules.write_text('.decl value(x:symbol)\nvalue("1").\n')
    configure_project(project, tmp_path / "valid", emit=False)
    assert len(started) == checked_startups + int(serve == "1")


def test_started_evaluator_is_closed_when_fact_setup_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(SERVE_ENV, "1")
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')
    started = []
    real_start = SouffleRounds._start

    def starting(self, *args):
        real_start(self, *args)
        started.append(self.proc)

    def fail_schema(*args):
        raise LoaderError(["injected fact setup failure"])

    monkeypatch.setattr(SouffleRounds, "_start", starting)
    monkeypatch.setattr(engine, "schema_from_rules", fail_schema)
    with pytest.raises(LoaderError, match="injected fact setup failure"):
        configure_project(project, tmp_path / "build", emit=False)
    assert len(started) == 1 and started[0].poll() == 0
    assert all(pipe.closed for pipe in (started[0].stdin, started[0].stdout, started[0].stderr))


def test_binary_without_serve_retains_the_composed_check(tmp_path, monkeypatch):
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    monkeypatch.setattr(engine, "_serve_supported", lambda binary: False)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')
    calls = []
    real_run = engine.subprocess.run

    def counting(argv, *args, **kwargs):
        calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", counting)
    configure_project(project, tmp_path / "build", emit=False)
    assert sum("--show=type-analysis" in argv for argv in calls) == 1
    assert not any("--serve" in argv for argv in calls)
