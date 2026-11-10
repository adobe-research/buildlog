# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from .errors import LoaderError
from .facts import read_rows, tuple_key, write_rows
from .layers import ProbeDecl


@dataclass(frozen=True)
class ProbeContext:
    builddir: Path
    workdir: Path


@dataclass(frozen=True)
class Kind:
    name: str
    req: str
    domain: str
    results: tuple[str, ...]
    entry: str
    fn: Callable[
        [ProbeContext, str, tuple[str, ...], Mapping[str, frozenset[tuple[str, ...]]]],
        dict[str, list[tuple[str, ...]]],
    ]
    fingerprint: Callable[..., str] | None = None
    fingerprint_params: tuple[tuple[str, object], ...] = ()
    unguarded: str | None = None


def _load_entry(entry: str, manifest: Path):
    """Resolve `module:function`, or `file.py:function` relative to the manifest."""
    mod_name, func_name = entry.split(":", 1)
    if mod_name.endswith(".py"):
        path = (manifest.parent / mod_name).resolve()
        name = f"buildlog_pkg_{abs(hash(str(path)))}_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"{manifest}: cannot load probe module {mod_name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(mod_name)
    return getattr(module, func_name)


def kinds_from_probes(probes: list[ProbeDecl]) -> list[Kind]:
    """Bind loader-validated probe declarations to their Python entry points."""
    kinds: list[Kind] = []
    for probe in probes:
        try:
            fn = _load_entry(probe.entry, probe.manifest)
            fingerprint = (
                _load_entry(probe.fingerprint, probe.manifest) if probe.fingerprint else None
            )
        except Exception as exc:
            raise LoaderError(
                [f"{probe.layer}: probe {probe.kind}: cannot load entry point: {exc}"]
            ) from exc
        kinds.append(
            Kind(
                probe.kind,
                probe.request,
                probe.domain,
                probe.results,
                probe.entry,
                fn,
                fingerprint,
                probe.fingerprint_params,
                probe.unguarded,
            )
        )
    return kinds


SOURCE_MAP = "source_map.json"
# The previous converged derivation's answers, the seed of the next configure.
SEED_FILE = "probe-answers.json"
SEED_ENV = "BUILDLOG_SEED_PROBES"


# A request is identified by its kind and id: ids are unique only per kind.
ReqKey = tuple[str, str]


@dataclass
class Answer:
    kind: Kind
    args: tuple[str, ...]
    key: str | None  # None: an uncached kind (no fingerprint)
    rows: dict[str, list]
    confirmed: bool


def _result_state(
    answers: dict[ReqKey, Answer], confirmed_only: bool
) -> dict[str, set[tuple[str, ...]]]:
    out: dict[str, set[tuple[str, ...]]] = {}
    for a in answers.values():
        if confirmed_only and not a.confirmed:
            continue
        for rel, rows in a.rows.items():
            if rel != ORIGIN_KEY:
                out.setdefault(rel, set()).update(tuple(r) for r in rows)
    return out


@dataclass
class EnvWriteState:
    """What write_env last left in one env_dir, kept in process so unchanged
    files are not rewritten. Valid only for an env_dir that nothing else
    touches; `rels` is None until a full write has completed (and again after
    a failed one)."""

    rels: dict[str, set[tuple[str, ...]]] | None = None
    source_map: str | None = None
    source_map_rows: dict[str, list[str]] | None = None
    origin_keys: dict[tuple[str, tuple[str, ...]], str] = field(default_factory=dict)

    def reset(self) -> None:
        self.rels = None
        self.source_map = None
        self.source_map_rows = None


def write_env(
    env_dir: Path, answers: dict[ReqKey, Answer], state: EnvWriteState | None = None
) -> None:
    """Write every answer's rows, and their origins as source_map.json, in
    the order the answers were obtained. With `state`, only the files whose
    content changed since the previous call are rewritten (the rest keep their
    identity); without it, everything is rewritten."""
    results = _result_state(answers, confirmed_only=False)
    source_map: dict[str, list[str]] = {}
    origin_keys = state.origin_keys if state is not None else {}
    touched: set[str] = set()
    for a in answers.values():
        touched.update(rel for rel in a.rows if rel != ORIGIN_KEY)
        for rel, row, origin in a.rows.get(ORIGIN_KEY, []):
            key = (rel, tuple(row))
            if key not in origin_keys:
                origin_keys[key] = tuple_key(*key)
            entry = source_map.setdefault(origin_keys[key], [])
            if origin not in entry:
                entry.append(origin)
    if state is not None and source_map == state.source_map_rows:
        map_text = state.source_map
    else:
        map_text = json.dumps(source_map, indent=2, sort_keys=True) + "\n" if source_map else None
    previous = state.rels if state is not None else None
    previous_map = state.source_map if state is not None else None
    if state is not None:
        state.reset()  # stays reset if a write below fails
    if previous is None:
        for f in env_dir.glob("*.facts"):
            f.unlink()
        (env_dir / SOURCE_MAP).unlink(missing_ok=True)
        previous, previous_map = {}, None
    else:
        for rel in previous.keys() - touched:
            (env_dir / f"{rel}.facts").unlink(missing_ok=True)
    if map_text != previous_map or (map_text and not (env_dir / SOURCE_MAP).exists()):
        (env_dir / SOURCE_MAP).unlink(missing_ok=True)
        if map_text:
            (env_dir / SOURCE_MAP).write_text(map_text)
    for rel in sorted(touched):
        rows = results.get(rel, set())
        path = env_dir / f"{rel}.facts"
        if previous.get(rel) == rows and path.exists():
            continue
        path.unlink(missing_ok=True)
        write_rows(path, rows)
    if state is not None:
        state.rels = {rel: results.get(rel, set()) for rel in touched}
        state.source_map = map_text
        state.source_map_rows = source_map


def static_digest(facts_in: Path, results: set[str]) -> str:
    """A digest of a round's input facts without the probe answers."""
    h = hashlib.sha256()
    for f in sorted(facts_in.iterdir()):
        if f.name == SOURCE_MAP:
            data = json.loads(f.read_text())
            kept = {k: v for k, v in data.items() if json.loads(k)[0] not in results}
            h.update(json.dumps(kept, sort_keys=True).encode())
        elif f.stem not in results:
            h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def cache_key(
    kind: Kind, args: tuple[str, ...], results: Mapping[str, AbstractSet[tuple[str, ...]]]
) -> str | None:
    """The cache and seed key of a request, or None for a probe kind that
    declares no fingerprint: with nothing to say what its answer depends on,
    it is never cached or seeded and runs on every configure."""
    if kind.fingerprint is None:
        return None
    fp = kind.fingerprint(args, results, **dict(kind.fingerprint_params))
    return json.dumps([kind.name, list(args), fp], sort_keys=True)


# Reserved key of a probe result: (relation, row, "file:line") provenance of
# result rows, merged into the round's source_map.json like TOML origins.
ORIGIN_KEY = "__origin__"

# Reserved key of a probe result: the watch set, filesystem state the answer
# was computed from that the cache key cannot name beforehand (the files a
# compile read, the directories a search could have found a file in). A list
# of `watch_identity` rows, stored with the cached answer and re-checked when
# it is reused; any difference makes the entry a miss. Not a result relation.
WATCH_KEY = "__watch__"


def watch_identity(path: str, directory: bool = False) -> list:
    """What a watched path is identified by: where it resolves to, its size
    (files), modification time, inode and device. A directory's modification
    time changes when an entry is added, removed or renamed in it. A path
    that cannot be stat'ed has an all-None identity (its appearance differs)."""
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        return [path, real, None if directory else st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev]
    except OSError:
        return [path, None, None, None, None, None]


def watch_holds(entry: dict, memo: dict) -> bool:
    """Whether a cached entry's watch set still describes the filesystem.
    `memo` shares stats across entries within one run."""
    for row in entry.get(WATCH_KEY, []):
        path, directory = row[0], row[2] is None and row[1] is not None
        now = memo.get((path, directory))
        if now is None:
            now = memo[(path, directory)] = watch_identity(path, directory)
        if now != row:
            return False
    return True


def undeclared_message(kind: Kind, undeclared: list[str]) -> str:
    return f"probe {kind.name} returned undeclared relations: {', '.join(undeclared)}"


class Violation(NamedTuple):
    """A failed per-round driver check; `order` is the check kind (domain,
    argument conflict, undeclared relation, missing answer)."""

    order: int
    probe: str
    key: tuple[str, ...]
    message: str


def raise_violations(violations: list[Violation]) -> None:
    """Report every violation of a round, sorted by (check kind, probe kind,
    request arguments), so the failure does not depend on probe order."""
    if not violations:
        return
    ordered = sorted(violations)
    if len(ordered) == 1:
        raise SystemExit(ordered[0].message)
    body = "\n".join(f"[{i}] {v.message}" for i, v in enumerate(ordered, 1))
    raise SystemExit(f"{len(ordered)} driver check failures in this round:\n{body}")


def has_answer(kind: Kind, req_id: str, rows_by_rel: dict[str, list]) -> bool:
    """Whether an answer has a row for the request in its primary relation."""
    return any(r and r[0] == req_id for r in rows_by_rel.get(kind.results[0], ()))


def no_answer_error(kind: Kind, req_id: str, args: tuple[str, ...]) -> SystemExit:
    return SystemExit(
        f"probe {kind.name} gave no answer to request {req_id} "
        f"({', '.join(args) or 'no arguments'}): its primary result relation "
        f"{kind.results[0]} has no row for the request. Every request needs an "
        "answer; a probe that finds nothing must say so in a row (for example "
        "a status column), or the request would stay pending."
    )


def _snapshot(results: dict[str, set[tuple[str, ...]]]) -> Mapping[str, frozenset]:
    """The answers at the start of a round, read-only: every probe and
    fingerprint of the round sees the same state, whatever the order."""
    return MappingProxyType({rel: frozenset(rows) for rel, rows in results.items()})


def _cached_rows(entry: dict) -> dict[str, list]:
    return {
        rel: [tuple(r) for r in rows] if rel != ORIGIN_KEY else rows
        for rel, rows in entry.items()
        if rel != WATCH_KEY
    }


def _seed(
    builddir: Path, kinds: list[Kind], cache: dict, program_digest: str | None, memo: dict
) -> tuple[dict[ReqKey, Answer], dict | None]:
    """Tentative answers from the previous configure's record, each still
    valid: its cache entry exists and its key is unchanged."""
    records = _records(builddir)
    if program_digest is None or os.environ.get(SEED_ENV, "1") == "0" or not records:
        return {}, None
    # The record of this program (a project whose probes discover rule
    # packages composes one program per outer iteration), else the latest.
    record = records.get(program_digest) or records[next(reversed(records))]
    by_name = {k.name: k for k in kinds}
    answers: dict[ReqKey, Answer] = {}
    for entry in record.get("answers", []):
        try:
            name, req_id, args, key = entry
            hash((name, req_id))
        except (TypeError, ValueError):
            continue
        if name in by_name and key is not None and key in cache and watch_holds(cache[key], memo):
            answers[(name, req_id)] = Answer(
                by_name[name], tuple(args), key, _cached_rows(cache[key]), False
            )
    view = _result_state(answers, confirmed_only=False)
    for rk, a in list(answers.items()):
        if a.key is None or cache_key(a.kind, a.args, view) != a.key:
            del answers[rk]
    exact = record.get("program") == program_digest and len(answers) == len(
        record.get("answers", [])
    )
    # Records before `withdrawn` could say they were not replayable.
    replayable = "withdrawn" in record or record.get("replayable", True)
    return answers, (record if exact and replayable else None)


def _records(builddir: Path) -> dict[str, dict]:
    try:
        return json.loads((builddir / SEED_FILE).read_text())["records"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save_record(builddir: Path, record: dict) -> None:
    records = _records(builddir)
    records.pop(record["program"], None)
    records[record["program"]] = record
    kept = dict(list(records.items())[-4:])
    (builddir / SEED_FILE).write_text(json.dumps({"records": kept}, indent=1) + "\n")


def run_fixpoint(
    builddir: Path,
    derive_round: Callable[[Path, int], Path],
    materialize: Callable[[Path, int], Path],
    kinds: list[Kind],
    *,
    shuffle_seed: int | None = None,
    program_digest: str | None = None,
    seed: bool = True,
    allow_replay: bool = True,
    fresh: bool = False,
) -> tuple[Path, dict]:
    """Derive, answer the new probe requests, repeat until no request is new.

    With `program_digest` (the composed program's), round 0 is seeded with
    the previous configure's answers (SEED_FILE) whose cache keys still hold:
    - *replay*: the program and the non-answer input facts are unchanged,
      and every recorded key and watch holds; all its answers are
      seeded and it converges in one round. Any deviation (a request it did
      not make, an answer it does not use) restarts with guarded seeds only.
      The record lists the answers whose requests had withdrawn by the final
      round; they stay in the facts, so replay seeds them as confirmed.
    - *guarded*: otherwise only answers of probes whose results every rule
      reads joined with their request (layers._check_answer_reads) are
      seeded. A probe kind without a fingerprint is never seeded (nor
      cached): it re-runs every configure. An answer is confirmed when its request arises and its key,
      computed from the confirmed answers, is unchanged; else it is dropped
      and the request is answered as usual. An answer whose request never
      arises derives nothing and is removed from the final input facts.
    Under the cache and prerequisite obligations, either way the derived
    non-result facts are those of an unseeded run. Retained inert guarded
    answers can differ:
    - a round answers unguarded requests only when no guarded request is
      pending (their answers are not inert, so they must not depend on the
      seeds), and a round that drops a stale seed answers nothing;
    - a seeded run that fails a driver check reruns unseeded.
    BUILDLOG_SEED_PROBES=0 turns seeding off. With `fresh`, no seed and no
    cache row is ever read: every probe request is executed (rows are still
    written for later runs), also in the restarts of a failed check.
    """
    ctx = ProbeContext(builddir=builddir, workdir=builddir / "probe-work")
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    env_dir = builddir / "env-facts"
    if env_dir.exists():
        shutil.rmtree(env_dir)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_state = EnvWriteState()
    cache_path = builddir / "probe-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    watch_memo: dict = {}
    metrics: dict = {"rounds": [], "executed": 0, "cache_hits": 0}
    result_rels = {rel for k in kinds for rel in k.results}
    answers, replay = (
        _seed(builddir, kinds, cache, program_digest, watch_memo)
        if seed and not fresh
        else ({}, None)
    )
    if not allow_replay:
        replay = None
    mode = "replay" if replay is not None else ("guarded" if answers else None)
    facts_in = None
    if answers:
        write_env(env_dir, answers, env_state)
        facts_in = materialize(env_dir, 0)
        if replay is not None and replay.get("static") != static_digest(facts_in, result_rels):
            replay, mode = None, "guarded"
        if replay is None:
            unguarded = {k.name for k in kinds if k.unguarded}
            before = len(answers)
            answers = {rk: a for rk, a in answers.items() if a.kind.name not in unguarded}
            if len(answers) != before:
                write_env(env_dir, answers, env_state)
                facts_in = materialize(env_dir, 0)
            mode = "guarded" if answers else None
        else:
            # The previous final facts held these answers though their requests
            # had withdrawn, so the replayed model holds them too.
            for name, rid in replay.get("withdrawn", []):
                if (a := answers.get((name, rid))) is not None:
                    a.confirmed = True
    metrics["seed"] = {"mode": mode, "seeded": len(answers)}
    round_no = 0

    def check(violations: list[Violation]):
        """Raise the round's violations; a seeded run that fails a check
        restarts unseeded and reports that retry's outcome. Without passing
        checks, this does not establish cached/fresh outcome equivalence."""
        try:
            raise_violations(violations)
        except SystemExit:
            if mode is None:
                raise
            out, again = run_fixpoint(
                builddir,
                derive_round,
                materialize,
                kinds,
                shuffle_seed=shuffle_seed,
                program_digest=program_digest,
                seed=False,
                fresh=fresh,
            )
            again["seed"] = {**metrics["seed"], "restarted": True, "reason": "check"}
            return out, again
        return None

    while True:
        if facts_in is None:
            facts_in = materialize(env_dir, round_no)
        out = derive_round(facts_in, round_no)
        requests: dict[ReqKey, tuple[Kind, tuple[str, ...]]] = {}
        violations: list[Violation] = []
        counts = {}
        for k in kinds:
            reqs = read_rows(out / f"{k.req}.csv")
            domain = set(read_rows(out / f"{k.domain}.csv"))
            outside = sorted(set(reqs) - domain)
            if outside:
                sample = "\n  ".join("\t".join(r) for r in outside[:5])
                violations.append(
                    Violation(
                        0,
                        k.name,
                        tuple(outside[0]),
                        f"request outside finite domain for {k.name}:\n  {sample}",
                    )
                )
            tuples_by_id: dict[str, set[tuple[str, ...]]] = {}
            for r in reqs:
                tuples_by_id.setdefault(r[0], set()).add(tuple(r[1:]))
                requests[(k.name, r[0])] = (k, tuple(r[1:]))
            for rid, tuples in sorted(tuples_by_id.items()):
                if len(tuples) > 1:
                    shown = " and ".join(f"({', '.join(t)})" for t in sorted(tuples))
                    violations.append(
                        Violation(
                            1,
                            k.name,
                            (rid,),
                            f"request {rid} of {k.name} has two argument tuples "
                            f"{shown}: a request is identified by (kind, id) and "
                            "must have one argument tuple",
                        )
                    )
            counts[k.name] = {"requests": len(reqs)}
        if (fell_back := check(violations)) is not None:
            return fell_back
        # Confirm the seeded answers whose requests arose, in dependency order
        # (a key may read the answers it depends on); drop the stale ones.
        view = _result_state(answers, confirmed_only=True)
        progress = True
        while progress:
            progress = False
            for rk in requests:
                a = answers.get(rk)
                if (
                    a is not None
                    and not a.confirmed
                    and a.key is not None
                    and cache_key(a.kind, a.args, view) == a.key
                ):
                    a.confirmed = progress = True
                    metrics["cache_hits"] += 1
                    view = _result_state(answers, confirmed_only=True)
        stale = [rk for rk in requests if rk in answers and not answers[rk].confirmed]
        pending = [
            (k, rk[1], args)
            for rk, (k, args) in requests.items()
            if rk not in answers or rk in stale
        ]
        for k in kinds:
            counts[k.name]["new"] = sum(1 for p in pending if p[0] is k)
            counts[k.name]["deferred"] = 0
        unused = [rk for rk, a in answers.items() if not a.confirmed and rk not in requests]
        if replay is not None and (pending or stale or unused):
            # Unguarded answers may affect negation even after their requests
            # withdraw. Start over with only inert, guarded seeds, not the
            # replay's already-confirmed answers.
            out, again = run_fixpoint(
                builddir,
                derive_round,
                materialize,
                kinds,
                shuffle_seed=shuffle_seed,
                program_digest=program_digest,
                allow_replay=False,
                fresh=fresh,
            )

            again["seed"] = {
                **again["seed"],
                "restarted": True,
                "reason": again["seed"].get("reason", "replay"),
                "replay_seeded": metrics["seed"]["seeded"],
            }
            return out, again
        if stale:
            # A request may stem from a stale seed's rows, which the probes'
            # snapshot lacks: answer nothing until the model is recomputed.
            for rk in stale:
                del answers[rk]
            metrics["rounds"].append(counts | {"stale_round": True})
            write_env(env_dir, answers, env_state)
            facts_in = None
            round_no += 1
            continue
        # Unguarded answers are not inert: answer them only in a model with no
        # guarded request pending, so they do not depend on the seeds.
        batch = [p for p in pending if not p[0].unguarded]
        if not batch:
            batch = pending
        for k in kinds:
            counts[k.name]["deferred"] = sum(1 for p in pending if p[0] is k and p not in batch)
        metrics["rounds"].append(counts)
        if not pending:
            if unused:
                # Inert (their requests did not arise). Pruning preserves
                # non-result facts, not the unseeded run's answer history.
                for rk in unused:
                    del answers[rk]
                write_env(env_dir, answers, env_state)
                materialize(env_dir, round_no)
            metrics["seed"]["unused"] = len(unused)
            cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
            if program_digest is not None:
                record = {
                    "program": program_digest,
                    "static": static_digest(facts_in, result_rels),
                    "withdrawn": [[rk[0], rk[1]] for rk in answers if rk not in requests],
                    "answers": [
                        [a.kind.name, rk[1], list(a.args), a.key] for rk, a in answers.items()
                    ],
                }
                _save_record(builddir, record)
            metrics["final_round"] = round_no
            return out, metrics
        if shuffle_seed is not None:
            import random

            random.Random(shuffle_seed + round_no).shuffle(batch)
        # Every probe of the round reads the answers of earlier rounds only
        # (and the seeded answers confirmed above), never another's answer
        # from this round, so the order of `pending` cannot matter.
        results = _snapshot(view)
        violations = []
        for k, req_id, args in batch:
            key = cache_key(k, args, results)
            cached = cache.get(key) if key is not None and not fresh else None
            if cached is not None and not watch_holds(cached, watch_memo):
                cached = None
            if cached is not None and has_answer(k, req_id, _cached_rows(cached)):
                rows_by_rel = _cached_rows(cached)
                metrics["cache_hits"] += 1
            else:
                rows_by_rel = dict(k.fn(ctx, req_id, args, results))
                watch = rows_by_rel.pop(WATCH_KEY, [])
                undeclared = sorted(set(rows_by_rel) - set(k.results) - {ORIGIN_KEY})
                if undeclared:
                    violations.append(
                        Violation(2, k.name, (req_id, *args), undeclared_message(k, undeclared))
                    )
                    continue
                if not has_answer(k, req_id, rows_by_rel):
                    violations.append(
                        Violation(3, k.name, (req_id, *args), str(no_answer_error(k, req_id, args)))
                    )
                    continue
                if key is not None:
                    cache[key] = {
                        rel: [
                            [r[0], list(r[1]), r[2]] if rel == ORIGIN_KEY else list(r) for r in rows
                        ]
                        for rel, rows in rows_by_rel.items()
                    }
                    if watch:
                        cache[key][WATCH_KEY] = [list(w) for w in watch]
                metrics["executed"] += 1
            answers[(k.name, req_id)] = Answer(k, args, key, rows_by_rel, True)
        if (fell_back := check(violations)) is not None:
            return fell_back
        write_env(env_dir, answers, env_state)
        facts_in = None
        round_no += 1
