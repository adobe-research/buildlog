# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The discovery schedule that makes a seeded configure equal a fresh one
(discovery.run_fixpoint): unguarded requests are answered last, a round that
drops a stale seed answers nothing, and a seeded run that fails a driver
check reruns unseeded. A synthetic program stands in for the rules; static
inputs are `static.facts` lines, set per run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from buildlog.discovery import Kind, run_fixpoint
from buildlog.facts import read_rows, write_rows


def _kind(name, fn, *, fingerprint=lambda args, results: "", unguarded=None) -> Kind:
    return Kind(
        name,
        f"req_{name}",
        f"dom_{name}",
        (f"res_{name}",),
        "test:fn",
        fn,
        fingerprint=fingerprint,
        unguarded=unguarded,
    )


class Program:
    """`rules(static, have)` returns (requests, extra out-of-domain requests) per kind name,
    `have` being the kinds with an answer in the round's input."""

    def __init__(self, rules):
        self.rules = rules
        self.static: set[str] = set()

    def materialize(self, env_dir: Path, round_no: int) -> Path:
        d = env_dir.parent / "in" / f"round-{round_no}"
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*"):
            f.unlink()
        for f in env_dir.glob("*.facts"):
            write_rows(d / f.name, read_rows(f))
        write_rows(d / "static.facts", [(s,) for s in sorted(self.static)])
        return d

    def derive(self, facts_in: Path, round_no: int) -> Path:
        out = facts_in.parent.parent / "out" / f"round-{round_no}"
        out.mkdir(parents=True, exist_ok=True)
        have = {f.stem.removeprefix("res_") for f in facts_in.glob("res_*.facts") if read_rows(f)}
        static = {r[0] for r in read_rows(facts_in / "static.facts")}
        requests, outside = self.rules(static, have)
        for name in "bcghn":
            rows = requests.get(name, [])
            write_rows(out / f"req_{name}.csv", rows + outside.get(name, []))
            write_rows(out / f"dom_{name}.csv", rows)
        for f in facts_in.glob("*.facts"):
            write_rows(out / f"{f.stem}.csv", read_rows(f))
        return out

    def run(self, build, kinds, **kw):
        return run_fixpoint(build, self.derive, self.materialize, kinds, program_digest="p", **kw)


def _env(build: Path) -> dict[str, list]:
    return {f.stem: sorted(read_rows(f)) for f in sorted((build / "env-facts").glob("*.facts"))}


def _no_transient(static, have):
    """The inventory g is guarded; package n is requested while g is unanswered."""
    return {"g": [("rg", "x")], **({} if "g" in have else {"n": [("rn", "foo")]})}, {}


def test_unguarded_requests_wait_for_guarded_closure(tmp_path):
    calls = []

    def n_fn(ctx, req, args, results):
        calls.append(req)
        return {"res_n": [(req, "found")]}

    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("n", n_fn, unguarded="reads below the relay"),
    ]
    program = Program(_no_transient)
    _, metrics = program.run(tmp_path / "fresh", kinds)
    assert calls == []  # the transient package request was never answered
    first = metrics["rounds"][0]
    assert first["n"]["deferred"] == 1 and first["g"]["deferred"] == 0
    assert "res_n" not in _env(tmp_path / "fresh")
    # Seeded after an unrelated input change: same outcome.
    program.static = {"edit"}
    _, seeded = program.run(tmp_path / "fresh", kinds)
    assert seeded["seed"]["mode"] == "guarded" and seeded["executed"] == 0
    assert calls == [] and _env(tmp_path / "fresh") == {"res_g": [("rg", "ok")]}


def test_unguarded_requests_are_answered_once_nothing_guarded_is_pending(tmp_path):
    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("n", lambda ctx, req, args, results: {"res_n": [(req, "found")]}, unguarded="x"),
    ]
    program = Program(lambda static, have: ({"g": [("rg", "x")], "n": [("rn", "foo")]}, {}))
    _, metrics = program.run(tmp_path, kinds)
    assert [r["n"]["deferred"] for r in metrics["rounds"]] == [1, 0, 0]
    assert _env(tmp_path) == {"res_g": [("rg", "ok")], "res_n": [("rn", "found")]}


