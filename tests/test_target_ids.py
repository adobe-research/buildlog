# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Package-qualified target ids and no-choice reference resolution."""

import json
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.errors import LoaderError
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
IDS = ROOT / "tests" / "fixtures" / "target-ids"
TOY = ROOT / "tests" / "fixtures" / "defined"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def rows(path):
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def _core(tmp_path, uses, spec):
    """A core-only project on the toy platform; returns (facts dir or None, errors)."""
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "buildlog.toml").write_text(
        f'uses = {json.dumps(["toy-platform", *uses])}\nspec = ["spec.toml"]\n'
        + (
            '[author_decides]\nalias = "test fixture exercises alias diagnostics"\n'
            if "alias =" in spec
            else ""
        )
    )
    (project / "spec.toml").write_text(
        'config_decl = [["toy", "toy"]]\nrequest = [["app", "toy"]]\n' + spec
    )
    build = tmp_path / "build"
    try:
        facts, _ = configure_project(project, build, emit=False, package_paths=[TOY, IDS])
    except SystemExit:
        facts = None
    errors = json.loads((build / "metrics.json").read_text())["errors"]
    return facts, sorted(tuple(e) for e in errors)


@pytest.mark.integration
def test_two_packages_define_util(tmp_path):
    """alpha::util, beta::util and the project's util build side by side; each
    package's bare "util" means its own, the project's means the project's."""
    project = IDS / "project"
    build = tmp_path / "build"
    facts, metrics = configure_project(
        project,
        build,
        package_paths=[IDS],
        user_files=[project / "presets" / "apple-clang.toml"],
    )
    assert metrics["errors"] == []
    assert {r for r in rows(facts / "dep_resolves.csv") if r[1] == "util"} == {
        ("alpha::api", "util", "alpha::util"),
        ("beta::api", "util", "beta::util"),
        ("app", "util", "util"),
    }
    ninja = build / "souffle"
    subprocess.run(["ninja", "-C", str(ninja)], check=True, capture_output=True)
    libs = sorted(p.name for p in ninja.glob("lib/*/*.a"))
    assert libs == [
        "libalpha::api.a",
        "libalpha::util.a",
        "libbeta::api.a",
        "libbeta::util.a",
        "libutil.a",
    ]
    (exe,) = ninja.glob("bin/*/app")
    # alpha_api = 1 * 10, beta_api = 2 * 100, project_util = 3. A wrong
    # resolution would leave an alpha_util/beta_util symbol undefined.
    assert subprocess.check_output([str(exe)], text=True) == "sum=213\n"


def test_bare_name_is_not_a_suffix_match(tmp_path):
    """A bare name the project does not own never resolves to some package's
    q::name, even when only one package defines it: no implicit exports."""
    facts, errors = _core(
        tmp_path,
        ["alpha2"],
        'target = [["app"]]\ndep = [["app", "util", "public"]]\n',
    )
    assert facts is None
    assert errors == [("unresolved dependency", "dependency util of app has no definition")]
    # With a second definer the answer is the same: adding a package cannot
    # turn a failing or working reference into a different one silently.
    _, errors = _core(
        tmp_path / "two",
        ["alpha2", "ids-amb-a"],
        'target = [["app"]]\ndep = [["app", "util", "public"]]\n',
    )
    assert errors == [("unresolved dependency", "dependency util of app has no definition")]
    facts, errors = _core(
        tmp_path / "ok",
        ["alpha2", "ids-amb-a"],
        'target = [["app"]]\n'
        'dep = [["app", "alpha2::util", "public"], ["app", "amba::util", "public"]]\n',
    )
    assert errors == []
    assert rows(facts / "dep_resolves.csv") == {
        ("app", "alpha2::util", "alpha2::util"),
        ("app", "amba::util", "amba::util"),
    }


