# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core L0/L1 semantics on small fixtures that need no plugin or probe."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
DEFINED = ROOT / "tests" / "fixtures" / "defined"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


# Core L3 declares the native settings, so every configuration has them: the
# requested (default debug) one and its exec configuration (release).
TOY_CONFIGS = {
    f"[toy, [[native.opt, {opt}], [[native.sanitize, none], [[native.linkage, default], nil]]]]"
    for opt in ("debug", "release")
}


def rows(path):
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def _defined_project(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(DEFINED / "project", project)
    return project


def own_conditions(path):
    return {(c, n) for c, n in rows(path) if not n.startswith("on:")}


def test_defined_holds_and_fails(tmp_path):
    project = _defined_project(tmp_path)
    facts, metrics = configure_project(
        project, tmp_path / "build", emit=False, package_paths=[DEFINED]
    )
    assert metrics["errors"] == []
    # (on:<platform> is discovery's per-platform condition, which holds too.)
    assert own_conditions(facts / "cond_holds.csv") == {
        (cfg, name) for cfg in TOY_CONFIGS for name in ("has_present", "lacks_absent")
    }
    # The edge guarded by $Defined(absent) is dropped without an error; the
    # $Not($Defined(absent)) fallback is taken.
    assert {t for t, _ in rows(facts / "needed.csv")} == {"app", "present", "fallback"}


def test_defined_record_encoding_agrees(tmp_path):
    """The NO_ADT tagged-record encoding of Cond derives the same conditions."""
    project = _defined_project(tmp_path)
    build = tmp_path / "build"
    _, metrics = configure_project(project, build, emit=False, package_paths=[DEFINED])
    facts_in = tmp_path / "in"
    shutil.copytree(build / "facts-in" / f"round-{metrics['final_round']}", facts_in)
    defined = '[defined, {}, "", nil, nil]'
    (facts_in / "condition.facts").write_text(
        f"has_present\t{defined.format('present')}\n"
        f"has_absent\t{defined.format('absent')}\n"
        f'lacks_absent\t[not, "", "", {defined.format("absent")}, nil]\n'
    )
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        [
            str(souffle_path()),
            "-M",
            "NO_ADT=1",
            "-w",
            "-F",
            str(facts_in),
            "-D",
            str(out),
            str(build / "program" / "build.dl"),
        ],
        check=True,
        capture_output=True,
    )
    assert own_conditions(out / "cond_holds.csv") == {
        (cfg, name) for cfg in TOY_CONFIGS for name in ("has_present", "lacks_absent")
    }


JOIN = ROOT / "tests" / "fixtures" / "join"