@pytest.mark.parametrize("shuffle_seed", [None, 1, 2])
def test_stale_seed_round_probes_nothing(tmp_path, shuffle_seed):
    """g's key reads c's rows, so a seeded g is stale while c (whose request no
    longer arises) is unconfirmed; h's request arises from g's seeded rows,
    which the probes' snapshot lacks."""
    h_calls = []

    def answer(name):
        return lambda ctx, req, args, results: {f"res_{name}": [(req, "ok")]}

    def h(ctx, req, args, results):
        h_calls.append(sorted(results["res_g"]))  # the rows the request came from
        return {"res_h": [(req, "ok")]}

    kinds = [
        _kind("b", answer("b")),
        _kind("c", answer("c")),
        _kind(
            "g",
            answer("g"),
            fingerprint=lambda args, results: repr(sorted(results.get("res_c", ()))),
        ),
        _kind("h", h, fingerprint=None),
    ]

    def rules(static, have):
        requests = {"b": [("rb", "x")]}
        if "edit" not in static:
            requests["c"] = [("rc", "x")]
        if "b" in have:
            requests["g"] = [("rg", "x")]
        if "g" in have:
            requests["h"] = [("rh", "x")]
        return requests, {}

    program = Program(rules)
    program.run(tmp_path / "a", kinds, shuffle_seed=shuffle_seed)
    program.static = {"edit"}
    _, seeded = program.run(tmp_path / "a", kinds, shuffle_seed=shuffle_seed)
    assert seeded["seed"]["mode"] == "guarded" and "restarted" not in seeded["seed"]
    assert [i for i, r in enumerate(seeded["rounds"]) if r.get("stale_round")] == [0]
    assert len(h_calls) == 2 and all(h_calls)
    program.run(tmp_path / "b", kinds, shuffle_seed=shuffle_seed)
    assert _env(tmp_path / "a") == _env(tmp_path / "b")


def test_seeded_check_failure_reruns_unseeded(tmp_path):
    """An inert seeded answer that a rule reads without its request (an
    unguarded read) makes a request outside the domain, in a model a fresh
    run never computes."""

    def rules(static, have):
        requests = {"g": [("rg", "x")]}
        if "edit" not in static:
            requests["h"] = [("rh", "y")]
        outside = {"g": [("rbad", "z")]} if "edit" in static and "h" in have else {}
        return requests, outside

    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("h", lambda ctx, req, args, results: {"res_h": [(req, "ok")]}),
    ]
    program = Program(rules)
    program.run(tmp_path / "a", kinds)
    program.static = {"edit"}
    _, seeded = program.run(tmp_path / "a", kinds)
    assert seeded["seed"]["restarted"] is True and seeded["seed"]["reason"] == "check"
    program.run(tmp_path / "b", kinds)
    assert _env(tmp_path / "a") == _env(tmp_path / "b") == {"res_g": [("rg", "ok")]}


def test_fresh_check_failure_is_still_reported(tmp_path):
    kinds = [_kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]})]
    program = Program(lambda static, have: ({"g": [("rg", "x")]}, {"g": [("rbad", "z")]}))
    with pytest.raises(SystemExit, match="request outside finite domain for g"):
        program.run(tmp_path, kinds)


@pytest.mark.parametrize("legacy_record", [False, True])
def test_withdrawn_unguarded_answer_is_replayed_not_discarded(tmp_path, legacy_record):
    """n withdraws its own request through negation, but its answer is still
    part of the fresh outcome. The record lists it as withdrawn, so a warm
    configure replays it; a legacy record (replayable unknown) falls back."""
    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("n", lambda ctx, req, args, results: {"res_n": [(req, "found")]}, unguarded="x"),
    ]
    program = Program(
        lambda static, have: (
            {"g": [("rg", "x")], **({} if "n" in have else {"n": [("rn", "foo")]})},
            {},
        )
    )
    build = tmp_path / "warm"
    program.run(build, kinds)
    cold = _env(build)
    record_file = build / "probe-answers.json"
    records = json.loads(record_file.read_text())
    record = records["records"]["p"]
    assert record["withdrawn"] == [["n", "rn"]]
    assert {a[0] for a in record["answers"]} == {"g", "n"}
    if legacy_record:
        del record["withdrawn"]
        record["replayable"] = False
        record_file.write_text(json.dumps(records))
    _, warm = program.run(build, kinds)
    if legacy_record:
        assert warm["seed"]["mode"] == "guarded"
        assert warm["seed"]["seeded"] == 1
        assert not warm["seed"].get("restarted")
        assert len(warm["rounds"]) == 2
        assert read_rows(build / "out/round-1/req_n.csv") == []
    else:
        assert warm["seed"]["mode"] == "replay"
        assert warm["seed"]["seeded"] == 2 and warm["seed"]["unused"] == 0
        assert not warm["seed"].get("restarted") and len(warm["rounds"]) == 1
        assert warm["executed"] == 0
    program.run(tmp_path / "fresh", kinds, seed=False)
    assert _env(build) == _env(tmp_path / "fresh") == cold
    assert cold["res_n"] == [("rn", "found")]


