# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import providers, prune, rule_index, souffle_cache, uptodate
from .available import Certificate
from .discovery import Kind, kinds_from_probes, run_fixpoint
from .emit import RELATIONS as EMIT_RELATIONS
from .errors import BuildlogError, LoaderError
from .facts import (
    MISSING_ENTRY_POINT,
    OPTION_PREFIX,
    SPEC_WARNING,
    collect_profiles,
    default_vars,
    load_source_project,
    load_toml_file,
    package_vars,
    project_config_file,
    project_dl_files,
    project_fact_rows,
    project_source_root,
    project_spec,
    read_project_config,
    read_rows,
    schema_from_rules,
    source_projects,
    spec_files,
    spec_relations,
    tuple_key,
    user_file_parts,
    write_rows,
)
from .layers import Layer, PackageRef, Program, compose_program, composed_errors, package_files
from .packages import (
    LOCKFILE,
    PACKAGE_PATH_ENV,
    Package,
    check_lock,
    resolve_packages,
    search_prefixes,
    write_lock,
)
from .paths import require_souffle
from .provenance.program import write_sources

__all__ = ["BuildlogError", "LoaderError", "configure_project", "prepare_program"]

# Relations the driver reads besides the emitter's L0: diagnostics and the
# outer-loop hook (rule packages discovered by probes).
DISCOVERED = "discovered_rule_package"
DRIVER_RELATIONS = (*EMIT_RELATIONS, "error", "global_error", "warning", DISCOVERED)
# Relations whose rows only the driver writes (manifest key `driver`).
DRIVER_INPUTS = (
    "configure_input",
    "configure_arg",
    "builtin_argv",
    "fact_key",
    "target_qualifier",
    "package_provider",
    "config_select",
    "author_decision",
    "source_target_input",
    "source_imported_input",
    "source_alias_input",
    "module_manifest",
    "module_root",
    "module_requires",
    "module_import",
    "module_fact_key",
    *(f"av_pkg_{name.removeprefix('res_pkg_')}" for name in providers.RELATIONS),
    "av_pkg_bundle",
    "av_pkg_root",
    "av_pkg_spelling",
    "av_pkg_offer",
    "av_pkg_same",
    "av_pkg_invalid",
    "av_pkg_platform",
    "spec_warning",
)

STATE_FILE = "buildlog-state.json"
COMPOSE_CACHE_DIR = "compose-cache"
PRUNE_DIR = "pruned"
# BUILDLOG_NO_PRUNE=1 evaluates the composed program as it is; with
# BUILDLOG_PRUNE_REPORT=1 the pruning statistics are computed even when the
# pruned program is not the one evaluated (metrics["prune"]).
NO_PRUNE_ENV = "BUILDLOG_NO_PRUNE"
PRUNE_REPORT_ENV = "BUILDLOG_PRUNE_REPORT"