def _join(tmp_path, spec):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('uses = ["join-demo"]\nspec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(spec)
    build = tmp_path / "build"
    try:
        facts, metrics = configure_project(project, build, emit=False, package_paths=[JOIN])
    except SystemExit:
        metrics = json.loads((build / "metrics.json").read_text())
        return None, sorted(tuple(e) for e in metrics["errors"])
    return rows(facts / "joindemo_text.csv"), []


def test_ordered_join_folds_by_rank(tmp_path):
    text, errors = _join(
        tmp_path,
        """
joindemo_item = [["g", "b"], ["g", "a"], ["g", "c"], ["one", "x"]]
joindemo_rank = [["g", "b", 20], ["g", "a", 30], ["g", "c", 10], ["g", "unused", 5], ["one", "x", 1]]
joindemo_sep = [["g", ","]]
""",
    )
    assert errors == []
    # Rank order, not lexical order; a one-item group needs no separator.
    assert text == {("g", "c,b,a"), ("one", "x")}


def _join_spec(items, ranks, seps):
    return "".join(
        f"{rel} = {json.dumps(val)}\n"
        for rel, val in (("joindemo_item", items), ("joindemo_rank", ranks), ("joindemo_sep", seps))
        if val
    )


AB = [["g", "a"], ["g", "b"]]


@pytest.mark.parametrize(
    ("items", "ranks", "seps", "expected"),
    [
        (
            AB,
            [["g", "a", 1], ["g", "b", 1]],
            [["g", ","]],
            ("ordered join is not total", "joindemo:g: a / b"),
        ),
        (AB, [["g", "a", 1]], [["g", ","]], ("ordered join item has no rank", "joindemo:g: b")),
        (
            AB,
            [["g", "a", 1], ["g", "b", 2], ["g", "b", 3]],
            [["g", ","]],
            ("ordered join item has two ranks", "joindemo:g: b"),
        ),
        (AB, [["g", "a", 1], ["g", "b", 2]], [], ("ordered join has no separator", "joindemo:g")),
        (
            [["g", "a"]],
            [["g", "a", 1]],
            [["g", ","], ["g", "-"]],
            ("ordered join has two separators", "joindemo:g"),
        ),
    ],
)
def test_ordered_join_rejects_non_total_orders(tmp_path, items, ranks, seps, expected):
    _, errors = _join(tmp_path, _join_spec(items, ranks, seps))
    assert expected in errors


# The exponential behaviour these guard against took minutes; a configure is
# ~0.7 s alone but several seconds under a loaded parallel run.
JOIN_TIME_LIMIT = 20.0


def _timed_join(tmp_path, spec):
    start = time.perf_counter()
    text, errors = _join(tmp_path, spec)
    return text, errors, time.perf_counter() - start


def test_ill_formed_join_errors_without_exponential_work(tmp_path):
    # 25 ranks with two items each used to build 2^25 suffixes before the error.
    n = 25
    items = [["g", f"{c}{k}"] for k in range(n) for c in "ab"]
    ranks = [["g", f"{c}{k}", k] for k in range(n) for c in "ab"]
    _, errors, secs = _timed_join(tmp_path, _join_spec(items, ranks, [["g", ","]]))
    assert ("ordered join is not total", "joindemo:g: a0 / b0") in errors
    assert secs < JOIN_TIME_LIMIT  # join_suffix is private, so time is the check


def test_join_with_ambiguous_separators_is_not_folded(tmp_path):
    n = 25
    items = [["g", f"i{k}"] for k in range(n)]
    ranks = [["g", f"i{k}", k] for k in range(n)]
    _, errors, secs = _timed_join(tmp_path, _join_spec(items, ranks, [["g", ","], ["g", "-"]]))
    assert ("ordered join has two separators", "joindemo:g") in errors
    assert secs < JOIN_TIME_LIMIT


def test_well_formed_join_of_many_ranks_is_unchanged(tmp_path):
    n = 25
    items = [["g", f"i{k}"] for k in range(n)]
    ranks = [["g", f"i{k}", n - k] for k in range(n)]
    text, errors, secs = _timed_join(tmp_path, _join_spec(items, ranks, [["g", ","]]))
    assert errors == []
    assert text == {("g", ",".join(f"i{k}" for k in reversed(range(n))))}
    assert secs < JOIN_TIME_LIMIT


def _coverage(tmp_path, covers):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(
        'uses = ["toy-platform", "coverage-demo"]\nspec = ["spec.toml"]\n'
    )
    (project / "spec.toml").write_text(
        'target = [["app"]]\n'
        'config_decl = [["fat", "toy-fat"]]\n'
        'request = [["app", "fat"]]\n'
        'coveragedemo_consumes = [["app", "lib.so"]]\n'
        f"artifact_covers = {json.dumps(covers)}\n"
    )
    build = tmp_path / "build"
    try:
        configure_project(project, build, emit=False, package_paths=[DEFINED])
    except SystemExit:
        pass
    return sorted(tuple(e) for e in json.loads((build / "metrics.json").read_text())["errors"])


def test_artifact_coverage_of_every_slice(tmp_path):
    assert _coverage(tmp_path, [["lib.so", "toy-a"], ["lib.so", "toy-b"]]) == []


def test_artifact_coverage_missing_slice_is_an_error(tmp_path):
    # The toy-b slice and the multi-platform both need toy-b.
    assert _coverage(tmp_path, [["lib.so", "toy-a"]]) == [
        ("artifact lacks platform coverage", "app in toy-b.debug.none needs lib.so on toy-b"),
        ("artifact lacks platform coverage", "app in toy-fat.debug.none needs lib.so on toy-b"),
    ]


L0 = ROOT / "tests" / "fixtures" / "l0"


def _l0_errors(tmp_path, package):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(f'uses = ["{package}"]\nspec = ["spec.toml"]\n')
    (project / "spec.toml").write_text("")
    build = tmp_path / "build"
    try:
        configure_project(project, build, emit=False, package_paths=[L0])
    except SystemExit:
        pass
    metrics = json.loads((build / "metrics.json").read_text())
    return sorted(tuple(e) for e in metrics["errors"])


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("l0-ok", []),
        (
            "l0-multi-producer",
            [("output produced by more than one action", "out:shared.h <- out:x | out:y")],
        ),
        (
            "l0-unproduced-input",
            [
                ("action input is neither a source nor produced", "out:a.o <- a.c"),
                ("action input is neither a source nor produced", "out:a.o <- out:missing.h"),
            ],
        ),
        (
            "l0-cycle",
            [("action dependency cycle", "out:a"), ("action dependency cycle", "out:b")],
        ),
        (
            "l0-cycle-hub",
            [("action dependency cycle", "out:hub"), ("action dependency cycle", "out:x7")],
        ),
        (
            "l0-argv",
            [
                ("action argv has no program (gap at position 0)", "out:a"),
                (
                    "action argv position has several tokens but is not a declared set",
                    "out:b 1",
                ),
                ("action argv position used by an argument and a sequence", "out:b 4"),
            ],
        ),
        ("l0-default-output", [("default output not produced by any action", "out:nothing")]),
        (
            "l0-streams",
            [
                ("action has two stdin files", "out:a: src:a.in / src:b.in"),
                ("action has two stdout files", "out:b: out:b.log / out:elsewhere"),
                ("action stdout is not one of its outputs", "out:b > out:elsewhere"),
            ],
        ),
        (
            "l0-cwd",
            [
                ("action has two builtins", "out:c: copy / frobnicate"),
                ("action has two working directories", "out:a: out:x / out:y"),
                ("action with a working directory has a depfile", "out:b"),
                ("action working directory is not a rooted path", "out:b: /tmp"),
                ("builtin action also has an argv[0]", "out:c"),
                ("restat of an undeclared action", "out:nothing"),
                ("unknown buildlog builtin", "out:c: frobnicate"),
            ],
        ),
    ],
)
def test_l0_integrity(tmp_path, package, expected):
    assert _l0_errors(tmp_path, package) == expected


