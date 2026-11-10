# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Finding packages, bounded provider rules, and buildlog.lock."""

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from buildlog import cli
from buildlog.engine import LoaderError, configure_project, project_packages
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SEARCH_A = FIXTURES / "search" / "a"
SEARCH_B = FIXTURES / "search" / "b"
FOUND = FIXTURES / "discovered" / "found"
DEFINED = FIXTURES / "defined"

needs_souffle = pytest.mark.skipif(
    not souffle_path().exists(), reason="patched Souffle not installed"
)


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def make_project(root, uses, spec=""):
    project = root / "project"
    project.mkdir(parents=True)
    listing = 'spec = ["spec.toml"]\n' if spec else ""
    decides = (
        '[author_decides]\npackage_search_dir = "test fixture uses a local provider root"\n'
        if "package_search_dir" in spec
        else ""
    )
    (project / "buildlog.toml").write_text(f"uses = {json.dumps(uses)}\n{listing}{decides}")
    if spec:
        (project / "spec.toml").write_text(spec)
    return project


def lock(project):
    data = tomllib.loads((project / "buildlog.lock").read_text())
    return {entry["name"]: entry for entry in data["package"]}


def actions(facts):
    """Build actions, without core's regeneration (generator) action."""
    generators = set((facts / "action_generator.csv").read_text().split())
    rows = (line.split("\t")[0] for line in (facts / "action.csv").read_text().splitlines())
    return sorted(a for a in rows if a not in generators)


def test_plugins_load_only_when_used(tmp_path):
    project = make_project(tmp_path, [])
    assert [p.name for p in project_packages(project)] == ["buildlog"]
    project = make_project(tmp_path / "x", ["buildlog-cpp"])
    packages = project_packages(project)
    assert [(p.name, p.candidate.source) for p in packages] == [
        ("buildlog", "core"),
        ("buildlog-cpp", "plugin"),
    ]
    assert packages[1].candidate.entry_point == "buildlog_cpp:manifest_dir"


def test_ambiguous_uses_lists_candidates(tmp_path):
    project = make_project(tmp_path, ["dup"])
    with pytest.raises(LoaderError) as exc:
        project_packages(project, [SEARCH_A, SEARCH_B])
    (error,) = exc.value.errors
    assert error.startswith("package dup (uses) is ambiguous: ")
    assert str(SEARCH_A / "share" / "buildlog" / "dup") in error
    assert str(SEARCH_B / "share" / "buildlog" / "dup") in error
    # One prefix: unique.
    assert [p.name for p in project_packages(project, [SEARCH_A])] == ["buildlog", "dup"]


def test_unknown_uses_is_an_error(tmp_path):
    project = make_project(tmp_path, ["no-such-package"])
    with pytest.raises(LoaderError, match="package no-such-package \\(uses\\) not found"):
        project_packages(project, [SEARCH_A])


def test_search_path_from_environment(tmp_path, monkeypatch):
    project = make_project(tmp_path, ["dup"])
    monkeypatch.setenv("BUILDLOG_PACKAGE_PATH", f"{SEARCH_B}:")
    (dup,) = [p for p in project_packages(project) if p.name == "dup"]
    assert dup.candidate.manifest == SEARCH_B / "share" / "buildlog" / "dup" / "manifest.toml"


@needs_souffle
def test_namespace_autoload_unique(tmp_path):
    project = make_project(tmp_path, [], 'twin_item = [["a"]]\n')
    facts, _ = configure_project(project, tmp_path / "build", emit=False, package_paths=[SEARCH_A])
    assert actions(facts) == ["out:twin-one/a"]
    entry = lock(project)["twin-one"]
    assert entry["reason"] == "namespace:twin_item"
    assert entry["source"] == "path"


def test_namespace_autoload_ambiguous(tmp_path):
    project = make_project(tmp_path, [], 'twin_item = [["a"]]\n')
    with pytest.raises(LoaderError) as exc:
        project_packages(project, [SEARCH_A, SEARCH_B])
    (error,) = exc.value.errors
    assert "relation twin_item matches several namespaces" in error
    assert "twin-one" in error and "twin-two" in error


