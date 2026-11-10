# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Seeding the discovery fixpoint with the previous configure's probe answers
(discovery.run_fixpoint) must not change the outcome. These fixtures also
agree on derived facts, final input facts, build.ninja and `why`; exact
answer-fact equality is not guaranteed in general (see test_fallback.py).
Also the join discipline that makes seeding sound (layers._check_answer_reads)
and the cache of the loader's Soufflé checks (souffle_cache)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from buildlog import souffle_cache
from buildlog.cli import main
from buildlog.discovery import SEED_ENV
from buildlog.engine import DRIVER_INPUTS, DRIVER_RELATIONS, configure_project
from buildlog.layers import PackageRef, check_layers, compose_program
from buildlog.paths import souffle_path

from . import host
from .test_why import GOLDEN_CASES, _golden, _why_json

ROOT = Path(__file__).resolve().parents[1]
CROSS = ROOT / "examples" / "cross"
USER = CROSS / "presets" / "apple-clang.toml"
CORE = ROOT / "src" / "buildlog" / "layers" / "core" / "manifest.toml"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def _configure(build: Path, configs=None, *, seed: bool, monkeypatch) -> dict:
    monkeypatch.setenv(SEED_ENV, "1" if seed else "0")
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    _, metrics = configure_project(CROSS, build, user_files=[USER], configs=configs)
    return metrics


def _final_in(build: Path) -> Path:
    final = json.loads((build / "metrics.json").read_text())["final_round"]
    return build / "facts-in" / f"round-{final}"


def _tree(d: Path, build: Path) -> dict[str, str | list[str]]:
    files = {}
    for f in sorted(d.rglob("*")):
        if f.is_file():
            text = f.read_text().replace(str(build), "<BUILD>")
            files[f.relative_to(d).as_posix()] = (
                sorted(text.splitlines()) if f.suffix in {".csv", ".facts"} else text
            )
    return files


def assert_same_derivation(seeded: Path, unseeded: Path) -> None:
    assert _tree(seeded / "souffle" / "facts", seeded) == _tree(
        unseeded / "souffle" / "facts", unseeded
    )
    assert _tree(_final_in(seeded), seeded) == _tree(_final_in(unseeded), unseeded)
    ninja = [
        (b / "souffle" / "build.ninja").read_text().replace(str(b), "<BUILD>")
        for b in (seeded, unseeded)
    ]
    assert ninja[0] == ninja[1]


def answers_have_requests(build: Path) -> None:
    """In this fixture every answer's request still holds in the final model."""
    requested = set()
    for f in (build / "souffle" / "facts").glob("req_*.csv"):
        requested |= {line.split("\t")[0] for line in f.read_text().splitlines()}
    for f in _final_in(build).glob("res_*.facts"):
        for line in f.read_text().splitlines():
            assert line.split("\t")[0] in requested, f"{f.name}: {line}"


@host.cross
def test_replay_converges_in_one_round(tmp_path, monkeypatch, capsys):
    a, b = tmp_path / "seeded", tmp_path / "unseeded"
    _configure(a, seed=True, monkeypatch=monkeypatch)
    warm = _configure(a, seed=True, monkeypatch=monkeypatch)
    _configure(b, seed=False, monkeypatch=monkeypatch)
    cold = _configure(b, seed=False, monkeypatch=monkeypatch)
    assert warm["seed"]["mode"] == "replay" and warm["seed"]["seeded"] > 0
    assert len(warm["rounds"]) == 1 < len(cold["rounds"])
    assert cold["seed"]["mode"] is None
    assert warm["executed"] == 0
    assert_same_derivation(a, b)
    answers_have_requests(a)
    monkeypatch.setenv(SEED_ENV, "1")
    for name, argv in sorted(GOLDEN_CASES.items()):
        _golden(name, _why_json(capsys, a, *argv))


