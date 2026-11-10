# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The discovery driver's probe contract (discovery.run_fixpoint), on a
synthetic program: every request gets an answer (an empty one is an error),
and every probe of a round reads the answers as they were at the start of
the round, so the order of the probes cannot change the result."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from buildlog.discovery import Kind, run_fixpoint
from buildlog.facts import read_rows, write_rows


def _kind(name: str, fn) -> Kind:
    return Kind(name, f"req_{name}", f"dom_{name}", (f"res_{name}",), "test:fn", fn)


def _cached(kind: Kind) -> Kind:
    """The kind with a constant fingerprint: cacheable and seedable."""
    return replace(kind, fingerprint=lambda args, results: "")


def _program(requests: dict[str, list[tuple[str, ...]]]):
    """A derive_round that makes the same requests every round and copies
    the probe answers it is given to its output."""

    def materialize(env_dir: Path, round_no: int) -> Path:
        return env_dir

    def derive_round(facts_in: Path, round_no: int) -> Path:
        out = facts_in.parent / "out" / f"round-{round_no}"
        out.mkdir(parents=True, exist_ok=True)
        for name, rows in requests.items():
            write_rows(out / f"req_{name}.csv", rows)
            write_rows(out / f"dom_{name}.csv", rows)
        for f in facts_in.glob("*.facts"):
            write_rows(out / f"{f.stem}.csv", read_rows(f))
        return out

    return derive_round, materialize


def _answers(build: Path) -> dict[str, list]:
    return {f.stem: sorted(read_rows(f)) for f in sorted((build / "env-facts").glob("*.facts"))}


def test_empty_answer_is_an_error_naming_probe_and_request(tmp_path):
    derive, mat = _program({"silent": [("r1", "libfoo")]})
    kind = _kind("silent", lambda ctx, req, args, results: {})
    with pytest.raises(SystemExit) as exc:
        run_fixpoint(tmp_path, derive, mat, [kind])
    msg = str(exc.value)
    assert "probe silent gave no answer to request r1 (libfoo)" in msg
    assert "res_silent" in msg
    # Nothing was cached, so a fixed probe is asked again.
    assert not (tmp_path / "probe-cache.json").exists()


def test_rows_for_another_request_are_not_an_answer(tmp_path):
    derive, mat = _program({"wrong": [("r1", "x")]})
    kind = _kind("wrong", lambda ctx, req, args, results: {"res_wrong": [("r2", "y")]})
    with pytest.raises(SystemExit, match="probe wrong gave no answer to request r1"):
        run_fixpoint(tmp_path, derive, mat, [kind])


def test_a_negative_answer_is_a_row_and_terminates(tmp_path):
    derive, mat = _program({"find": [("r1", "libfoo")]})
    kind = _kind("find", lambda ctx, req, args, results: {"res_find": [(req, "not-found")]})
    _, metrics = run_fixpoint(tmp_path, derive, mat, [kind])
    assert len(metrics["rounds"]) == 2 and metrics["executed"] == 1
    assert _answers(tmp_path) == {"res_find": [("r1", "not-found")]}


def test_a_cached_empty_answer_is_asked_again(tmp_path):
    """A cache written by a driver that accepted empty answers."""
    derive, mat = _program({"find": [("r1", "libfoo")]})
    kind = _kind("find", lambda ctx, req, args, results: {"res_find": [(req, "found")]})
    key = json.dumps(["find", ["libfoo"], ""], sort_keys=True)
    (tmp_path / "probe-cache.json").write_text(json.dumps({key: {}}))
    _, metrics = run_fixpoint(tmp_path, derive, mat, [kind])
    assert metrics["executed"] == 1 and metrics["cache_hits"] == 0
    assert _answers(tmp_path) == {"res_find": [("r1", "found")]}


def _peeking_kinds() -> list[Kind]:
    """Two probes asked in the same round; each reports whether it saw the
    other's answer."""

    def peek(name: str, other: str):
        def fn(ctx, req, args, results):
            with pytest.raises((TypeError, AttributeError)):
                results[f"res_{name}"] = set()  # read-only
            saw = "saw" if results.get(f"res_{other}") else "none"
            return {f"res_{name}": [(req, saw)]}

        return fn

    return [_kind("a", peek("a", "b")), _kind("b", peek("b", "a"))]