def test_argv_conflicts_count_distinct_prefix_value_pairs(tmp_path):
    prefix = tmp_path / "packages" / "share" / "buildlog" / "argv-count"
    prefix.mkdir(parents=True)
    (prefix / "manifest.toml").write_text(
        'name = "argv-count"\nversion = "0.1.0"\nrank = 1\nlayers = ["args.dl"]\n'
        'requires = { buildlog = ">=0.1" }\nextends = ["action", "action_arg", "action_arg_set"]\n'
    )
    (prefix / "args.dl").write_text(
        'action(a, "STEP"), action_arg(a, 0, "", "/bin/true") :- '
        'a = "out:prefix"; a = "out:value"; a = "out:set"; a = "out:duplicate".\n'
        'action_arg("out:prefix", 1, "-a", "x").\n'
        'action_arg("out:prefix", 1, "-b", "x").\n'
        'action_arg("out:value", 1, "", "a").\n'
        'action_arg("out:value", 1, "", "b").\n'
        'action_arg("out:set", 1, "", "a").\n'
        'action_arg("out:set", 1, "", "b").\n'
        'action_arg_set("out:set", 1).\n'
        'action_arg("out:duplicate", 1, "", "same").\n'
        'action_arg("out:duplicate", 1, "", "same").\n'
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('uses = ["argv-count"]\nspec = ["spec.toml"]\n')
    (project / "spec.toml").write_text("")
    build = tmp_path / "build"
    with pytest.raises(SystemExit):
        configure_project(project, build, emit=False, package_paths=[tmp_path / "packages"])
    assert json.loads((build / "metrics.json").read_text())["errors"] == [
        ["action argv position has several tokens but is not a declared set", "out:prefix 1"],
        ["action argv position has several tokens but is not a declared set", "out:value 1"],
    ]


def _partial(tmp_path, extra="", keep_going=False):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "buildlog.toml").write_text(
        'uses = ["toy-platform", "partial-demo"]\nspec = ["spec.toml"]\n'
    )
    (project / "spec.toml").write_text(
        'target = [["app"], ["lib"], ["other"]]\n'
        'dep = [["app", "lib", "private"]]\n'
        'config_decl = [["a", "toy-a"], ["b", "toy-b"]]\n'
        'request = [["app", "a"], ["app", "b"], ["other", "a"]]\n' + extra
    )
    build = tmp_path / "build"
    try:
        configure_project(project, build, package_paths=[DEFINED], keep_going=keep_going)
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, build / "souffle", json.loads((build / "metrics.json").read_text())


