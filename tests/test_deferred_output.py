# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deferred serialization of the discovery rounds (engine.SouffleRounds with
`--serve-deferred-output`): rounds write only the relations the loop reads,
and the final pass's remaining outputs are dumped from the very evaluation.

The protocol and lifecycle tests run against a stand-in binary that speaks the
serve protocol (its outputs are a function of the fact bytes read at the
request); the end-to-end tests use the real patched Soufflé and need its
deferred-output support."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from buildlog import engine, souffle_cache
from buildlog.engine import (
    DEFER_ENV,
    DISCOVERED,
    ROUND_MEMO_ENV,
    SERVE_ENV,
    SouffleRounds,
    configure_project,
)
from buildlog.errors import LoaderError
from buildlog.paths import souffle_path

from .host import CROSS_REASON, MACOS
from .test_loader import make_project
from .test_probe_seeding import CROSS, USER, assert_same_derivation

RELATIONS = ["req_a", "dom_a", DISCOVERED, "other", "emitted"]
CONTROL = frozenset({"req_a", "dom_a", DISCOVERED})

FAKE = r"""#!{python}
import hashlib, os, sys
from pathlib import Path

args = sys.argv[1:]
log = Path(os.environ["FAKE_LOG"])


def note(text):
    with log.open("a") as fh:
        fh.write(text + "\n")


def content(rel, data):
    h = hashlib.sha256()
    for name in sorted(data):
        h.update(name.encode() + b"\0" + data[name] + b"\0")
    return f"{{rel}} {{h.hexdigest()}}\n"


def write(out, rels, data):
    for rel in rels:
        if rel != os.environ.get("FAKE_SKIP"):
            (Path(out) / f"{{rel}}.csv").write_text(content(rel, data))


def read(facts):
    return {{f.name: f.read_bytes() for f in sorted(Path(facts).glob("*.facts"))}}


if "--help" in args:
    note("help")
    print("--serve  serve")
    if {defer}:
        print("--serve-deferred-output  defer")
    sys.exit(0)
rels = [r for r in Path(args[-1]).read_text().split()]
if "--serve" not in args:
    note("run")
    write(args[args.index("-D") + 1], rels, read(args[args.index("-F") + 1]))
    sys.exit(0)
note("start " + " ".join(a for a in args if a.startswith("--serve")))
print("souffle-serve: ready", flush=True)
retained = None
for line in sys.stdin:
    line = line.rstrip("\n")
    if line == "DUMP":
        if retained is None:
            print("DUMP without deferred outputs", file=sys.stderr)
            sys.exit(1)
        out, data, deferred = retained
        retained = None
        if os.environ.get("FAKE_FAIL_DUMP"):
            print("injected dump failure", file=sys.stderr)
            sys.exit(3)
        write(out, deferred, data)
        note("dump")
        print("souffle-serve: dumped 0.0", flush=True)
        continue
    retained = None
    parts = line.split("\t")
    facts, out = parts[0], parts[1]
    data = read(facts)
    immediate = set(parts[2][len("defer="):].split(",")) if len(parts) == 3 else None
    note("eval " + ("full" if immediate is None else ",".join(sorted(immediate))))
    write(out, [r for r in rels if immediate is None or r in immediate], data)
    if immediate is not None:
        retained = (out, data, [r for r in rels if r not in immediate])
    print("souffle-serve: done 0.0", flush=True)
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    def make(*, defer: bool = True) -> Path:
        binary = tmp_path / f"souffle-{int(defer)}"
        binary.write_text(FAKE.format(python=sys.executable, defer=defer))
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("BUILDLOG_SOUFFLE", str(binary))
        monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
        return binary

    for var in (SERVE_ENV, DEFER_ENV, ROUND_MEMO_ENV):
        monkeypatch.delenv(var, raising=False)
    for var in ("FAKE_SKIP", "FAKE_FAIL_DUMP"):
        monkeypatch.delenv(var, raising=False)
    engine.SOUFFLE_TIMES.clear()
    return make


def log(tmp_path: Path) -> list[str]:
    path = tmp_path / "log"
    return path.read_text().splitlines() if path.exists() else []


def evals(tmp_path: Path) -> list[str]:
    return [line for line in log(tmp_path) if line.split()[0] in ("eval", "run", "dump")]


def make_rounds(tmp_path: Path, digest: str | None = "program") -> SouffleRounds:
    program = tmp_path / "program" / "build.dl"
    program.parent.mkdir(exist_ok=True)
    program.write_text("\n".join(RELATIONS) + "\n")
    rounds = SouffleRounds(program, digest)
    rounds.set_outputs(RELATIONS)
    return rounds


def facts_dir(tmp_path: Path, text: str = "one", name: str = "facts") -> Path:
    facts = tmp_path / name
    facts.mkdir(exist_ok=True)
    (facts / "a.facts").write_text(text)
    (facts / "source_map.json").write_text("{}")
    return facts


def expected(text: str, rels=RELATIONS) -> dict[str, str]:
    h = hashlib.sha256()
    h.update(b"a.facts\0" + text.encode() + b"\0")
    return {f"{r}.csv": f"{r} {h.hexdigest()}\n" for r in rels}


def read_out(out: Path) -> dict[str, str]:
    return {f.name: f.read_text() for f in sorted(out.glob("*.csv"))}


def stamp(out: Path) -> dict:
    return json.loads(out.with_name(out.name + ".key").read_text())


def test_one_help_probe_serves_both_capabilities(tmp_path, fake):
    fake()
    binary = engine.require_souffle()
    assert engine._serve_supported(binary) and engine._defer_supported(binary)
    assert log(tmp_path).count("help") == 1


@pytest.mark.parametrize(
    ("setup", "defer"),
    [("old binary", False), ("opt out", False), ("no serve", False), ("capable", True)],
)
def test_deferral_needs_the_binary_and_serve_and_is_opt_out(
    tmp_path, fake, monkeypatch, setup, defer
):
    fake(defer=setup != "old binary")
    if setup == "opt out":
        monkeypatch.setenv(DEFER_ENV, "0")
    if setup == "no serve":
        monkeypatch.setenv(SERVE_ENV, "0")
    rounds = make_rounds(tmp_path)
    assert rounds.defer is defer
    out = tmp_path / "round-0"
    rounds.run(facts_dir(tmp_path), out, CONTROL)
    rounds.finish(out)
    rounds.close()
    # Every round without the feature is a full evaluation, and the result is the same.
    assert read_out(out) == expected("one")
    assert evals(tmp_path) == (
        ["eval " + ",".join(sorted(CONTROL)), "dump"]
        if defer
        else (["run"] if setup == "no serve" else ["eval full"])
    )
    assert stamp(out)["complete"] is True


def test_session_starts_with_the_exact_feature_flags_and_the_check_key_has_them(
    tmp_path, fake, monkeypatch
):
    fake()
    seen = []
    real_run = souffle_cache.run

    def spying(args, *a, **kw):
        seen.append(args)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(souffle_cache, "run", spying)
    monkeypatch.setenv(souffle_cache.CACHE_ENV, "off")
    rounds = make_rounds(tmp_path)
    rounds.check_composed(rounds.program, [], [])
    rounds.close()
    assert "--serve-deferred-output" in seen[0] and seen[0][1:3] == [
        "--serve",
        "--serve-deferred-output",
    ]
    assert "start --serve --serve-deferred-output" in log(tmp_path)


def test_deferred_round_is_one_evaluation_and_the_dump_completes_the_same_directory(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    rounds.run(facts_dir(tmp_path), out, CONTROL)
    assert sorted(read_out(out)) == sorted(f"{r}.csv" for r in CONTROL)
    assert stamp(out)["complete"] is False
    rounds.finish(out)
    assert read_out(out) == expected("one")
    assert stamp(out)["complete"] is True
    assert [t for t, _ in engine.SOUFFLE_TIMES].count("round") == 1
    rounds.close()
    assert len(evals(tmp_path)) == 2


def test_full_memo_hit_needs_no_process_and_serves_control_without_downgrade(tmp_path, fake):
    fake()
    first = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    first.run(facts_dir(tmp_path), out, CONTROL)
    first.finish(out)
    first.close()
    (tmp_path / "log").unlink()

    for control in (None, CONTROL):
        rounds = make_rounds(tmp_path)
        rounds.run(facts_dir(tmp_path), out, control)
        rounds.finish(out)
        rounds.close()
    assert log(tmp_path) == []
    assert stamp(out)["complete"] is True and read_out(out) == expected("one")
    # A complete round elsewhere is renamed in, and stays complete.
    rounds = make_rounds(tmp_path)
    moved = tmp_path / "round-5"
    rounds.run(facts_dir(tmp_path), moved, CONTROL)
    assert stamp(moved)["complete"] is True and not out.exists()
    assert log(tmp_path) == []


def test_partial_round_never_satisfies_a_full_request(tmp_path, fake):
    fake()
    first = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    first.run(facts_dir(tmp_path), out, CONTROL)
    first.close()
    assert stamp(out)["complete"] is False
    rounds = make_rounds(tmp_path)
    rounds.run(facts_dir(tmp_path), out, None)
    rounds.close()
    assert log(tmp_path)[-1] == "eval full"
    assert read_out(out) == expected("one") and stamp(out)["complete"] is True


def test_control_hit_promoted_from_its_original_bytes_after_in_place_changes(tmp_path, fake):
    fake()
    first = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    facts = facts_dir(tmp_path, "original")
    first.run(facts, out, CONTROL)
    first.close()
    (tmp_path / "log").unlink()

    rounds = make_rounds(tmp_path)
    rounds.run(facts, out, CONTROL)
    assert log(tmp_path) == []
    # The guarded cleanup of unused seeds rewrites the facts after the evaluation.
    (facts / "a.facts").write_text("cleaned")
    rounds.finish(out)
    rounds.close()
    assert read_out(out) == expected("original")
    assert log(tmp_path) == ["start --serve --serve-deferred-output", "eval full"]
    assert not list(tmp_path.glob("*.replay"))
    assert stamp(out)["complete"] is True


def test_dump_preserves_the_evaluation_not_the_mutated_facts_and_its_key(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    facts = facts_dir(tmp_path, "evaluated")
    key = rounds._inputs_key(facts)
    rounds.run(facts, out, CONTROL)
    (facts / "a.facts").write_text("cleaned")
    rounds.finish(out)
    rounds.close()
    assert read_out(out) == expected("evaluated")
    assert stamp(out)["inputs"] == key
    assert len(evals(tmp_path)) == 2


def test_an_older_retained_engine_is_not_dumped_for_a_later_memo_hit(tmp_path, fake):
    fake()
    first = make_rounds(tmp_path)
    first.run(facts_dir(tmp_path, "x"), tmp_path / "round-0", CONTROL)
    first.close()
    (tmp_path / "log").unlink()

    rounds = make_rounds(tmp_path)
    rounds.run(facts_dir(tmp_path, "y", "facts-y"), tmp_path / "round-1", CONTROL)
    out = tmp_path / "round-2"
    rounds.run(facts_dir(tmp_path, "x"), out, CONTROL)
    assert not (tmp_path / "round-0").exists()
    rounds.finish(out)
    rounds.close()
    assert read_out(out) == expected("x")
    assert "dump" not in log(tmp_path)
    assert log(tmp_path)[-1] == "eval full"


def test_finishing_an_older_round_is_a_programming_error(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path)
    rounds.run(facts_dir(tmp_path), tmp_path / "round-0", CONTROL)
    rounds.run(facts_dir(tmp_path, "two"), tmp_path / "round-1", CONTROL)
    with pytest.raises(AssertionError):
        rounds.finish(tmp_path / "round-0")
    rounds.close()


@pytest.mark.parametrize("skip", ["emitted", "req_a"])
def test_a_missing_output_is_an_error_and_never_stamped_complete(tmp_path, fake, monkeypatch, skip):
    fake()
    monkeypatch.setenv("FAKE_SKIP", skip)
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    with pytest.raises(SystemExit, match=skip):
        rounds.run(facts_dir(tmp_path), out, CONTROL)
        rounds.finish(out)
    rounds.close()
    assert not out.with_name("round-0.key").exists()


def test_full_evaluation_missing_an_output_is_not_stamped(tmp_path, fake, monkeypatch):
    fake()
    monkeypatch.setenv("FAKE_SKIP", "emitted")
    rounds = make_rounds(tmp_path)
    with pytest.raises(SystemExit, match="emitted"):
        rounds.run(facts_dir(tmp_path), tmp_path / "round-0", None)
    rounds.close()
    assert not (tmp_path / "round-0.key").exists()


def test_a_failed_dump_is_reported_and_leaves_no_complete_stamp(tmp_path, fake, monkeypatch):
    fake()
    monkeypatch.setenv("FAKE_FAIL_DUMP", "1")
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    rounds.run(facts_dir(tmp_path), out, CONTROL)
    with pytest.raises(SystemExit, match="injected dump failure"):
        rounds.finish(out)
    rounds.close()
    assert not (tmp_path / "round-0.key").exists()
    assert rounds.returncode == 3


def test_without_a_memo_digest_the_dump_still_completes(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path, digest=None)
    out = tmp_path / "round-0"
    rounds.run(facts_dir(tmp_path), out, CONTROL)
    rounds.finish(out)
    rounds.close()
    assert read_out(out) == expected("one") and not (tmp_path / "round-0.key").exists()


def test_paths_with_a_tab_fall_back_to_a_full_run(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round\t0"
    rounds.run(facts_dir(tmp_path), out, CONTROL)
    rounds.finish(out)
    rounds.close()
    assert read_out(out) == expected("one")
    assert evals(tmp_path) == ["run"]


def test_old_memo_stamps_are_not_trusted(tmp_path, fake):
    fake()
    rounds = make_rounds(tmp_path)
    out = tmp_path / "round-0"
    rounds.run(facts_dir(tmp_path), out, None)
    rounds.close()
    data = stamp(out)
    del data["complete"]
    out.with_name("round-0.key").write_text(json.dumps(data))
    again = make_rounds(tmp_path)
    assert again._reuse(data["inputs"], out) is None
    assert again._reuse(data["inputs"], out, CONTROL) is None


# --- configure-level lifecycle (real Soufflé; serve only) -------------------

real = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


@pytest.fixture
def real_env(monkeypatch, tmp_path):
    monkeypatch.setenv(souffle_cache.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    for var in (SERVE_ENV, DEFER_ENV, ROUND_MEMO_ENV):
        monkeypatch.delenv(var, raising=False)


def _track_processes(monkeypatch) -> list:
    started = []
    real_start = SouffleRounds._start

    def starting(self, *args):
        real_start(self, *args)
        started.append(self.proc)

    monkeypatch.setattr(SouffleRounds, "_start", starting)
    return started


def _assert_closed(started: list) -> None:
    assert started
    for proc in started:
        assert proc.poll() is not None
        assert all(pipe.closed for pipe in (proc.stdin, proc.stdout, proc.stderr))


@real
@pytest.mark.parametrize("phase", ["facts", "kinds", "round", "final-dump"])
def test_evaluator_is_closed_when_any_phase_fails(tmp_path, monkeypatch, real_env, phase):
    started = _track_processes(monkeypatch)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')

    def boom(*args, **kwargs):
        raise LoaderError([f"injected {phase} failure"])

    if phase == "facts":
        monkeypatch.setattr(engine, "schema_from_rules", boom)
    elif phase == "kinds":
        monkeypatch.setattr(engine, "kinds_from_probes", boom)
    elif phase == "round":
        real_fixpoint = engine.run_fixpoint

        def failing(builddir, derive_round, *args, **kwargs):
            def once(facts_in, round_no):
                derive_round(facts_in, round_no)
                raise LoaderError([f"injected {phase} failure"])

            return real_fixpoint(builddir, once, *args, **kwargs)

        monkeypatch.setattr(engine, "run_fixpoint", failing)
    else:
        monkeypatch.setattr(SouffleRounds, "finish", boom)
    with pytest.raises(LoaderError, match=f"injected {phase} failure"):
        configure_project(project, tmp_path / "build", emit=False)
    _assert_closed(started)


@real
def test_evaluator_is_closed_when_reading_used_modules_fails(tmp_path, monkeypatch, real_env):
    started = _track_processes(monkeypatch)
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')

    read = engine.read_rows

    def boom(path):
        if path.name == f"{DISCOVERED}.csv":
            raise LoaderError(["injected discovery failure"])
        return read(path)

    monkeypatch.setattr(engine, "read_rows", boom)
    with pytest.raises(LoaderError, match="injected discovery failure"):
        configure_project(project, tmp_path / "build", emit=False)
    _assert_closed(started)


@real
def test_the_final_outputs_are_complete_before_anything_reads_them(tmp_path, monkeypatch, real_env):
    project = make_project(tmp_path, [], 'target = [["unused"]]\n')
    order = []
    real_finish = SouffleRounds.finish
    real_active = engine.read_rows

    def finishing(self, out):
        real_finish(self, out)
        order.append(("finish", out))
        wanted = {f"{r}.csv" for r in self.outputs}
        assert wanted <= {f.name for f in out.iterdir()}

    def reading(path, *args, **kwargs):
        if Path(path).name == "config_active.csv":
            order.append(("config_active", Path(path).parent))
        return real_active(path, *args, **kwargs)

    monkeypatch.setattr(SouffleRounds, "finish", finishing)
    monkeypatch.setattr(engine, "read_rows", reading)
    build = tmp_path / "build"
    final_dir, _ = configure_project(project, build, emit=False)
    assert [kind for kind, _ in order] == ["finish", "config_active"]
    assert (final_dir / "config_active.csv").exists()


@real
def test_fixed_program_finishes_outputs_without_moving_program(tmp_path, monkeypatch, real_env):
    from .test_single_composition import project_for

    project, _ = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    seen = []
    real_finish = SouffleRounds.finish

    def finishing(self, out):
        seen.append(self.program)
        real_finish(self, out)

    monkeypatch.setattr(SouffleRounds, "finish", finishing)
    _, warm = configure_project(project, build, emit=False)
    assert warm["composition_calls"] == 1
    assert len(warm["outer"]) == 1
    assert seen == [build / "program" / "build.dl"]


# --- end to end (needs the deferred-output core) ----------------------------


def _core_defers() -> bool:
    if not souffle_path().exists():
        return False
    return engine._binary_capabilities(souffle_path())[1]


deferred_core = pytest.mark.skipif(
    not _core_defers(), reason="patched Souffle lacks --serve-deferred-output"
)


def _configure(build: Path, monkeypatch, *, defer: str = "1") -> list[str]:
    monkeypatch.setenv(DEFER_ENV, defer)
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    engine.SOUFFLE_TIMES.clear()
    configure_project(CROSS, build, user_files=[USER])
    return [purpose for purpose, _ in engine.SOUFFLE_TIMES]


@deferred_core
@pytest.mark.skipif(not MACOS, reason=CROSS_REASON)
@pytest.mark.integration
def test_deferred_derivation_equals_the_opt_out_with_no_extra_evaluation(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain_calls = _configure(plain, monkeypatch, defer="0")
    deferred = tmp_path / "deferred"
    calls = _configure(deferred, monkeypatch)
    assert "dump" not in plain_calls and calls.count("dump") == 1
    assert calls.count("round") == plain_calls.count("round")
    assert_same_derivation(deferred, plain)
    # Warm: the full memo is a hit and starts nothing.
    assert _configure(deferred, monkeypatch) == ["reused"]
    assert_same_derivation(deferred, plain)
    shutil.rmtree(plain)


# A serve-only Soufflé (no --serve-deferred-output), for the real fallback test.
LEGACY_ENV = "BUILDLOG_TEST_LEGACY_SOUFFLE"
_legacy = os.environ.get(LEGACY_ENV)
LEGACY = Path(_legacy) if _legacy else None


@deferred_core
@pytest.mark.skipif(
    LEGACY is None or not LEGACY.is_file(), reason=f"{LEGACY_ENV} names no serve-only Souffle"
)
@pytest.mark.skipif(not MACOS, reason=CROSS_REASON)
@pytest.mark.integration
def test_a_real_serve_only_binary_falls_back_to_full_rounds_with_the_same_derivation(
    tmp_path, monkeypatch
):
    deferred = tmp_path / "deferred"
    _configure(deferred, monkeypatch)
    monkeypatch.setenv("BUILDLOG_SOUFFLE", str(LEGACY))
    assert engine._binary_capabilities(LEGACY) == (True, False)
    legacy = tmp_path / "legacy"
    calls = _configure(legacy, monkeypatch)
    assert "dump" not in calls and "translate" in calls
    assert_same_derivation(deferred, legacy)
    assert _configure(legacy, monkeypatch) == ["reused"]


def _rewrite_stamp(out: Path, **changes) -> None:
    data = stamp(out)
    data.update(changes)
    out.with_name(out.name + ".key").write_text(json.dumps(data))


def test_a_control_round_serves_only_requests_it_covers(tmp_path, fake):
    fake()
    out = tmp_path / "round-0"
    small = frozenset({"req_a"})
    first = make_rounds(tmp_path)
    first.run(facts_dir(tmp_path), out, small)
    first.close()
    assert sorted(read_out(out)) == ["req_a.csv"]
    # The same inputs asked for more relations are evaluated again, not read as empty.
    wider = make_rounds(tmp_path)
    wider.run(facts_dir(tmp_path), out, CONTROL)
    wider.close()
    assert log(tmp_path)[-1] == "eval " + ",".join(sorted(CONTROL))
    assert sorted(read_out(out)) == sorted(f"{r}.csv" for r in CONTROL)
    # ... and the wider round serves the narrower request.
    (tmp_path / "log").unlink()
    narrower = make_rounds(tmp_path)
    narrower.run(facts_dir(tmp_path), out, small)
    assert log(tmp_path) == [] and stamp(out)["complete"] is False
    assert narrower._last is not None and not narrower._last.complete


def test_a_complete_memo_must_cover_every_requested_control_relation(tmp_path, fake):
    fake()
    out = tmp_path / "round-0"
    first = make_rounds(tmp_path)
    first.run(facts_dir(tmp_path), out)
    first.close()
    rounds = make_rounds(tmp_path)
    with pytest.raises(SystemExit, match="unknown"):
        rounds.run(facts_dir(tmp_path), out, frozenset({"unknown"}))
    rounds.close()
    assert not out.with_name("round-0.key").exists()


@pytest.mark.parametrize("control", [None, CONTROL])
def test_a_stamp_claiming_complete_but_listing_fewer_outputs_is_a_miss(tmp_path, fake, control):
    fake()
    out = tmp_path / "round-0"
    first = make_rounds(tmp_path)
    first.run(facts_dir(tmp_path), out, CONTROL)
    first.close()
    _rewrite_stamp(
        out,
        complete=True,
        outputs=[[f.name, f.stat().st_size, f.stat().st_mtime_ns] for f in sorted(out.iterdir())],
    )
    (tmp_path / "log").unlink()
    rounds = make_rounds(tmp_path)
    rounds.run(facts_dir(tmp_path), out, control)
    rounds.close()
    assert log(tmp_path)[-1].startswith("eval")
    assert sorted(read_out(out)) == sorted(
        f"{r}.csv" for r in (RELATIONS if control is None else CONTROL)
    )


@pytest.mark.parametrize("marker", ["missing", "yes", 1, None])
def test_only_an_explicit_boolean_complete_marker_is_trusted(tmp_path, fake, marker):
    fake()
    out = tmp_path / "round-0"
    first = make_rounds(tmp_path)
    first.run(facts_dir(tmp_path), out, None)
    first.close()
    data = stamp(out)
    if marker == "missing":
        del data["complete"]
    else:
        data["complete"] = marker
    out.with_name("round-0.key").write_text(json.dumps(data))
    rounds = make_rounds(tmp_path)
    for control in (None, CONTROL):
        assert rounds._reuse(data["inputs"], out, control) is None