@pytest.mark.parametrize("shuffle_seed", [None, 1, 2, 3])
def test_probe_order_within_a_round_does_not_matter(tmp_path, shuffle_seed):
    requests = {"a": [("ra", "x")], "b": [("rb", "y")]}
    results = []
    for i, kinds in enumerate([_peeking_kinds(), list(reversed(_peeking_kinds()))]):
        build = tmp_path / str(i)
        derive, mat = _program(requests)
        run_fixpoint(build, derive, mat, kinds, shuffle_seed=shuffle_seed)
        results.append(_answers(build))
    assert results[0] == results[1] == {"res_a": [("ra", "none")], "res_b": [("rb", "none")]}


@pytest.mark.parametrize("shuffle_seed", [None, 1, 2, 3, 4])
def test_all_check_failures_of_a_round_are_reported_in_canonical_order(tmp_path, shuffle_seed):
    requests = {"b": [("rb", "y"), ("rb2", "z")], "a": [("ra", "x")], "c": [("rc", "w")]}
    messages = []
    for i, order in enumerate([("a", "b", "c"), ("c", "b", "a")]):
        kinds = [
            _kind(
                n,
                (lambda ctx, req, args, results: {})
                if n in {"a", "b"}
                else (lambda ctx, req, args, results: {"res_c": [(req, "ok")], "extra": []}),
            )
            for n in order
        ]
        derive, mat = _program(requests)
        with pytest.raises(SystemExit) as exc:
            run_fixpoint(tmp_path / str(i), derive, mat, kinds, shuffle_seed=shuffle_seed)
        messages.append(str(exc.value))
    assert messages[0] == messages[1]
    lines = messages[0].splitlines()
    assert lines[0] == "4 driver check failures in this round:"
    assert lines[1].startswith("[1] probe c returned undeclared relations: extra")
    assert lines[2].startswith("[2] probe a gave no answer to request ra (x)")
    assert lines[3].startswith("[3] probe b gave no answer to request rb (y)")
    assert lines[4].startswith("[4] probe b gave no answer to request rb2 (z)")


def test_kinds_sharing_a_request_id_are_both_answered(tmp_path):
    derive, mat = _program({"a": [("r1", "x")], "b": [("r1", "y")]})
    kinds = [
        _kind(n, lambda ctx, req, args, results, n=n: {f"res_{n}": [(req, n + args[0])]})
        for n in ("a", "b")
    ]
    _, metrics = run_fixpoint(tmp_path, derive, mat, kinds)
    assert metrics["executed"] == 2 and len(metrics["rounds"]) == 2
    assert _answers(tmp_path) == {"res_a": [("r1", "ax")], "res_b": [("r1", "by")]}


def test_same_id_with_two_argument_tuples_in_one_kind_is_an_error(tmp_path):
    derive, mat = _program({"a": [("r1", "x"), ("r1", "y")]})
    kind = _kind("a", lambda ctx, req, args, results: {"res_a": [(req, "ok")]})
    with pytest.raises(SystemExit, match="request r1 of a has two argument tuples"):
        run_fixpoint(tmp_path, derive, mat, [kind])


def test_shared_ids_replay_in_one_round_and_old_records_miss(tmp_path):
    requests = {"a": [("r1", "x")], "b": [("r1", "y")]}
    kinds = [
        _cached(_kind(n, lambda ctx, req, args, results, n=n: {f"res_{n}": [(req, n + args[0])]}))
        for n in ("a", "b")
    ]
    derive, mat = _program(requests)
    run_fixpoint(tmp_path, derive, mat, kinds, program_digest="p")
    derive, mat = _program(requests)
    _, metrics = run_fixpoint(tmp_path, derive, mat, kinds, program_digest="p")
    assert len(metrics["rounds"]) == 1 and metrics["executed"] == 0
    assert metrics["seed"]["seeded"] == 2
    assert _answers(tmp_path) == {"res_a": [("r1", "ax")], "res_b": [("r1", "by")]}
    # Records of an older format are ignored, not trusted or fatal.
    (tmp_path / "probe-answers.json").write_text(
        json.dumps({"records": {"p": {"program": "p", "answers": [["a", "r1"], "junk", 3]}}})
    )
    derive, mat = _program(requests)
    _, metrics = run_fixpoint(tmp_path, derive, mat, kinds, program_digest="p")
    assert metrics["seed"]["seeded"] == 0
    assert _answers(tmp_path) == {"res_a": [("r1", "ax")], "res_b": [("r1", "by")]}