def test_partial_emission_fails_only_affected_edges(tmp_path):
    broken = 'partialdemo_broken = [["lib", "lib is broken"]]\n'
    code, ninja_dir, metrics = _partial(tmp_path, broken)
    assert code == 1
    assert metrics["errors"] == [["partial demo broken", "lib is broken"]]
    assert metrics["global_errors"] == []
    assert not (ninja_dir / "build.ninja").exists()
    # --keep-going: emit the partial graph and exit 0.
    code, ninja_dir, metrics = _partial(tmp_path, broken, keep_going=True)
    assert code == 0
    # lib in toy-a, and app in toy-a which consumes it, fail; the rest builds.
    assert metrics["failed_actions"] == 2
    ok = subprocess.run(
        ["ninja", "toy-b.debug.none/app", "toy-a.debug.none/other"],
        cwd=ninja_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout
    assert (ninja_dir / "toy-b.debug.none" / "app").exists() and (
        ninja_dir / "toy-a.debug.none" / "other"
    ).exists()
    bad = subprocess.run(
        ["ninja", "toy-a.debug.none/app"],
        cwd=ninja_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode != 0
    assert "FAILED STEP toy-a.debug.none/app" in bad.stdout
    assert "error: partial demo broken: lib is broken" in bad.stdout + bad.stderr
    assert not (ninja_dir / "toy-a.debug.none" / "app").exists()


def test_global_error_emits_nothing(tmp_path):
    code, ninja_dir, _ = _partial(tmp_path)
    assert code == 0 and (ninja_dir / "build.ninja").exists()
    code, ninja_dir, metrics = _partial(
        tmp_path, 'partialdemo_global = [["the whole spec is wrong"]]\n', keep_going=True
    )
    assert code == 1
    assert metrics["global_errors"] == [["partial demo global", "the whole spec is wrong"]]
    assert not (ninja_dir / "build.ninja").exists()


def test_regeneration_edge_reruns_configure(tmp_path):
    code, ninja_dir, _ = _partial(tmp_path)
    assert code == 0
    ninja = (ninja_dir / "build.ninja").read_text()
    spec = tmp_path / "project" / "spec.toml"
    regen = ninja.split("build build.ninja: regen ")[1].split("\n\n")[0]
    for path in (
        spec,
        tmp_path / "project" / "buildlog.toml",
        tmp_path / "project" / "buildlog.lock",
    ):
        assert f" {path}" in regen
    assert "partial_demo.dl" in regen and "buildlog-0.1.0.dist-info/METADATA" in regen
    assert "rule regen\n  command = $cmd\n  description = $label $out\n  generator = 1\n" in ninja

    def ninja_run(*args):
        return subprocess.run(
            ["ninja", *args], cwd=ninja_dir, capture_output=True, text=True, check=True
        ).stdout

    assert "CONFIGURE" not in ninja_run("-n")
    # Age build.ninja rather than date the spec into the future, which would
    # keep build.ninja older than its input after regenerating.
    manifest = ninja_dir / "build.ninja"
    past = manifest.stat().st_mtime - 60
    os.utime(manifest, (past, past))
    os.utime(spec)
    assert "CONFIGURE build.ninja" in ninja_run("-n")
    spec.write_text(spec.read_text().replace('["other", "a"]]', '["other", "a"], ["other", "b"]]'))
    os.utime(manifest, (past, past))
    out = ninja_run()
    assert "CONFIGURE build.ninja" in out and "STEP toy-b.debug.none/other" in out
    assert "build toy-b.debug.none/other: run" in (ninja_dir / "build.ninja").read_text()
    assert "CONFIGURE" not in ninja_run("-n")