def test_aliases_resolve_to_one_target(tmp_path):
    """alias(from, to) is followed, through chains, to exactly one target."""
    facts, errors = _core(
        tmp_path,
        ["ids-amb-a", "ids-amb-b", "ids-alias"],
        'target = [["app"]]\n'
        'dep = [["app", "Only::Only", "public"], ["app", "Util::A", "public"]]\n',
    )
    assert errors == []
    assert rows(facts / "dep_resolves.csv") == {
        ("app", "Only::Only", "amba::only"),
        ("app", "Util::A", "amba::util"),
    }


def test_name_aliased_by_two_packages_is_ambiguous(tmp_path):
    _, errors = _core(
        tmp_path,
        ["ids-amb-a", "ids-amb-b", "ids-alias", "ids-alias-b"],
        'target = [["app"]]\ndep = [["app", "Only::Only", "public"]]\n',
    )
    assert set(errors) == {
        ("ambiguous alias", "Only::Only is amba::only or ids-amb-b::util"),
        (
            "ambiguous dependency",
            "dependency Only::Only of app is amba::only or ids-amb-b::util (qualify it)",
        ),
    }


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        (
            [["x", "y"], ["y", "x"]],
            {("alias cycle", "x"), ("alias cycle", "y")},
        ),
        # No suffix match: util is not a target id, so the alias dangles.
        ([["U", "util"]], {("alias has no definition", "U -> util")}),
        ([["D", "nothing"]], {("alias has no definition", "D -> nothing")}),
        (
            [["V", "amba::only"], ["V", "ids-amb-b::util"]],
            {("ambiguous alias", "V is amba::only or ids-amb-b::util")},
        ),
        (
            [["amba::only", "ids-amb-b::util"]],
            {("ambiguous alias", "amba::only is amba::only or ids-amb-b::util")},
        ),
    ],
)
def test_alias_errors(tmp_path, alias, expected):
    _, errors = _core(
        tmp_path,
        ["ids-amb-a", "ids-amb-b"],
        f'target = [["app"]]\nalias = {json.dumps(alias)}\n',
    )
    assert expected <= set(errors)


@pytest.mark.parametrize(
    ("uses", "targets", "expected"),
    [
        (
            ["ids-bare"],
            [],
            {
                (
                    "package target outside its target namespace",
                    "ids-bare defines helper; name it ids-bare::helper",
                )
            },
        ),
        (
            ["ids-amb-a"],
            ["amba::fake"],
            {
                (
                    "spec target in a package target namespace",
                    "amba::fake (amba:: belongs to ids-amb-a)",
                )
            },
        ),
        (
            ["ids-amb-b"],
            ["ids-amb-b::util"],
            {
                (
                    "spec target in a package target namespace",
                    "ids-amb-b::util (ids-amb-b:: belongs to ids-amb-b)",
                ),
                ("target defined by two sources", "ids-amb-b::util: (spec) and ids-amb-b"),
            },
        ),
    ],
)
def test_target_id_ownership_errors(tmp_path, uses, targets, expected):
    spec = f"target = {json.dumps([[t] for t in ['app', *targets]])}\n"
    _, errors = _core(tmp_path, uses, spec)
    assert expected <= set(errors)


@pytest.mark.parametrize(
    ("uses", "message"),
    [
        (["ids-amb-a", "ids-tq-clash"], r"target namespace [Aa]mba collides with ids-"),
        (["ids-tq-bad"], r"target_namespace must match"),
    ],
)
def test_target_namespace_checks(tmp_path, uses, message):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(
        f'uses = {json.dumps(["toy-platform", *uses])}\nspec = ["spec.toml"]\n'
    )
    (project / "spec.toml").write_text("")
    with pytest.raises(LoaderError, match=message):
        configure_project(project, tmp_path / "build", emit=False, package_paths=[TOY, IDS])


def test_spec_may_not_supply_driver_relations(tmp_path):
    """Provenance facts are driver-owned; a spec forging fact_key is rejected."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('uses = ["toy-platform"]\nspec = ["spec.toml"]\n')
    (project / "spec.toml").write_text('fact_key = [["amba", "target", "x"]]\n')
    with pytest.raises(LoaderError, match="relation fact_key is accepted by no loaded package"):
        configure_project(project, tmp_path / "build", emit=False, package_paths=[TOY])