def test_a_kind_without_fingerprint_is_never_cached_or_seeded(tmp_path):
    derive, mat = _program({"find": [("r1", "libfoo")]})
    runs = []

    def fn(ctx, req, args, results):
        runs.append(req)
        return {"res_find": [(req, "found")]}

    kind = _kind("find", fn)
    for _ in range(2):
        _, metrics = run_fixpoint(tmp_path, derive, mat, [kind], program_digest="p")
        assert metrics["executed"] == 1 and metrics["cache_hits"] == 0
        assert metrics["seed"]["seeded"] == 0
    assert len(runs) == 2
    cache = tmp_path / "probe-cache.json"
    assert not cache.exists() or json.loads(cache.read_text()) == {}


def test_a_fingerprinted_kind_is_cached_until_its_fingerprint_changes(tmp_path):
    derive, mat = _program({"find": [("r1", "libfoo")]})
    state = {"fp": "v1", "runs": 0}

    def fn(ctx, req, args, results):
        state["runs"] += 1
        return {"res_find": [(req, "found")]}

    kind = Kind(
        "find", "req_find", "dom_find", ("res_find",), "test:fn", fn, lambda a, r: state["fp"]
    )
    for fp, expect_runs in (("v1", 1), ("v1", 1), ("v2", 2)):
        state["fp"] = fp
        run_fixpoint(tmp_path, derive, mat, [kind], program_digest="p")
        assert state["runs"] == expect_runs


def _ans(kind, rows, origins=()):
    from buildlog.discovery import ORIGIN_KEY, Answer

    rows_by_rel = {kind.results[0]: list(rows)}
    if origins:
        rows_by_rel[ORIGIN_KEY] = list(origins)
    return Answer(kind, (), None, rows_by_rel, True)


def _env_files(env):
    return {f.name: f.read_bytes() for f in sorted(env.iterdir())}


def _count_writes(monkeypatch):
    from buildlog import discovery

    writes = []
    real = discovery.write_rows
    monkeypatch.setattr(
        discovery,
        "write_rows",
        lambda path, rows, *a, **k: (writes.append(path.name), real(path, rows, *a, **k))[1],
    )
    return writes


def test_write_env_state_skips_unchanged_files_and_matches_stateless(tmp_path, monkeypatch):
    from buildlog.discovery import EnvWriteState, write_env

    kind = _kind("a", None)
    kind_b = _kind("b", None)
    env, ref = tmp_path / "env", tmp_path / "ref"
    env.mkdir()
    ref.mkdir()
    state = EnvWriteState()
    writes = _count_writes(monkeypatch)
    o1 = [("res_a", ("r1", "x"), "f:1")]
    a1 = {("a", "r1"): _ans(kind, [("r1", "x")], o1)}
    b1 = {("b", "r1"): _ans(kind_b, [("r1", "y")])}
    steps = [
        {**a1},
        {**a1, **b1},  # new relation only
        {**a1, **b1},  # nothing changed
        {**a1, ("a", "r2"): _ans(kind, [("r2", "z"), ("r1", "x")])},  # overlap + new row
        {
            **a1,
            ("a", "r2"): _ans(kind, [("r2", "z")], [("res_a", ("r1", "x"), "g:2")]),
        },  # provenance
        {("a", "r2"): _ans(kind, [])},  # dropped answers/relation, empty touched relation
        {},
    ]
    counts = []
    for answers in steps:
        before = len(writes)
        write_env(env, answers, state)
        counts.append(len(writes) - before)
        write_env(ref, answers)
        assert _env_files(env) == _env_files(ref)
    # res_a once, res_b once; second round writes only res_b; third nothing
    assert counts[:3] == [1, 1, 0]
    assert _env_files(env) == {}


def test_write_env_state_keeps_identity_of_unchanged_files(tmp_path):
    from buildlog.discovery import EnvWriteState, write_env

    kind = _kind("a", None)
    state = EnvWriteState()
    a1 = {("a", "r1"): _ans(kind, [("r1", "x")], [("res_a", ("r1", "x"), "f:1")])}
    write_env(tmp_path, a1, state)
    ino = {f.name: f.stat().st_ino for f in tmp_path.iterdir()}
    mtimes = {f.name: f.stat().st_mtime_ns for f in tmp_path.iterdir()}
    write_env(tmp_path, dict(a1), state)
    assert ino == {f.name: f.stat().st_ino for f in tmp_path.iterdir()}
    assert mtimes == {f.name: f.stat().st_mtime_ns for f in tmp_path.iterdir()}
    # an origin-only change rewrites source_map.json, not the facts
    a2 = {("a", "r1"): _ans(kind, [("r1", "x")], [("res_a", ("r1", "x"), "g:9")])}
    write_env(tmp_path, a2, state)
    assert ino["res_a.facts"] == (tmp_path / "res_a.facts").stat().st_ino
    assert "g:9" in (tmp_path / "source_map.json").read_text()
    # a deleted file is restored
    (tmp_path / "res_a.facts").unlink()
    write_env(tmp_path, a2, state)
    assert read_rows(tmp_path / "res_a.facts") == [("r1", "x")]