@host.cross
def test_changed_inputs_seed_only_guarded_answers(tmp_path, monkeypatch):
    """Dropping two configurations changes the input facts: the answers for
    x86_64 and universal2 are seeded (guarded), never requested, and gone
    from the final input facts."""
    a, b = tmp_path / "seeded", tmp_path / "unseeded"
    _configure(a, seed=True, monkeypatch=monkeypatch)
    narrowed = _configure(a, ["host-asan"], seed=True, monkeypatch=monkeypatch)
    _configure(b, seed=False, monkeypatch=monkeypatch)
    _configure(b, ["host-asan"], seed=False, monkeypatch=monkeypatch)
    assert narrowed["seed"]["mode"] == "guarded"
    assert narrowed["seed"]["unused"] > 0
    assert narrowed["executed"] == 0
    assert_same_derivation(a, b)
    answers_have_requests(a)
    assert "x86_64" not in (_final_in(a) / "res_cc_capability.facts").read_text()
    # And back: the seeds are the narrowed run's answers, the new requests
    # are answered from the probe cache.
    widened = _configure(
        a, ["host-asan", "cross-release", "u2-release"], seed=True, monkeypatch=monkeypatch
    )
    _configure(b, ["host-asan", "cross-release", "u2-release"], seed=False, monkeypatch=monkeypatch)
    assert widened["seed"]["mode"] == "guarded" and widened["executed"] == 0
    assert_same_derivation(a, b)


@host.cross
def test_stale_seed_is_dropped(tmp_path, monkeypatch):
    """A recorded answer whose cache key no longer holds is not seeded."""
    a, b = tmp_path / "seeded", tmp_path / "unseeded"
    _configure(a, seed=True, monkeypatch=monkeypatch)
    data = json.loads((a / "probe-answers.json").read_text())
    (record,) = data["records"].values()
    # A leaf: no other answer's key reads it.
    i = next(i for i, e in enumerate(record["answers"]) if e[0] == "cc_warning")
    kind, req_id, args, key = record["answers"][i]
    record["answers"][i] = [kind, req_id, args, key + " "]
    (a / "probe-answers.json").write_text(json.dumps(data))
    again = _configure(a, seed=True, monkeypatch=monkeypatch)
    assert again["seed"]["mode"] == "guarded"
    assert again["seed"]["seeded"] == len(record["answers"]) - 1
    _configure(b, seed=False, monkeypatch=monkeypatch)
    assert_same_derivation(a, b)


@host.cross
def test_why_after_seeded_reconfigure(tmp_path, monkeypatch, capsys):
    a = tmp_path / "seeded"
    _configure(a, seed=True, monkeypatch=monkeypatch)
    _configure(a, ["host-asan"], seed=True, monkeypatch=monkeypatch)
    capsys.readouterr()
    fact = 'req_cc_capability(id, "apple-clang", "cxx", "x86_64-macos")'
    assert main(["query", "-C", str(a), fact, str(CROSS)]) == 0
    assert "x86_64" not in capsys.readouterr().out


# --- the join discipline ------------------------------------------------------


PROBE_MANIFEST = """
name = "probe-pkg"
version = "0.1.0"
rank = 3
layers = ["probe.dl"]
open = []
exports = ["seen"]
requires = {{ buildlog = ">=0" }}

[[probe]]
kind = "thing"
request = "req_thing"
domain = "domain_thing"
result = ["res_thing"]
entry = "buildlog_testplugins.probes:toy"
{extra}
"""


def _probe_layer(tmp_path: Path, rule: str, extra: str = "") -> Path:
    d = tmp_path / "probe-pkg"
    d.mkdir()
    (d / "manifest.toml").write_text(PROBE_MANIFEST.format(extra=extra))
    (d / "probe.dl").write_text(
        ".decl req_thing(id:symbol, x:symbol)\n"
        ".decl domain_thing(id:symbol, x:symbol)\n"
        ".decl res_thing(req:symbol, v:symbol)\n"
        ".decl seen(v:symbol)\n.decl asked(id:symbol)\n"
        'domain_thing("thing:a", "a") :- config_decl(_, _).\n'
        "req_thing(i, x) :- domain_thing(i, x).\n"
        "asked(i) :- req_thing(i, _).\n" + rule
    )
    return d / "manifest.toml"


def _read_errors(tmp_path: Path, manifest: Path) -> list[str]:
    refs = [PackageRef(CORE, "core"), PackageRef(manifest, "plugin")]
    return [e for e in check_layers(refs, tmp_path / "check") if "without its request" in e]