def test_pending_replay_request_restarts_guarded(tmp_path):
    """An uncached kind always needs a new answer, even with static inputs
    unchanged; replay must not keep its speculative unguarded answers."""
    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("h", lambda ctx, req, args, results: {"res_h": [(req, "ok")]}, fingerprint=None),
    ]
    program = Program(lambda static, have: ({"g": [("rg", "x")], "h": [("rh", "x")]}, {}))
    build = tmp_path / "warm"
    program.run(build, kinds)
    # The non-cacheable answer makes this a guarded run already. To exercise
    # replay deviation, start with a record predating this kind's request.
    records = json.loads((build / "probe-answers.json").read_text())
    records["records"]["p"]["answers"] = [
        a for a in records["records"]["p"]["answers"] if a[0] != "h"
    ]
    (build / "probe-answers.json").write_text(json.dumps(records))
    _, warm = program.run(build, kinds)
    assert warm["seed"]["mode"] == "guarded"
    assert warm["seed"]["restarted"] is True
    assert warm["seed"]["reason"] == "replay"
    assert warm["executed"] == 1
    program.run(tmp_path / "fresh", kinds, seed=False)
    assert _env(build) == _env(tmp_path / "fresh")


@pytest.fixture
def seeded_record_with_withdrawn_package(tmp_path):
    """Finite, stratified rules with constant probes/keys (FC and FL):

    req_g("rg") :- dom_g("rg").
    req_b("rb") :- enabled_b(), req_g("rg"), !res_g("rg", "ok").
    use_b() :- req_b("rb"), res_b("rb", "ok").
    req_n("rn") :- dom_n("rn"), !res_n("rn", "found").

    b is guarded and transient; n is unguarded and self-withdrawing.
    There are no probe prerequisites. The edited seeded pass never asks b,
    but a fresh pass answers it alongside g. Both answer n after closure.
    """
    kinds = [
        _kind("g", lambda ctx, req, args, results: {"res_g": [(req, "ok")]}),
        _kind("b", lambda ctx, req, args, results: {"res_b": [(req, "ok")]}),
        _kind("n", lambda ctx, req, args, results: {"res_n": [(req, "found")]}, unguarded="x"),
    ]

    def rules(static, have):
        requests = {"g": [("rg", "x")]}
        if "enabled_b" in static and "g" not in have:
            requests["b"] = [("rb", "x")]
        if "n" not in have:
            requests["n"] = [("rn", "foo")]
        return requests, {}

    program = Program(rules)
    build = tmp_path / "seeded"
    program.run(build, kinds)
    program.static = {"enabled_b"}
    _, seeded = program.run(build, kinds)
    assert seeded["seed"]["mode"] == "guarded"
    record = json.loads((build / "probe-answers.json").read_text())["records"]["p"]
    assert record["withdrawn"] == [["n", "rn"]]
    assert {a[0] for a in record["answers"]} == {"g", "n"}
    seeded_facts = _env(build)
    _, replay = program.run(build, kinds)
    assert replay["seed"]["mode"] == "replay"
    assert replay["executed"] == 0 and len(replay["rounds"]) == 1
    assert not replay["seed"].get("restarted")
    assert _env(build) == seeded_facts
    fresh = tmp_path / "fresh"
    _, cold = program.run(fresh, kinds, fresh=True)
    for name in ("b", "g", "n"):
        assert read_rows(build / f"out/round-0/req_{name}.csv") == read_rows(
            fresh / f"out/round-{cold['final_round']}/req_{name}.csv"
        )
    assert seeded_facts == {"res_g": [("rg", "ok")], "res_n": [("rn", "found")]}
    assert _env(fresh) == seeded_facts | {"res_b": [("rb", "ok")]}
    return seeded_facts, _env(fresh)


def test_replay_of_seeded_record_retains_withdrawn_unguarded_answers(
    seeded_record_with_withdrawn_package,
):
    """Canonical package answers agree; absent-request guarded answers need not."""
    seeded, fresh = seeded_record_with_withdrawn_package
    assert seeded["res_n"] == fresh["res_n"]


@pytest.mark.xfail(
    strict=True,
    reason="cache agreement excludes inert probe-result facts, not just metrics or rounds",
)
def test_seeded_record_replay_has_exact_fresh_answer_facts(seeded_record_with_withdrawn_package):
    seeded, fresh = seeded_record_with_withdrawn_package
    assert seeded == fresh