def test_write_env_rejection_resets_state_and_stays_stateless_equivalent(tmp_path):
    from buildlog.discovery import EnvWriteState, write_env
    from buildlog.errors import SpecError

    kind = _kind("a", None)
    state = EnvWriteState()
    good = {("a", "r1"): _ans(kind, [("r1", "x")])}
    write_env(tmp_path, good, state)
    bad = {("a", "r1"): _ans(kind, [("r1", "x\ty")])}
    with pytest.raises(SpecError):
        write_env(tmp_path, bad, state)
    assert state.rels is None
    write_env(tmp_path, good, state)
    assert read_rows(tmp_path / "res_a.facts") == [("r1", "x")]


def test_write_env_reuses_origin_keys_and_unchanged_serialization(tmp_path, monkeypatch):
    from buildlog import discovery
    from buildlog.discovery import EnvWriteState, write_env

    keys, dumps = [], []
    real_key, real_dumps = discovery.tuple_key, discovery.json.dumps
    monkeypatch.setattr(
        discovery, "tuple_key", lambda *args: (keys.append(args), real_key(*args))[1]
    )
    monkeypatch.setattr(
        discovery.json,
        "dumps",
        lambda *args, **kwargs: (dumps.append(1), real_dumps(*args, **kwargs))[1],
    )
    kind = _kind("a", None)
    state = EnvWriteState()
    answers = {("a", "r1"): _ans(kind, [("r1", "x")], [("res_a", ("r1", "x"), "f:1")])}
    write_env(tmp_path, answers, state)
    first = (len(keys), len(dumps))
    write_env(tmp_path, answers, state)
    assert (len(keys), len(dumps)) == first == (1, 2)
    (tmp_path / "source_map.json").unlink()
    write_env(tmp_path, answers, state)
    assert "f:1" in (tmp_path / "source_map.json").read_text()


def test_run_fixpoint_uses_fresh_state_and_writes_each_relation_once(tmp_path, monkeypatch):
    writes = _count_writes(monkeypatch)
    derive, mat = _program({"a": [("r1",)], "b": [("r1",)]})
    ka = _kind("a", lambda ctx, req, args, results: {"res_a": [(req, "1")]})
    kb = _kind("b", lambda ctx, req, args, results: {"res_b": [(req, "2")]})
    run_fixpoint(tmp_path, derive, mat, [ka, kb])
    assert sorted(n for n in writes if n.startswith("res_")) == ["res_a.facts", "res_b.facts"]
    writes.clear()
    # a rerun starts from a cleared env-facts dir, so nothing is skipped
    run_fixpoint(tmp_path, derive, mat, [ka, kb])
    assert sorted(n for n in writes if n.startswith("res_")) == ["res_a.facts", "res_b.facts"]
    assert _answers(tmp_path)["res_a"] == [("r1", "1")]


def _stale_cache(build: Path) -> None:
    key = json.dumps(["find", ["libfoo"], ""], sort_keys=True)
    (build / "probe-cache.json").write_text(json.dumps({key: {"res_find": [["r1", "yes"]]}}))


def test_fresh_run_ignores_a_stale_cache_row(tmp_path):
    """One guarded constant request; the probe said "yes" in an old
    environment and says "no" now; constant fingerprint, no watches. The
    cached run reuses "yes"; a fresh run asks the probe and gets "no"."""
    derive, mat = _program({"find": [("r1", "libfoo")]})
    kind = _cached(_kind("find", lambda ctx, req, args, results: {"res_find": [(req, "no")]}))

    cached, fresh = tmp_path / "cached", tmp_path / "fresh"
    for b in (cached, fresh):
        b.mkdir()
        _stale_cache(b)
    _, m = run_fixpoint(cached, derive, mat, [kind])
    assert m["cache_hits"] == 1 and m["executed"] == 0
    assert _answers(cached) == {"res_find": [("r1", "yes")]}

    _, m = run_fixpoint(fresh, derive, mat, [kind], fresh=True)
    assert m["cache_hits"] == 0 and m["executed"] == 1
    assert _answers(fresh) == {"res_find": [("r1", "no")]}
    # The row is still written for later runs.
    assert json.loads((fresh / "probe-cache.json").read_text())[
        json.dumps(["find", ["libfoo"], ""], sort_keys=True)
    ]["res_find"] == [["r1", "no"]]
