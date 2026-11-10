# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static pruning (prune.py): a configure that evaluates the pruned program
gives the build, facts and proofs of one that evaluates the composed program.

Each case study is configured twice in the same build directory path (the
path is part of the commands): with BUILDLOG_NO_PRUNE=1 and without. Both
configure cold, then lose their round memo and configure again, so the second
evaluation starts a Soufflé process on the program alone (a cold evaluation
is over the composed program, which the loader checks first). build.ninja,
tests.json and `why` are compared byte for byte; the facts file by file as
sets of rows, since Soufflé numbers symbols by first sight in the program
text, so a pruned program may write the same rows in another order."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog import prune
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CS = ROOT / "case-studies"
BUILDLOG = ROOT / ".venv" / "bin" / "buildlog"
HOMEBREW = Path("/opt/homebrew")

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(sys.platform != "darwin", reason="the studies' presets pin Apple clang"),
]

# name -> (project, user files, configs, BUILDLOG_PACKAGE_PATH)
STUDIES = {
    "zlib": (CS / "zlib/project", [CS / "zlib/project/presets/apple-clang-arm64.toml"], [], None),
    "fribidi": (
        CS / "fribidi/project",
        [CS / "fribidi/project/presets/apple-clang-arm64.toml"],
        [],
        None,
    ),
    "zlib-ng": (
        CS / "zlib-ng/project",
        [CS / "zlib-ng/project/presets/apple-clang.toml"],
        ["release"],
        None,
    ),
    "flatbuffers": (
        CS / "flatbuffers/project",
        [CS / "flatbuffers/project/presets/apple-clang.toml"],
        ["release"],
        None,
    ),
    "libhalide": (
        CS / "halide/libhalide",
        [CS / "halide/libhalide/presets/homebrew-arm64.toml"],
        [],
        CS / "halide/packages",
    ),
}


def run(argv, env, check=True):
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if check and done.returncode:
        raise RuntimeError(f"{' '.join(map(str, argv))}: {done.stderr[-600:]}")
    return done


def variant(name, tmp_path, no_prune):
    """Configure cold, then again without the round memo; the artifacts."""
    project, users, configs, packages = STUDIES[name]
    build, cache = tmp_path / "b", tmp_path / "cache"
    shutil.rmtree(build, ignore_errors=True)
    shutil.rmtree(cache, ignore_errors=True)
    env = {
        **os.environ,
        "BUILDLOG_CACHE_DIR": str(cache),
        "BUILDLOG_UPTODATE": "0",
        "BUILDLOG_PRUNE_REPORT": "1",
    }
    env.pop("BUILDLOG_PACKAGE_PATH", None)
    if packages:
        env["BUILDLOG_PACKAGE_PATH"] = str(packages)
    if no_prune:
        env["BUILDLOG_NO_PRUNE"] = "1"
    cmd = [str(BUILDLOG), "configure", "-C", str(build)]
    for u in users:
        cmd += ["--user", str(u)]
    for c in configs:
        cmd += ["--config", c]
    cmd.append(str(project))
    run(cmd, env)
    shutil.rmtree(build / "rounds")
    run(cmd, env)
    metrics = json.loads((build / "metrics.json").read_text())
    ninja = build / "souffle"
    facts = {f.name: f.read_bytes() for f in sorted((ninja / "facts").iterdir())}
    outputs = sorted((ninja / "facts" / "default_output.csv").read_text().splitlines())[:3]
    why = [run([str(BUILDLOG), "why", "-C", str(build), o], env).stdout for o in outputs]
    return {
        "ninja": (ninja / "build.ninja").read_bytes(),
        "tests": (ninja / "tests.json").read_bytes() if (ninja / "tests.json").exists() else None,
        "facts": facts,
        "why": why,
        "prune": metrics.get("prune"),
    }


@pytest.mark.parametrize("name", list(STUDIES))
def test_pruned_configure_equals_the_unpruned_one(name, tmp_path):
    project = STUDIES[name][0]
    if not (project / "buildlog.toml").exists():
        pytest.skip(f"{name}: no project")
    try:
        plain = variant(name, tmp_path, no_prune=True)
    except RuntimeError as exc:
        pytest.skip(f"{name}: not configurable here ({exc})")
    pruned = variant(name, tmp_path, no_prune=False)
    assert pruned["prune"] and pruned["prune"]["clauses"][1] < pruned["prune"]["clauses"][0]
    assert pruned["ninja"] == plain["ninja"]
    assert pruned["tests"] == plain["tests"]
    assert pruned["why"] == plain["why"]
    assert pruned["facts"].keys() == plain["facts"].keys()
    for rel, data in plain["facts"].items():
        assert sorted(pruned["facts"][rel].splitlines()) == sorted(data.splitlines()), rel


# --- the pruner (no Soufflé) ------------------------------------------------

PROGRAM = """
.decl a(x:symbol)
.decl b(x:symbol)
.decl c(x:symbol)
.decl d(x:symbol)
.decl e(n:number)
.decl f(x:symbol)
.decl res(x:symbol)
.decl req(x:symbol)
.input a
.input b
.input res
.output c
.output d
.output e
.output f
.output req
c(x) :- a(x), b(x).
d(x) :- a(x), !b(x).
e(n) :- n = count : { b(_) }.
e(n) :- n = count : b(_).
f(x) :- b(x), c(x).
f(x) :- a(x), (b(x) ; c(x)).
f(x) :- res(x), req(x).
req("r") :- a("x").
"""


def pruned(inputs, probes=()):
    result = prune.prune_text(PROGRAM, set(inputs), list(probes))
    assert result is not None
    return result.text


def clauses(text):
    return {line for line in text.splitlines() if ":-" in line}


def test_a_clause_over_an_empty_relation_is_dropped():
    text = pruned({"a"})
    assert "c(x) :- a(x), b(x)." not in clauses(text)
    assert "f(x) :- b(x), c(x)." not in clauses(text)


def test_a_negated_atom_does_not_block_a_clause():
    assert "d(x) :- a(x), !b(x)." in clauses(pruned({"a"}))


def test_aggregates_over_empty_relations_still_fire():
    text = clauses(pruned({"a"}))
    assert "e(n) :- n = count : { b(_) }." in text
    assert "e(n) :- n = count : b(_)." in text


def test_a_guarded_probe_result_needs_its_request():
    probe = ("req", ("res",), False)
    assert "f(x) :- res(x), req(x)." not in clauses(pruned(set(), [probe]))


def test_a_derived_request_makes_its_result_possible():
    # req("r") :- a("x") fires when a has facts, so res can then have rows.
    text = pruned({"a"}, [("req", ("res",), False)])
    assert "f(x) :- res(x), req(x)." in clauses(text)
    assert ".input res" in text.splitlines()


def test_outputs_stay_declared_and_output():
    text = pruned(set())
    for rel in ("c", "d", "e", "f", "req"):
        assert f".output {rel}" in text.splitlines()
        assert any(line.startswith(f".decl {rel}(") for line in text.splitlines())


def test_what_is_not_modelled_is_not_pruned():
    assert prune.prune_text(".comp C { }\n", set(), []) is None
    assert prune.prune_text(".decl a(x:symbol), b(x:symbol)\n", set(), []) is None


def test_a_parenthesised_disjunction_keeps_its_clause():
    assert "f(x) :- a(x), (b(x) ; c(x))." in clauses(pruned({"a"}))