def test_namespace_autoload_none(tmp_path):
    project = make_project(tmp_path, [], 'mystery_item = [["a"]]\n')
    with pytest.raises(LoaderError, match="relation mystery_item is accepted by no loaded package"):
        project_packages(project, [SEARCH_A])


@needs_souffle
def test_namespace_ownership_is_unique(tmp_path):
    project = make_project(tmp_path, ["twin-one", "twin-two"])
    with pytest.raises(LoaderError) as exc:
        configure_project(
            project, tmp_path / "build", emit=False, package_paths=[SEARCH_A, SEARCH_B]
        )
    assert "twin-two: namespace twin is already owned by twin-one" in exc.value.errors


@pytest.mark.parametrize("separator", [":", ": ", ":  "])
def test_lock_reason_is_portable_with_provider_whitespace(tmp_path, separator):
    from buildlog.packages import _project_relative

    project = tmp_path / "project"
    manifest = tmp_path / "prefix" / "share" / "buildlog" / "pkg" / "manifest.toml"
    reason = f"buildlog{separator}{manifest}"
    expected = f"buildlog{separator}../prefix/share/buildlog/pkg/manifest.toml"
    assert _project_relative(reason, project) == expected


@needs_souffle
def test_lockfile_created_mismatch_and_update(tmp_path, capsys):
    project = make_project(tmp_path, ["twin-one"], 'twin_item = [["a"]]\n')
    shutil.copytree(SEARCH_A / "share" / "buildlog" / "twin-one", project / "packages" / "twin-one")
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    entries = lock(project)
    assert list(entries) == ["buildlog", "twin-one"]
    assert entries["buildlog"]["source"] == "core"
    assert entries["twin-one"] == {
        "name": "twin-one",
        "version": "0.1.0",
        "source": "path",
        "path": "packages/twin-one",
        "hash": entries["twin-one"]["hash"],
        "reason": "uses",
    }
    assert entries["twin-one"]["hash"].startswith("sha256:")
    text = (project / "buildlog.lock").read_text()

    # Unchanged packages: the lock is stable.
    configure_project(project, build, emit=False)
    assert (project / "buildlog.lock").read_text() == text

    layer = project / "packages" / "twin-one" / "twin.dl"
    layer.write_text(layer.read_text().replace('"TWIN"', '"TWIN2"'))
    with pytest.raises(LoaderError) as exc:
        configure_project(project, build, emit=False)
    (error,) = exc.value.errors
    assert error.startswith("buildlog.lock: twin-one hash changed")
    assert "--update-lock" in error
    assert (project / "buildlog.lock").read_text() == text

    assert cli.main(["configure", "-C", str(build), str(project)]) == 1
    assert "twin-one hash changed" in capsys.readouterr().err
    assert cli.main(["configure", "--update-lock", "-C", str(build), str(project)]) == 0
    assert lock(project)["twin-one"]["hash"] != entries["twin-one"]["hash"]
    configure_project(project, build, emit=False)


@needs_souffle
def test_probe_cannot_introduce_uncertified_rule_code(tmp_path):
    pointer = tmp_path / "rules.txt"
    pointer.write_text(f"{FOUND}\n")
    project = make_project(
        tmp_path, ["test-discover"], f"test_discover_file = [[{json.dumps(str(pointer))}]]\n"
    )
    build = tmp_path / "build"
    with pytest.raises(LoaderError, match="rule package was not certified by preparation"):
        configure_project(project, build, emit=False)