@pytest.mark.parametrize(
    "rule",
    [
        "seen(v) :- res_thing(_, v).\n",
        "seen(v) :- res_thing(i, v), domain_thing(i, _).\n",
        "seen(v) :- res_thing(i, v), req_thing(j, _), i = j.\n",
        "seen(v) :- config_decl(v, _), !res_thing(_, v).\n",
        "seen(v) :- res_thing(i, v), asked(i) ; res_thing(_, v).\n",
    ],
)
def test_unguarded_answer_read_is_an_error(tmp_path, rule):
    errors = _read_errors(tmp_path, _probe_layer(tmp_path, rule))
    assert any("res_thing is read without its request" in e for e in errors), errors
    assert all("probe.dl:" in e for e in errors)


@pytest.mark.parametrize(
    "rule",
    [
        "seen(v) :- res_thing(i, v), req_thing(i, _).\n",
        "seen(v) :- asked(i), res_thing(i, v).\n",
        "seen(v) :- config_decl(v, _), req_thing(i, _), !res_thing(i, v).\n",
    ],
)
def test_guarded_answer_read_is_accepted(tmp_path, rule):
    assert _read_errors(tmp_path, _probe_layer(tmp_path, rule)) == []


def test_unguarded_probe_states_why(tmp_path):
    rule = "seen(v) :- res_thing(_, v).\n"
    ok = _probe_layer(tmp_path, rule, 'unguarded = "read below its request"')
    assert _read_errors(tmp_path, ok) == []


# --- the cache of the loader's Soufflé checks ---------------------------------


def test_layer_check_cache_is_shared_and_keeps_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    calls = []
    real = souffle_cache.subprocess.run

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(souffle_cache.subprocess, "run", counting)
    bad = "seen(v) :- res_thing(i, v), req_thing(i, _), v = 1.\n"
    layer = _probe_layer(tmp_path, bad)
    refs = [PackageRef(CORE, "core"), PackageRef(layer, "plugin")]

    def check(d: Path) -> list[str]:
        return compose_program(refs, d / "p", d / "w", DRIVER_RELATIONS, DRIVER_INPUTS).errors

    first = check(tmp_path / "one")
    n = len(calls)
    assert n > 0 and any("composed program" in e for e in first)
    stored = [f.read_text() for f in (tmp_path / "cache").rglob("*.json")]
    assert stored and not any(str(tmp_path / "one") in t for t in stored)
    second = check(tmp_path / "two")
    assert len(calls) == n
    assert second == [e.replace("/one/", "/two/") for e in first]
    (layer.parent / "probe.dl").write_text(
        (layer.parent / "probe.dl").read_text().replace("v = 1", 'v = "1"')
    )
    assert check(tmp_path / "three") == []
    assert len(calls) > n
    monkeypatch.setenv(souffle_cache.CACHE_ENV, "off")
    m = len(calls)
    check(tmp_path / "four")
    assert len(calls) > m


@host.cross
def test_probe_pending_empties_only_in_the_final_round(tmp_path, monkeypatch):
    """Error propagation waits for discovery_complete (no probe_pending): the
    early rounds still have requests, the final round has none. (Early rounds
    write only the relations discovery reads, unless deferral is off.)"""
    monkeypatch.setenv("BUILDLOG_SOUFFLE_DEFER", "0")
    metrics = _configure(tmp_path, seed=False, monkeypatch=monkeypatch)
    pending = [
        (tmp_path / "rounds" / f"round-{n}" / "probe_pending.csv").read_text().split()
        for n in range(len(metrics["rounds"]))
    ]
    assert all(pending[:-1]) and not pending[-1]


@host.cross
def test_fresh_configure_reads_no_cache_and_seed(tmp_path, monkeypatch):
    """`fresh` executes every probe request (no seed, no cache row read) and
    derives what a build directory without a cache derives."""
    a, b = tmp_path / "warm", tmp_path / "cold"
    warm = _configure(a, seed=True, monkeypatch=monkeypatch)
    again = _configure(a, seed=True, monkeypatch=monkeypatch)
    assert again["executed"] == 0 and again["seed"]["seeded"] > 0
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    _, fresh = configure_project(CROSS, a, user_files=[USER], fresh=True)
    assert fresh["cache_hits"] == 0 and fresh["seed"]["seeded"] == 0
    assert fresh["executed"] == warm["executed"]
    _configure(b, seed=False, monkeypatch=monkeypatch)
    assert_same_derivation(a, b)