def _read_state(builddir: Path) -> dict:
    path = builddir / STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(
    builddir: Path,
    *,
    user_files: list[Path],
    var_overrides: dict[str, str],
    configs: list[str] | None,
    active: list[str],
    discovered: list[tuple[Path, str]] = (),
) -> None:
    builddir.mkdir(parents=True, exist_ok=True)
    options = {
        k[len(OPTION_PREFIX) :]: v for k, v in var_overrides.items() if k.startswith(OPTION_PREFIX)
    }
    variables = {k: v for k, v in var_overrides.items() if not k.startswith(OPTION_PREFIX)}
    (builddir / STATE_FILE).write_text(
        json.dumps(
            {
                "user_files": [str(p.resolve()) for p in user_files],
                "vars": dict(sorted(variables.items())),
                **({"options": dict(sorted(options.items()))} if options else {}),
                "configs": configs,
                "active_configs": active,
                "discovered": [[str(p), reason] for p, reason in discovered],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _effective_user_files(builddir: Path, user_files: list[Path] | None) -> list[Path]:
    if user_files is not None:
        return [p.expanduser().resolve() for p in user_files]
    state = _read_state(builddir)
    return [Path(p).expanduser().resolve() for p in state.get("user_files", [])]


def _effective_configs(builddir: Path, configs: list[str] | None) -> list[str] | None:
    """--config replaces the recorded selection; without it, a reconfigure
    keeps the build directory's (bug B2), like --user and -D. None: the
    default (config_default, else every declared configuration)."""
    if configs is not None:
        return list(configs)
    raw = _read_state(builddir).get("configs")
    return [str(c) for c in raw] if isinstance(raw, list) else None


def _effective_var_overrides(
    builddir: Path, overrides: dict[str, str] | None, options: dict[str, str] | None = None
) -> dict[str, str]:
    """-D values, else the recorded ones; and -O values (under
    OPTION_PREFIX), else the recorded ones. Each replaces its own record."""
    state = _read_state(builddir)
    if overrides is not None:
        out = dict(overrides)
    else:
        raw = state.get("vars", {})
        out = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    if options is None:
        raw = state.get("options", {})
        options = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    out.update({f"{OPTION_PREFIX}{k}": v for k, v in options.items()})
    return out


def project_uses(project: Path) -> list[str]:
    config = project_config_file(project)
    if config is None:
        raise BuildlogError(f"{project / 'buildlog.toml'}: {MISSING_ENTRY_POINT}")
    uses = read_project_config(config).get("uses", [])
    if not isinstance(uses, list) or not all(isinstance(u, str) for u in uses):
        raise LoaderError([f"{config}: uses must be a list of package names"])
    return uses


def project_packages(
    project: Path,
    package_paths: list[Path] | None = None,
    discovered: list[tuple[Path, str]] = (),
) -> list[Package]:
    """Resolve the packages a project loads."""
    return resolve_packages(
        project,
        project_uses(project),
        spec_relations(project),
        search_prefixes(package_paths),
        discovered,
    )


def prepare_program(
    builddir: Path,
    packages: list[PackageRef],
    project_files: list[Path] | None = None,
    layer_cache: dict | None = None,
    *,
    rounds: SouffleRounds | None = None,
    cache_dir: Path | None = None,
    explicit_manifests: frozenset[Path] | None = None,
) -> Program:
    """Qualify, check and compose the layers; raise LoaderError on any check error.
    With `cache_dir`, a composition of unchanged inputs is restored from there."""
    program = compose_program(
        packages,
        builddir / "program",
        builddir / "layer-check",
        required_outputs=DRIVER_RELATIONS,
        required_inputs=DRIVER_INPUTS,
        project_files=project_files,
        layer_cache=layer_cache,
        composed_check=rounds.check_composed if rounds is not None and rounds.serve else None,
        cache_dir=cache_dir,
        explicit_manifests=explicit_manifests,
    )
    if program.errors:
        raise LoaderError(program.errors)
    write_sources(builddir / "program", program)
    return program


# Soufflé runs of the configure rounds: (purpose, seconds). "translate" is a
# --serve session's parse, check and translation (spawn to ready), "round" an
# evaluation (with --serve: request to done; else the whole run). For
# scripts/configure_timing.py; nothing reads it otherwise.
SOUFFLE_TIMES: list[tuple[str, float]] = []
SERVE_ENV = "BUILDLOG_SOUFFLE_SERVE"
DEFER_ENV = "BUILDLOG_SOUFFLE_DEFER"
_SERVE = "souffle-serve: "
# Per binary identity: (has --serve, has --serve-deferred-output), from one --help.
_capabilities: dict[tuple, tuple[bool, bool]] = {}
# Skip MinimiseProgramTransformer (about 0.1 s of translation per pass; the
# outputs are identical across the tests and the case-study goldens).
_SOUFFLE_FLAGS = ["-z", "MinimiseProgramTransformer"]


def _binary_capabilities(souffle: Path) -> tuple[bool, bool]:
    """Whether the Soufflé binary has --serve and --serve-deferred-output (the
    patched engine's); remembered per binary identity for the life of the
    process, from a single --help."""
    real = souffle.resolve()
    st = real.stat()
    ident = (str(real), st.st_size, st.st_mtime_ns, st.st_ino)
    if ident not in _capabilities:
        cp = subprocess.run([str(real), "--help"], text=True, capture_output=True, check=False)
        _capabilities[ident] = ("--serve" in cp.stdout, "--serve-deferred-output" in cp.stdout)
    return _capabilities[ident]


def _serve_supported(souffle: Path) -> bool:
    if os.environ.get(SERVE_ENV, "1") == "0":
        return False
    return _binary_capabilities(souffle)[0]


def _defer_supported(souffle: Path) -> bool:
    """Whether serve sessions may keep an evaluation's other outputs until
    DUMP (BUILDLOG_SOUFFLE_DEFER=0 opts out)."""
    if os.environ.get(DEFER_ENV, "1") == "0":
        return False
    return _serve_supported(souffle) and _binary_capabilities(souffle)[1]


SPARE_ROUNDS = 3


def _displace(out: Path, stamp: Path) -> None:
    """Clear `out` for a round moved there. A stamped round already in `out`
    is kept as a spare under its own key, which another outer pass of the
    program (its rounds share these numbers) can reuse; otherwise a warm
    configure's passes would evict each other's rounds."""
    spare: Path | None = None
    try:
        spare = out.parent / f"spare-{json.loads(stamp.read_text())['inputs'][:16]}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if spare is not None and out.is_dir():
        shutil.rmtree(spare, ignore_errors=True)
        spare.with_suffix(".key").unlink(missing_ok=True)
        out.rename(spare)
        stamp.rename(spare.with_suffix(".key"))
        os.utime(spare.with_suffix(".key"))
        spares = sorted(out.parent.glob("spare-*.key"), key=lambda k: k.stat().st_mtime_ns)
        for old in spares[:-SPARE_ROUNDS]:
            shutil.rmtree(old.with_suffix(""), ignore_errors=True)
            old.unlink(missing_ok=True)
        return
    stamp.unlink(missing_ok=True)
    shutil.rmtree(out, ignore_errors=True)


def _prepare_out(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def run_souffle(program: Path, facts: Path, out: Path) -> None:
    _prepare_out(out)
    t0 = time.perf_counter()
    cp = subprocess.run(
        [
            str(require_souffle()),
            "-w",
            *_SOUFFLE_FLAGS,
            "-F",
            str(facts),
            "-D",
            str(out),
            str(program),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    SOUFFLE_TIMES.append(("round", time.perf_counter() - t0))
    if cp.returncode:
        raise SystemExit((cp.stdout or "") + (cp.stderr or ""))


ROUND_MEMO_ENV = "BUILDLOG_ROUND_MEMO"
# Bump when what the round memo's key or stamp covers changes. A stamp is
# complete (every output of the program) or control (only the relations the
# discovery loop reads); a control stamp never satisfies a request for all.
MEMO_FORMAT = "3"


def _memo_stamp(out: Path) -> Path:
    return out.with_name(out.name + ".key")


def _listing(out: Path) -> list:
    """The output directory as written: each entry's name, size and mtime."""
    return [
        [f.name, st.st_size, st.st_mtime_ns] for f in sorted(out.iterdir()) for st in [f.stat()]
    ]


# Digests of the fact files materialize_static wrote: path -> name -> (sha256
# of the content, size). Only the writer of a directory vouches for it; an
# entry is used only while the directory lists exactly those files with those
# sizes, and any other directory is hashed in full.
_WRITTEN_DIGESTS: dict[str, dict[str, tuple[str, int]]] = {}


def _file_digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _dir_digests(facts: Path) -> dict[str, str]:
    """name -> content digest of every file in `facts` but the source map."""
    known = _WRITTEN_DIGESTS.get(str(facts))
    found: dict[str, str] = {}
    for f in sorted(facts.iterdir(), key=lambda f: f.name):
        if f.name == "source_map.json":
            continue
        entry = known.get(f.name) if known is not None else None
        if entry is not None and f.stat().st_size == entry[1]:
            found[f.name] = entry[0]
        else:
            found[f.name] = _file_digest(f)[0]
    return found


def _read_dir(facts: Path, names: dict[str, str]) -> dict[str, bytes]:
    return {name: (facts / name).read_bytes() for name in names}


@dataclass
class _Last:
    """The latest round: where its outputs are, the memo key of the inputs it
    was given (taken before it was evaluated), whether the outputs are
    complete, and what can still complete them."""

    out: Path
    key: str | None
    complete: bool
    # A serve process holds this very evaluation's other outputs (until DUMP).
    retained: bool = False
    # The exact inputs of a control memo hit, for the evaluation it replaces.
    snapshot: dict[str, bytes] | None = None


class SouffleRounds:
    """The rounds of one discovery fixpoint, which all evaluate the same
    program: with `souffle --serve`, the program is parsed, checked and
    translated once and each round is one request to the same process.
    The outputs are those of separate runs (run_souffle), which remain the
    fallback when the binary lacks --serve or BUILDLOG_SOUFFLE_SERVE=0.

    A round given `control` (the relations the discovery loop reads) may,
    with --serve-deferred-output, write only those and keep the others in the
    process; `finish` then dumps them into the same output directory once the
    pass is known to be final. Without the feature, every round is complete."""

    def __init__(self, program: Path, program_digest: str | None = None) -> None:
        self.program = program
        # With the program's digest, a round whose inputs equal an earlier
        # round's (a warm configure's round 0 and the last configure's final
        # round) reuses that round's outputs instead of evaluating again.
        self.set_program_digest(program_digest)
        self.proc: subprocess.Popen | None = None
        self.startups = 0
        self.evaluations = 0
        self.stderr: list[str] = []
        self.stdout: list[str] = []
        self._reader: threading.Thread | None = None
        self.returncode: int | None = None
        self.serve = _serve_supported(require_souffle())
        self.defer = self.serve and _defer_supported(require_souffle())
        # Every output relation of the program: what a complete round holds.
        self.outputs: tuple[str, ...] | None = None
        self._last: _Last | None = None
        # Called before a round is evaluated by the process (a memo hit does not).
        self.on_evaluate: Callable[[], None] | None = None
        # Before the first evaluation, when no process holds the program yet:
        # the pruned program to evaluate instead (None: the program as it is).
        # The first evaluation falls back to the program if the pruned one fails.
        self.pruner: Callable[[], Path | None] | None = None
        self.unpruned: Path | None = None
        self.prune_stats: dict | None = None

    def set_outputs(self, outputs: list[str]) -> None:
        self.outputs = tuple(outputs)

    def _serve_args(self) -> list[str]:
        return ["--serve", *(["--serve-deferred-output"] if self.defer else [])]

    def set_program_digest(self, program_digest: str | None) -> None:
        self.program_digest = program_digest if os.environ.get(ROUND_MEMO_ENV, "1") != "0" else None

    def check_composed(self, program: Path, layers: list[Layer], reads: list[Path]) -> list[str]:
        """Validate at serve startup on a cache miss, keeping the process for rounds."""
        assert program == self.program

        def start() -> souffle_cache.Result:
            try:
                self.start_idle(program.parent / "serve-facts", program.parent / "serve-out")
            except SystemExit:
                return souffle_cache.Result(
                    self.returncode or 1, "".join(self.stdout), "".join(self.stderr)
                )
            return souffle_cache.Result(0, "", "")

        result = souffle_cache.run(
            ["-w", *self._serve_args(), *_SOUFFLE_FLAGS, str(program)],
            [program, *reads],
            program.parent,
            keep_stdout=False,
            runner=start,
        )
        return composed_errors(result.stdout, result.stderr, layers) if result.returncode else []

    def _start(self, facts: Path, out: Path) -> None:
        t0 = time.perf_counter()
        self.startups += 1
        self.proc = subprocess.Popen(
            [
                str(require_souffle()),
                "-w",
                *self._serve_args(),
                *_SOUFFLE_FLAGS,
                "-F",
                str(facts),
                "-D",
                str(out),
            ]
            + [str(self.program)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Drained concurrently, so a chatty stderr cannot block the process.
        assert self.proc.stderr is not None
        stream = self.proc.stderr
        self._reader = threading.Thread(target=lambda: self.stderr.extend(stream), daemon=True)
        self._reader.start()
        self._expect("ready")
        SOUFFLE_TIMES.append(("translate", time.perf_counter() - t0))

    def _expect(self, word: str) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self._fail()
            if line.startswith(_SERVE + word):
                return
            self.stdout.append(line)

    def _fail(self) -> None:
        assert self.proc is not None
        self.returncode = self.proc.wait()
        if self._reader is not None:
            self._reader.join()
        message = "".join(self.stdout) + "".join(self.stderr)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except BrokenPipeError:
                    pass
        self.proc = None
        raise SystemExit(message)

    def _inputs(self, facts: Path) -> tuple[str, dict[str, str]]:
        """The memo key of a fact directory and the digest of each file in it.
        The key covers the binary, the program and every file's name and
        content digest. Digests come from the writer (materialize_static)
        when it vouches for the whole directory, else every file is hashed."""
        souffle = require_souffle().resolve()
        st = souffle.stat()
        h = hashlib.sha256()
        h.update(
            json.dumps(
                [
                    MEMO_FORMAT,
                    str(souffle),
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ino,
                    self.program_digest,
                ]
            ).encode()
        )
        # Every file Soufflé may read from the fact directory; the source map
        # is the driver's.
        digests = _dir_digests(facts)
        for name, digest in digests.items():
            h.update(name.encode() + b"\0" + digest.encode() + b"\0")
        return h.hexdigest(), digests

    def _inputs_key(self, facts: Path) -> str:
        return self._inputs(facts)[0]

    def _reuse(self, key: str, out: Path, control: frozenset[str] | None = None) -> bool | None:
        """Move an earlier round with these inputs to `out` (or find it there):
        True for a complete one, False for a control one (only for a `control`
        request), None when there is none. A round qualifies only by an
        explicit boolean `complete` and by the files it lists: a complete one
        lists every output of the program, a control one every relation asked
        for. A complete round is preferred and keeps its stamp when it serves
        a control request."""
        stamp = _memo_stamp(out)
        candidates = [stamp, *sorted(s for s in out.parent.glob("*.key") if s != stamp)]
        found: list[tuple[Path, dict, bool]] = []
        for cand in candidates:
            try:
                data = json.loads(cand.read_text())
            except (OSError, ValueError):
                continue
            src = cand.with_suffix("")
            complete = data.get("complete")
            if (
                data.get("inputs") != key
                or not isinstance(complete, bool)
                or not (complete or control is not None)
                or not src.is_dir()
                or _listing(src) != data.get("outputs")
            ):
                continue
            listed = {entry[0] for entry in data["outputs"]}
            needed = set(self.outputs or ()) if complete else set()
            needed.update(control or ())
            if not {f"{r}.csv" for r in needed} <= listed:
                continue
            found.append((cand, data, complete))
        if not found:
            return None
        cand, data, complete = next((f for f in found if f[2]), found[0])
        src = cand.with_suffix("")
        if src != out:
            _displace(out, stamp)
            cand.unlink()
            src.rename(out)
            stamp.write_text(json.dumps(data))
        return complete

    def _check_outputs(self, out: Path, control: frozenset[str] | None) -> None:
        """Every expected output file exists: Soufflé writes one per output
        relation, so a missing file is a failure, not an empty relation."""
        expected = control if control is not None else self.outputs
        missing = sorted(r for r in expected or () if not (out / f"{r}.csv").is_file())
        if missing:
            raise SystemExit(f"souffle did not write the outputs {', '.join(missing)} in {out}")

    def _stamp(self, out: Path, key: str | None, complete: bool) -> None:
        if key is not None:
            _memo_stamp(out).write_text(
                json.dumps({"inputs": key, "outputs": _listing(out), "complete": complete})
            )

    def run(self, facts: Path, out: Path, control: frozenset[str] | None = None) -> None:
        """Derive the round's outputs into `out`. With `control`, only those
        relations are needed now; the rest may be completed by `finish`."""
        if not self.defer or any(c in f"{facts}{out}" for c in "\t\n"):
            control = None
        self._last = None
        key, digests = self._inputs(facts) if self.program_digest is not None else (None, {})
        if key is not None:
            hit = self._reuse(key, out, control)
            if hit is not None:
                SOUFFLE_TIMES.append(("reused", 0.0))
                self._last = _Last(
                    out, key, hit, snapshot=None if hit else _read_dir(facts, digests)
                )
                return
        _memo_stamp(out).unlink(missing_ok=True)
        complete = self._evaluate(facts, out, control)
        self._check_outputs(out, control)
        self._last = _Last(out, key, complete, retained=not complete)
        self._stamp(out, key, complete)

    def finish(self, out: Path) -> None:
        """Make the latest round's outputs complete: dump what the process
        kept (the very evaluation of the round), or, for a control memo hit
        that has no process, evaluate its exact inputs again."""
        last = self._last
        assert last is not None and last.out == out, "finish is for the latest round"
        if last.complete:
            return
        self._last = None
        _memo_stamp(out).unlink(missing_ok=True)
        if last.retained:
            if self.proc is None or self.proc.stdin is None:
                raise SystemExit("souffle: the evaluator holding the round's outputs is gone")
            t0 = time.perf_counter()
            try:
                self.proc.stdin.write("DUMP\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                self._fail()
            self._expect("dumped")
            SOUFFLE_TIMES.append(("dump", time.perf_counter() - t0))
        else:
            assert last.snapshot is not None
            replay = out.with_name(out.name + ".replay")
            shutil.rmtree(replay, ignore_errors=True)
            replay.mkdir(parents=True)
            try:
                for name, data in last.snapshot.items():
                    (replay / name).write_bytes(data)
                self._evaluate(replay, out, None)
            finally:
                shutil.rmtree(replay, ignore_errors=True)
        self._check_outputs(out, None)
        self._stamp(out, last.key, True)
        self._last = _Last(out, last.key, True)

    def _evaluate(self, facts: Path, out: Path, control: frozenset[str] | None = None) -> bool:
        """Evaluate; whether `out` is complete."""
        if self.on_evaluate is not None:
            self.on_evaluate()
        if self.pruner is not None:
            pruner, self.pruner = self.pruner, None
            pruned = pruner() if self.proc is None else None
            if pruned is not None:
                self.unpruned, self.program = self.program, pruned
        if self.unpruned is None:
            return self._evaluate_program(facts, out, control)
        try:
            return self._evaluate_program(facts, out, control)
        except SystemExit:
            # Whatever went wrong with the pruned program, the program as
            # composed gives the outcome.
            self.program, self.unpruned = self.unpruned, None
            self.stdout.clear()
            self.stderr.clear()
            self.returncode = None
            self.kill()
            return self._evaluate_program(facts, out, control)
        finally:
            self.unpruned = None

    def _evaluate_program(
        self, facts: Path, out: Path, control: frozenset[str] | None = None
    ) -> bool:
        self.evaluations += 1
        if not self.serve or any(c in f"{facts}{out}" for c in "\t\n"):
            self.startups += 1
            run_souffle(self.program, facts, out)
            return True
        _prepare_out(out)
        if self.proc is None:
            self._start(facts, out)
        assert self.proc is not None and self.proc.stdin is not None
        t0 = time.perf_counter()
        request = f"{facts}\t{out}"
        if control is not None:
            request += f"\tdefer={','.join(sorted(control))}"
        try:
            self.proc.stdin.write(request + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            self._fail()
        self._expect("done")
        SOUFFLE_TIMES.append(("round", time.perf_counter() - t0))
        return control is None

    def start_idle(self, facts: Path, out: Path) -> None:
        """Spawn the --serve process now (parse, check and translate), before
        its first round; a no-op without --serve."""
        if self.serve and self.proc is None:
            facts.mkdir(parents=True, exist_ok=True)
            out.mkdir(parents=True, exist_ok=True)
            self._start(facts, out)

    def kill(self) -> None:
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()
        if self._reader is not None:
            self._reader.join()
        if proc.stderr is not None:
            proc.stderr.close()

    def close(self) -> None:
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        if self._reader is not None:
            self._reader.join()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


StaticFacts = tuple[
    dict[str, set[tuple[str, ...]]], dict[str, list[str]], dict[tuple[str, str], str]
]


# The round-dir contract (input-fact writes):
#   - facts-in/round-N is a complete input for `souffle -w -F`: a relation
#     with no rows has no file, and -w makes Soufflé read a missing file as
#     empty. Every Soufflé call over such a dir passes -w.
#   - Only the last round's dir exists once configure returns. Round N+1's
#     dir is round N's, renamed, with only the files that differ rewritten
#     (relations whose rows differ from the previous round's) or removed (now
#     empty); a rename is one
#     syscall and creating a file is ~0.3 ms. Nothing reads an earlier round:
#     `why`, the CLI and the tests read facts-in/round-<final_round> from
#     metrics.json, and the round memo and replay digest key on the contents
#     of the dir handed to Soufflé, never on its name.
@dataclass
class RoundDir:
    path: Path
    # The rows written to each relation that has probe answers or driver rows;
    # every other relation's file holds its static rows.
    rows: dict[str, set[tuple[str, ...]]]
    # The static origins and exact probe-map text that produced source_map.json.
    source_map_inputs: tuple[dict, str | None] | None = None
    # name -> (sha256, size) of each fact file in the dir, from its writer.
    digests: dict[str, tuple[str, int]] = field(default_factory=dict)


def _reread(rows: set[tuple[str, ...]]) -> set[tuple[str, ...]]:
    """The rows as read_rows reads them back from a file write_rows wrote
    (a row whose only value is empty is a blank line, and is dropped). Rows
    write_rows rejects are kept, for it to reject."""
    if any(c in value for row in rows for value in row for c in "\t\n\r"):
        return rows
    lines = io.StringIO("".join("\t".join(row) + "\n" for row in rows), newline="")
    return {tuple(row) for row in csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE) if row}


def materialize_static(
    project: Path,
    schema,
    env_dir: Path,
    out: Path,
    *,
    package_facts: list[Path] | None = None,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
    driver_rows: dict[str, set[tuple[str, ...]]] | None = None,
    driver_origins: dict[str, list[str]] | None = None,
    shuffle_seed: int | None = None,
    static: StaticFacts | None = None,
    previous: RoundDir | None = None,
    carried: tuple[StaticFacts, RoundDir] | None = None,
) -> tuple[StaticFacts, RoundDir]:
    """Write a round's input facts to `out`: the project's static facts
    (`static`, computed with facts.project_fact_rows when None) with the
    probe answers in `env_dir` and the driver's rows added. Returns the
    static facts, which do not change between the rounds of a fixpoint, and
    the dir written. Given the `previous` round's dir (of the same fixpoint),
    that dir becomes `out` and only the relations whose rows changed are
    rewritten (see the round-dir contract). `carried` is the static facts and
    last dir of the previous fixpoint (another program, perhaps another
    schema): its dir becomes `out` the same way, for a first round. Unused
    with a shuffle seed, whose files are rewritten in full."""
    if shuffle_seed is not None:
        carried = None
    old_rows: dict[str, set[tuple[str, ...]]] | None = None
    digests: dict[str, tuple[str, int]] = {}
    _WRITTEN_DIGESTS.pop(str(out), None)
    for old in (previous, carried[1] if carried is not None else None):
        if old is not None:
            _WRITTEN_DIGESTS.pop(str(old.path), None)
    if previous is not None:
        digests = dict(previous.digests)
    elif carried is not None and carried[1].path.is_dir():
        digests = dict(carried[1].digests)
    if previous is None and carried is not None and carried[1].path.is_dir():
        old_static, old_dir = carried
        if old_dir.path != out:
            if out.exists():
                shutil.rmtree(out)
            old_dir.path.rename(out)
        # What each file holds; no file (a relation the old schema did not
        # read, or without rows) is empty.
        old_rows = {
            rel: old_dir.rows[rel] if rel in old_dir.rows else old_static[0].get(rel, set())
            for rel in set(old_static[0]) | set(old_dir.rows)
            if (out / f"{rel}.facts").exists()
        }
        for rel in old_rows.keys() - schema.inputs:
            (out / f"{rel}.facts").unlink(missing_ok=True)
            digests.pop(f"{rel}.facts", None)
    elif previous is None:
        digests = {}
        if out.parent.exists():
            shutil.rmtree(out.parent)
        out.mkdir(parents=True)
    elif previous.path != out:
        if out.exists():
            shutil.rmtree(out)
        previous.path.rename(out)
    if static is None:
        static = project_fact_rows(
            project,
            schema,
            package_facts=package_facts,
            user_files=user_files,
            var_overrides=var_overrides,
        )
        for key, origins in (driver_origins or {}).items():
            static[1].setdefault(key, []).extend(origins)
    static_rows, static_map, author_decisions = static
    driver_rows = dict(driver_rows or {})
    if author_decisions:
        driver_rows.setdefault("author_decision", set()).update(
            (rel, key, reason) for (rel, key), reason in author_decisions.items()
        )
    extra = {f.stem: set(read_rows(f)) for f in sorted(env_dir.glob("*.facts"))}
    for rel, rows in driver_rows.items():
        extra.setdefault(rel, set()).update(rows)
    candidates = set(schema.inputs) if previous is None else (previous.rows.keys() | set(extra))
    written: dict[str, set[tuple[str, ...]]] = {}
    for rel in sorted(candidates & schema.inputs):
        rows = static_rows.get(rel, set())
        if rel in extra:
            # Each file is written once, with what writing the static rows,
            # reading them back and adding the extra rows would give.
            rows = _reread(rows) | extra[rel]
            written[rel] = rows
        if previous is not None and rows == previous.rows.get(rel, static_rows.get(rel, set())):
            continue
        if old_rows is not None and rows == old_rows.get(rel, set()):
            continue
        path = out / f"{rel}.facts"
        if rows:
            write_rows(path, rows, shuffle_seed)
            digests[path.name] = _file_digest(path)
        else:
            path.unlink(missing_ok=True)
            digests.pop(path.name, None)
    env_map = env_dir / "source_map.json"
    map_inputs = (static_map, env_map.read_text() if env_map.exists() else None)
    old_dir = previous if previous is not None else carried[1] if carried is not None else None
    if (
        old_dir is None
        or map_inputs != old_dir.source_map_inputs
        or not (out / "source_map.json").exists()
    ):
        source_map = {key: list(origins) for key, origins in static_map.items()}
        # Probe provenance (e.g. importer file:line) joins the TOML source map.
        if map_inputs[1] is not None:
            for key, origins in json.loads(map_inputs[1]).items():
                source_map.setdefault(key, []).extend(
                    o for o in origins if o not in source_map[key]
                )
        (out / "source_map.json").write_text(
            json.dumps(source_map, indent=2, sort_keys=True) + "\n"
        )
    # Any file without a digest (or a digest of a file since gone) is settled
    # against the directory itself.
    present = {f.name for f in out.iterdir()} - {"source_map.json"}
    digests = {n: digests[n] if n in digests else _file_digest(out / n) for n in sorted(present)}
    _WRITTEN_DIGESTS[str(out)] = digests
    return static, RoundDir(out, written, map_inputs, digests)


def _dist_metadata(dist: str) -> Path | None:
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        found = distribution(dist)
    except PackageNotFoundError:
        return None
    for f in found.files or []:
        if f.name == "METADATA" and f.parent.name.endswith(".dist-info"):
            return Path(str(found.locate_file(f))).resolve()
    return None


def provenance_facts(
    project: Path,
    schema,
    program: Program,
    *,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
) -> dict[str, set[tuple[str, ...]]]:
    """Where facts came from, for core's target-id rules:
    fact_key(origin, rel, key) with the first column of every spec row
    (origin "(spec)") and package facts row (origin: the package name),
    and target_qualifier(pkg, q), each loaded package's target namespace."""
    variables = default_vars(project, var_overrides)
    facts, tomls = spec_files(project)
    sources = [("(spec)", p) for p in (*facts, *tomls)]
    sources += [
        ("(user)", p) for p in user_files or [] if p.suffix == ".toml" or user_file_parts(p)[1]
    ]
    sources += [(layer.name, f) for layer in program.layers for f in layer.fact_files]
    keys: set[tuple[str, ...]] = set()
    module_keys: set[tuple[str, ...]] = set()
    profiles = collect_profiles(tomls)
    for origin, path in sources:
        if path.suffix == ".facts":
            rows = {path.stem: set(read_rows(path))}
        else:
            real, table = user_file_parts(path)
            known = variables if origin in ("(spec)", "(user)") else package_vars(path, variables)
            rows, _ = load_toml_file(
                real, schema, known, profiles if origin == "(spec)" else None, within=table
            )
        found = {
            (origin, rel, row[0])
            for rel, rs in rows.items()
            if rel != SPEC_WARNING
            for row in rs
            if row
        }
        scoped = program.modules.facts.get(path.resolve()) if program.modules is not None else None
        if scoped is not None:
            module_keys.update((scoped.module, *row) for row in found)
        else:
            keys |= found
    uses = project_uses(project)
    for name, directory, where in source_projects(user_files, variables):
        rows, _ = load_source_project(name, directory, schema, where, uses)
        origin = f"(source project {name})"
        keys |= {(origin, rel, row[0]) for rel, rs in rows.items() for row in rs if row}
    return {
        "fact_key": keys,
        "module_fact_key": module_keys,
        "target_qualifier": {
            (x.name, x.target_namespace) for x in program.layers if x.source != "project"
        },
        "package_provider": {(p.name,) for p in program.providers},
    }


def regeneration_facts(
    project: Path,
    packages: list[Package],
    package_paths: list[Path] | None,
    keep_going: bool,
    configs: list[str] | None = None,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
) -> dict[str, set[tuple[str, ...]]]:
    """configure_input(path) and configure_arg(pos, arg): what this configure
    read and how to rerun it from the Ninja directory (<builddir>/souffle).
    Shuffle seeds are test knobs and --update-lock must stay explicit, so
    neither is repeated. builtin_argv: how build commands run `buildlog -E`.
    config_select(name): the configurations picked with --config (none: the
    user's config_default, else all the spec declares; core L1)."""
    spec = project_spec(project)
    inputs = {*spec.inputs, *(user_file_parts(p)[0] for p in user_files or []), project / LOCKFILE}
    for _, directory, _ in source_projects(user_files, default_vars(project, var_overrides)):
        inputs.update(project_spec(directory).inputs)
    dists = {"buildlog"}
    for pkg in packages:
        inputs.update(package_files(pkg.candidate.manifest, pkg.data))
        if pkg.candidate.dist:
            dists.add(pkg.candidate.dist)
    inputs |= {m for m in map(_dist_metadata, sorted(dists)) if m is not None}
    env = os.environ.get(PACKAGE_PATH_ENV)
    argv = [
        "env",
        *([f"{PACKAGE_PATH_ENV}={env}"] if env is not None else ["-u", PACKAGE_PATH_ENV]),
    ]
    argv += [sys.executable, "-m", "buildlog", "configure", "-C", ".."]
    for path in package_paths or []:
        argv += ["--package-path", str(Path(path).resolve())]
    for path in user_files or []:
        _real, table = user_file_parts(path)
        if table:
            argv += ["--preset", table[-1]]
        else:
            argv += ["--user", str(Path(path).resolve())]
    for key, value in sorted((var_overrides or {}).items()):
        if key.startswith(OPTION_PREFIX):
            argv += ["-O", f"{key[len(OPTION_PREFIX) :]}={value}"]
        else:
            argv += ["-D", f"{key}={value}"]
    if keep_going:
        argv.append("--keep-going")
    for name in configs or []:
        argv += ["--config", name]
    argv.append(str(project))
    return {
        "configure_input": {(str(p.resolve()),) for p in inputs},
        "configure_arg": {(str(i), a) for i, a in enumerate(argv)},
        "builtin_argv": {
            (str(i), a) for i, a in enumerate([sys.executable, "-m", "buildlog", "-E"])
        },
        "config_select": {(name,) for name in configs or []},
    }


_ACTION_ERRORS = ("action", "argv", "tool reference", "template", "working directory", "test case")
_DECLARATION_ERRORS = ("unknown", "twice", "undeclared", "is not", "must be", "declares no")


def error_order(row: tuple[str, str]) -> tuple[int, str, str]:
    """Causes before consequences: a misspelt or undeclared name first, then
    what fails to resolve, then the actions that fail because of it."""
    kind = row[0]
    if kind.startswith(_ACTION_ERRORS):
        tier = 2
    elif any(w in kind for w in _DECLARATION_ERRORS):
        tier = 0
    else:
        tier = 1
    return (tier, *row)


def configure_project(
    project: Path,
    builddir: Path,
    *,
    emit: bool = True,
    check_only: bool = False,
    shuffle_seed: int | None = None,
    shuffle_probes: int | None = None,
    package_paths: list[Path] | None = None,
    update_lock: bool = False,
    keep_going: bool = False,
    configs: list[str] | None = None,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
    option_overrides: dict[str, str] | None = None,
    fresh: bool = False,
) -> tuple[Path, dict]:
    """Prepare one executable union and run its environmental fixpoint.

    Errors that layers attribute to targets/actions become failing edges and
    the rest of the graph is emitted; configure still exits 1 unless
    keep_going. A global (unattributed) error emits nothing and always exits
    1. With emit=False or check_only, any error exits 1."""
    project = project.resolve()
    builddir = builddir.resolve()
    builddir.mkdir(parents=True, exist_ok=True)
    effective_user_files = _effective_user_files(builddir, user_files)
    effective_var_overrides = _effective_var_overrides(builddir, var_overrides, option_overrides)
    configs = _effective_configs(builddir, configs)
    previous_active = _read_state(builddir).get("active_configs")
    lock_path = project / LOCKFILE
    prepared = rule_index.prepare_cached(
        project,
        effective_user_files,
        effective_var_overrides,
        project_packages(project, package_paths),
        search_prefixes(package_paths),
        builddir / "prepared.pkl",
    )
    certificate_path = builddir / "available-certificate.json"
    try:
        held = Certificate.from_json(json.loads(certificate_path.read_text()))
        if (
            held is not None
            and all(
                all(previous.get(key) == value for key, value in current.items())
                for current, previous in (
                    (prepared.certificate.files, held.files),
                    (prepared.certificate.directories, held.directories),
                    (prepared.certificate.paths, held.paths),
                )
            )
            and held.valid()
        ):
            prepared.certificate = held
    except (OSError, ValueError, TypeError):
        pass

    def save_certificate() -> None:
        prepared.certificate.confirm()
        text = json.dumps(prepared.certificate.to_json(), sort_keys=True) + "\n"
        if not certificate_path.exists() or certificate_path.read_text() != text:
            fd, name = tempfile.mkstemp(dir=builddir, prefix=".available-certificate-")
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(text)
                os.replace(temporary, certificate_path)
            finally:
                temporary.unlink(missing_ok=True)

    save_certificate()
    attempt = (
        uptodate.enabled()
        and emit
        and not check_only
        and not fresh
        and not update_lock
        and shuffle_seed is None
        and shuffle_probes is None
    )

    def resolve_again(found: list[tuple[Path, str]]):
        packages = prepared.packages
        check_lock(
            project,
            [p for p in packages if p.candidate.manifest.resolve() in prepared.explicit_manifests],
        )
        rows = regeneration_facts(
            project,
            packages,
            package_paths,
            keep_going,
            configs,
            effective_user_files,
            effective_var_overrides,
        )
        rows["configure_input"].add((str(certificate_path),))
        return packages, rows

    if attempt:
        held = uptodate.check(project, builddir, resolve_again, lock_path)
        if held is not None:
            return _up_to_date(builddir, held)
    uptodate.invalidate(builddir)
    packages = prepared.packages
    if not update_lock:
        check_lock(
            project,
            [p for p in packages if p.candidate.manifest.resolve() in prepared.explicit_manifests],
        )
    driver_rows = regeneration_facts(
        project,
        packages,
        package_paths,
        keep_going,
        configs,
        effective_user_files,
        effective_var_overrides,
    )
    driver_rows["configure_input"].add((str(certificate_path),))
    snap = (
        uptodate.snapshot(project, packages, driver_rows, lock_path) if uptodate.enabled() else None
    )
    derived = _derive(
        project,
        builddir,
        packages,
        driver_rows,
        shuffle_seed,
        shuffle_probes,
        user_files=effective_user_files,
        var_overrides=effective_var_overrides,
        fresh=fresh,
        prepared=prepared,
    )
    try:
        final, metrics = derived.final, derived.metrics
        derived.rounds.finish(final)
    finally:
        derived.rounds.close()
    save_certificate()
    if snap is not None:
        snap["files"][str(certificate_path)] = hashlib.sha256(
            certificate_path.read_bytes()
        ).hexdigest()
    used = {Path(directory).resolve() for _, directory in read_rows(final / f"{DISCOVERED}.csv")}
    known = [*packages, *prepared.metadata_packages]
    unknown = used - {p.candidate.manifest.parent.resolve() for p in known}
    if unknown:
        raise LoaderError(
            [
                f"{directory}: rule package was not certified by preparation; declare its metadata "
                "or rule root before inference (the executable program cannot change)"
                for directory in sorted(unknown)
            ]
        )
    used_packages = [
        p
        for p in known
        if p.candidate.manifest.resolve() in prepared.explicit_manifests
        or p.candidate.manifest.parent.resolve() in used
    ]
    by_name: dict[str, Package] = {}
    for package in used_packages:
        other = by_name.setdefault(package.name, package)
        if other.candidate.manifest.resolve() != package.candidate.manifest.resolve():
            raise LoaderError(
                [
                    (
                        f"{package.name}: used package found twice: {other.candidate.manifest}, "
                        f"{package.candidate.manifest}; bind one installed instance per package name"
                    )
                ]
            )
    if not update_lock:
        check_lock(project, used_packages)
    discovered = [
        (p.candidate.manifest.parent.resolve(), "used rule module")
        for p in used_packages
        if p.candidate.manifest.resolve() not in prepared.explicit_manifests
    ]
    write_lock(project, used_packages)
    active = sorted(n for (n,) in read_rows(final / "config_active.csv"))
    _write_state(
        builddir,
        user_files=effective_user_files,
        var_overrides=effective_var_overrides,
        configs=configs,
        active=active,
        discovered=discovered,
    )
    if isinstance(previous_active, list) and sorted(previous_active) != active:
        was = ", ".join(sorted(previous_active)) or "none"
        print(
            f"configuring {', '.join(active) or 'no configuration'} (was: {was})", file=sys.stderr
        )
    metrics["outer"] = [
        {
            "packages": [p.name for p in packages],
            "executed": metrics["executed"],
            "cache_hits": metrics["cache_hits"],
            "rounds": len(metrics["rounds"]),
            "seed": metrics.get("seed"),
        }
    ]
    metrics.update(
        composition_calls=1,
        compositions=int(not derived.loaded.cached),
        evaluator_startups=derived.rounds.startups,
        evaluations=derived.rounds.evaluations,
        available_bundles=len(prepared.files),
    )
    final_dir = builddir / "souffle" / "facts"
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.copytree(final, final_dir)
    errors = read_rows(final_dir / "error.csv")
    global_errors = read_rows(final_dir / "global_error.csv")
    metrics["errors"] = errors
    metrics["global_errors"] = global_errors
    metrics["warnings"] = read_rows(final_dir / "warning.csv")
    metrics["failed_actions"] = len({a for a, _, _ in read_rows(final_dir / "action_failed.csv")})
    (builddir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    printed = [f"warning: {kind}: {detail}" for kind, detail in sorted(metrics["warnings"])]
    printed += [f"error: {kind}: {detail}" for kind, detail in sorted(errors, key=error_order)]
    for line in printed:
        print(line, file=sys.stderr)
    if errors and (check_only or not emit):
        raise SystemExit(1)
    if check_only:
        return final_dir, metrics
    if global_errors:
        # Nothing is attributable, so no part of the graph is trustworthy; a
        # stale build.ninja must not survive either.
        (builddir / "souffle" / "build.ninja").unlink(missing_ok=True)
        raise SystemExit(1)
    if errors and not keep_going:
        # The configured derivation is known to contain errors.  By default
        # configure fails before writing a graph; --keep-going asks for the
        # old partial graph with failing edges.
        (builddir / "souffle" / "build.ninja").unlink(missing_ok=True)
        raise SystemExit(1)
    if emit:
        from .emit import main as emit_main

        ninja_dir = builddir / "souffle"
        source_root = project_source_root(project, effective_var_overrides)
        emit_main(str(final_dir), str(source_root), str(ninja_dir))
        if (
            uptodate.enabled()
            and snap is not None
            and shuffle_seed is None
            and shuffle_probes is None
        ):
            uptodate.store(
                builddir,
                lock=lock_path,
                snap=snap,
                discovered=discovered,
                packages=packages,
                program_digest=derived.program_digest,
                probes=derived.loaded.probes,
                provider_decls=derived.loaded.providers,
                kinds=derived.kinds,
                metrics=metrics,
                printed=printed,
            )
    return final_dir, metrics


def _up_to_date(builddir: Path, held: dict) -> tuple[Path, dict]:
    """Report the stored outcome of a configure none of whose inputs changed:
    its warnings and errors again, its metrics marked, build.ninja as it is
    (touched only when an input is newer, so mtime-based regeneration stops)."""
    for line in held["printed"]:
        print(line, file=sys.stderr)
    metrics = dict(held["metrics"])
    metrics["last_configure"] = {k: metrics.get(k) for k in ("rounds", "outer", "seed")}
    metrics.update(
        up_to_date=True,
        executed=0,
        cache_hits=0,
        rounds=[],
        outer=[],
        seed=None,
        composition_calls=0,
        compositions=0,
        evaluator_startups=0,
        evaluations=0,
    )
    (builddir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    ninja = builddir / "souffle" / "build.ninja"
    stamp = ninja.stat().st_mtime_ns
    newest = max(
        (os.stat(p).st_mtime_ns for p in held["snapshot"]["files"] if os.path.exists(p)),
        default=0,
    )
    if newest > stamp:
        os.utime(ninja)
    return builddir / "souffle" / "facts", metrics


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _prune(
    builddir: Path, digest: str, loaded: Program, schema, static, driver_rows
) -> tuple[Path, dict] | None:
    """The pruned program for the facts this configure starts from (see
    prune.py), written under builddir/pruned, keyed by the program's digest
    and what can be non-empty; None when it cannot be pruned."""
    rows, _, decisions = static
    nonempty = {r for r in schema.inputs if rows.get(r) or driver_rows.get(r)}
    if decisions:
        nonempty.add("author_decision")
    probes = sorted((p.request, tuple(p.results), bool(p.unguarded)) for p in loaded.probes)
    key = hashlib.sha256(
        json.dumps([prune.FORMAT, digest, sorted(nonempty), probes]).encode()
    ).hexdigest()[:32]
    directory = builddir / PRUNE_DIR
    text, meta = directory / f"{key}.dl", directory / f"{key}.json"
    try:
        return text, json.loads(meta.read_text())
    except (OSError, ValueError):
        pass
    result = prune.prune_program(builddir / "program", nonempty, probes)
    if result is None:
        return None
    stats = {
        "clauses": list(result.clauses),
        "decls": list(result.decls),
        "inputs": list(result.inputs),
    }
    directory.mkdir(parents=True, exist_ok=True)
    for old in sorted(directory.glob("*.dl"), key=lambda f: f.stat().st_mtime)[:-7]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    text.write_text(result.text)
    meta.write_text(json.dumps(stats))
    return text, stats


@dataclass
class Derived:
    """The fixed program's result; the caller completes outputs and closes it."""

    final: Path
    metrics: dict
    loaded: Program
    kinds: list[Kind]
    rounds: SouffleRounds
    program_digest: str


def _derive(
    project: Path,
    builddir: Path,
    packages: list[Package],
    driver_rows: dict[str, set[tuple[str, ...]]],
    shuffle_seed: int | None,
    shuffle_probes: int | None,
    *,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
    fresh: bool = False,
    prepared: rule_index.Prepared,
) -> Derived:
    """One environmental fixpoint over the complete prepared program."""
    souffle = SouffleRounds(builddir / "program" / "build.dl")

    try:
        loaded = prepare_program(
            builddir,
            [p.ref for p in packages],
            project_dl_files(project),
            rounds=souffle,
            cache_dir=builddir / COMPOSE_CACHE_DIR,
            explicit_manifests=prepared.explicit_manifests,
        )
        program_digest = _tree_digest(builddir / "program")
        souffle.set_program_digest(program_digest)
        assert loaded.build is not None
        souffle.set_outputs(loaded.outputs)
        schema = schema_from_rules([loaded.build.parent])
        schema.probe_results = {rel for probe in loaded.probes for rel in probe.results}
        schema.driver_inputs = set(loaded.driver_inputs)
        schema.user_inputs = {rel for layer in loaded.layers for rel in layer.user}
        if loaded.modules is not None:
            schema.module_facts = loaded.modules.facts
        if loaded.explicit_manifests is not None:
            schema.author_relations = {
                rel for layer in loaded.schema_layers for rel in layer.info.decls
            }
            schema.author_inputs = {
                rel
                for layer in loaded.schema_layers
                for rel in (
                    layer.accepts
                    | layer.user
                    | layer.driver
                    | {r for probe in layer.probes for r in probe.results}
                )
            }
        driver_rows = {
            **driver_rows,
            **provenance_facts(
                project,
                schema,
                loaded,
                user_files=user_files,
                var_overrides=var_overrides,
            ),
        }
        if loaded.modules is not None:
            for relation, values in loaded.modules.rows.items():
                driver_rows.setdefault(relation, set()).update(values)
        driver_origins: dict[str, list[str]] = {}
        available = rule_index.available_rows(prepared, providers.static_vocabulary(loaded.layers))
        for relation, values in available.items():
            if relation != "__origin__":
                driver_rows.setdefault(relation, set()).update(tuple(row) for row in values)
        for relation, row, origin in available.get("__origin__", []):
            driver_origins.setdefault(tuple_key(relation, tuple(row)), []).append(origin)
        kinds = kinds_from_probes(loaded.probes)
        control = frozenset({DISCOVERED, *(k.req for k in kinds), *(k.domain for k in kinds)})
        providers.activate(loaded.providers)
        package_facts = [f for layer in loaded.layers for f in layer.fact_files]
        static: StaticFacts | None = None
        current: RoundDir | None = None

        def materialize(env_dir: Path, round_no: int) -> Path:
            nonlocal static, current
            out = builddir / "facts-in" / f"round-{round_no}"
            static, current = materialize_static(
                project,
                schema,
                env_dir,
                out,
                package_facts=package_facts,
                user_files=user_files,
                var_overrides=var_overrides,
                driver_rows=driver_rows,
                driver_origins=driver_origins,
                shuffle_seed=shuffle_seed,
                static=static,
                previous=current,
            )
            return out

        def pruned_program(apply: bool = True) -> Path | None:
            if static is None or (apply and os.environ.get(NO_PRUNE_ENV) == "1"):
                return None
            result = _prune(builddir, program_digest, loaded, schema, static, driver_rows)
            souffle.prune_stats = result[1] if result is not None else None
            return result[0] if result is not None else None

        if os.environ.get(NO_PRUNE_ENV) != "1":
            souffle.pruner = pruned_program

        def derive_round(facts_in: Path, round_no: int) -> Path:
            out = builddir / "rounds" / f"round-{round_no}"
            souffle.run(facts_in, out, control)
            return out

        final, metrics = run_fixpoint(
            builddir,
            derive_round,
            materialize,
            kinds,
            shuffle_seed=shuffle_probes,
            program_digest=program_digest,
            fresh=fresh,
        )
        if os.environ.get(PRUNE_REPORT_ENV) == "1" and souffle.prune_stats is None:
            pruned_program(apply=False)
        metrics["prune"] = souffle.prune_stats
        return Derived(final, metrics, loaded, kinds, souffle, program_digest)
    except BaseException:
        souffle.close()
        raise