def toy_project(tmp_path, dep):
    toys = tmp_path / "toys"
    toys.mkdir()
    (toys / "greet.toy").write_text("interface toy:greet\n")
    # discovery is per platform and demanded through the needed
    # closure: a configuration (on the toy platform) and a request.
    spec = (
        f'package_search_dir = [["toy", {json.dumps(str(toys))}]]\n'
        f'target = [["app"]]\ndep = [["app", {json.dumps(dep)}, "public"]]\n'
        'config_decl = [["d", "toy"]]\nrequest = [["app", "d"]]\n'
    )
    return make_project(tmp_path, ["test-provider", "toy-platform"], spec), toys


@needs_souffle
def test_provider_rules_are_prepared_once_and_used_when_needed(tmp_path):
    project, _ = toy_project(tmp_path, "toy:greet")
    build = tmp_path / "build"
    facts, metrics = configure_project(project, build, emit=False, package_paths=[DEFINED])
    assert [o["packages"] for o in metrics["outer"]] == [
        ["buildlog", "toy-platform", "test-provider", "toy-rules"],
    ]
    assert metrics["composition_calls"] == metrics["evaluator_startups"] == 1
    assert actions(facts) == ["out:toy/toy:greet"]
    entry = lock(project)["toy-rules"]
    assert entry["reason"] == "provider toy"
    _, warm = configure_project(project, build, emit=False, package_paths=[DEFINED])
    assert warm["executed"] == 0 and len(warm["outer"]) == 1


@needs_souffle
def test_available_provider_rules_stay_unused_when_not_needed(tmp_path):
    project, _ = toy_project(tmp_path, "toy:absent")
    facts, metrics = configure_project(
        project, tmp_path / "build", keep_going=True, package_paths=[DEFINED]
    )
    assert [o["packages"] for o in metrics["outer"]] == [
        ["buildlog", "toy-platform", "test-provider", "toy-rules"]
    ]
    assert "toy-rules" not in lock(project)
    assert actions(facts) == []


@needs_souffle
def test_runaway_plugin_probe_detected(tmp_path):
    project = make_project(tmp_path, ["test-runaway"], 'config_decl = [["d", "p"]]\n')
    with pytest.raises(SystemExit) as exc:
        configure_project(project, tmp_path / "build", emit=False)
    assert "request outside finite domain" in str(exc.value)


@needs_souffle
def test_a_pinned_package_the_project_no_longer_asks_for_leaves_the_lock(tmp_path):
    """The lock pins `found`; the spec stops asking for it. The lock never loads
    it, so it is not found and the next configure drops its entry."""
    from .test_single_composition import project_for

    project, _ = project_for(tmp_path)
    configure_project(project, tmp_path / "build", emit=False)
    assert "found" in lock(project)
    spec = project / "spec.toml"
    spec.write_text(spec.read_text().replace('dep = [["root", "pc:rules", "public"]]\n', ""))
    _, metrics = configure_project(project, tmp_path / "build2", emit=False)
    assert "found" in metrics["outer"][0]["packages"]
    assert "found" not in lock(project)


@needs_souffle
def test_withdrawing_plugin_cannot_change_the_executable_union(tmp_path):
    pointer = tmp_path / "rules.txt"
    pointer.write_text(f"{FIXTURES / 'withdrawn' / 'withdrawing'}\n")
    project = make_project(
        tmp_path, ["test-discover"], f"test_discover_file = [[{json.dumps(str(pointer))}]]\n"
    )
    with pytest.raises(LoaderError) as exc:
        configure_project(project, tmp_path / "build", emit=False)
    (error,) = exc.value.errors
    assert "withdrawing" in error
    assert "rule package was not certified by preparation" in error


def test_a_discovered_rule_package_cannot_re_request_a_disabled_target(tmp_path):
    from .test_single_composition import project_for

    project, _ = project_for(tmp_path, FIXTURES / "discovered" / "found_req")
    spec = project / "spec.toml"
    spec.write_text(
        spec.read_text() + 'request_when = [["tool", "FANCY"]]\noption_decl = [["FANCY", "off"]]\n'
    )
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    metrics = (tmp_path / "build" / "metrics.json").read_text()
    assert "target is requested although its options disable it" in metrics
