# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Variants and per-platform discovery.

- The spec declares every configuration; `--config` (config_select) picks
  which are built, else the spec's config_default, else all of them. A
  configuration that is not built triggers no discovery and no probe.
- Toolchain pins are scoped by (language, exec platform, target platform).
- Discovery is per platform: a name is searched where it is used (a tool on
  the exec platform, a library on the target platform), with that
  platform's search locations, and selected per platform. Natively the two
  coincide: one binding.

The toy fixtures need no probe of the machine: toy-platform's platforms,
exec-demo's toolchains and actions, and the test plugins' `toy` provider.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
DEFINED = ROOT / "tests" / "fixtures" / "defined"
EXEC_DEMO = ROOT / "tests" / "fixtures" / "exec-demo" / "project"
PP = ROOT / "tests" / "fixtures" / "per-platform"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")

EXEC = "[toy, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], nil]]]]"
TOY_A = (
    "[toy-a, [[native.opt, debug], [[native.sanitize, none], [[native.linkage, default], nil]]]]"
)


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def rows(path):
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def metrics_of(build):
    return json.loads((build / "metrics.json").read_text())


# ---- scoped toolchain pins ---------------------------------------------------


def exec_demo(tmp_path, spec_extra="", uses=("toy-platform", "exec-demo", "toy-tcz")):
    project = tmp_path / "project"
    shutil.copytree(EXEC_DEMO, project)
    (project / "buildlog.toml").write_text(
        f'uses = {json.dumps(list(uses))}\nspec = ["spec.toml"]\n'
    )
    if spec_extra:
        (project / "user.toml").write_text(spec_extra)
    return project


def test_cross_build_without_pins_is_ambiguous_per_triple(tmp_path, capsys):
    project = exec_demo(tmp_path)
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", package_paths=[DEFINED])
    err = capsys.readouterr().err
    # tcz competes with tcy for the tools (toy on toy) and with tcx for the
    # product (toy on toy-a): two separate ambiguities.
    assert "toy running on toy for toy: tcy vs tcz" in err
    assert "toy running on toy for toy-a: tcx vs tcz" in err


def test_tools_pick_the_exec_scoped_toolchain(tmp_path):
    pins = 'toolchain_pin = [["toy", "toy", "toy", "tcy"], ["toy", "toy", "toy-a", "tcz"]]\n'
    project = exec_demo(tmp_path, pins)
    build = tmp_path / "build"
    facts, _ = configure_project(
        project, build, package_paths=[DEFINED], user_files=[project / "user.toml"]
    )
    assert metrics_of(build)["errors"] == []
    text = (build / "souffle" / "build.ninja").read_text()
    # The tools (built for toy, run on toy) use the (toy, toy) pin: tcy.
    assert "cmd = /bin/cp ../../project/gen.sh toy.release.none/gen\n" in text
    # The product (built for toy-a on toy) uses the (toy, toy-a) pin: tcz.
    assert "cmd = tcz-cp ../../project/app.txt toy-a.debug.none/app\n" in text
    assert ("toy", "toy", "toy", "tcy") in rows(facts / "tc_select.csv")
    assert ("toy", "toy", "toy-a", "tcz") in rows(facts / "tc_select.csv")


def test_a_pin_for_the_other_scope_does_not_apply(tmp_path, capsys):
    # Pinning only the product's triple leaves the tools' ambiguous: a pin
    # never reaches another (exec, target) pair.
    project = exec_demo(tmp_path, 'toolchain_pin = [["toy", "toy", "toy-a", "tcz"]]\n')
    with pytest.raises(SystemExit):
        configure_project(
            project, tmp_path / "build", package_paths=[DEFINED], user_files=[project / "user.toml"]
        )
    err = capsys.readouterr().err
    assert "toy running on toy for toy: tcy vs tcz" in err
    assert "for toy-a" not in err


def test_an_unscoped_pin_is_rejected_by_the_loader(tmp_path, capsys):
    project = exec_demo(tmp_path, 'toolchain_pin = [["toy", "tcy"]]\n')
    with pytest.raises(
        ValueError,
        match=r"toolchain_pin rows have 4 columns \(lang, exec, target, tc\), this one has 2",
    ):
        configure_project(
            project, tmp_path / "build", package_paths=[DEFINED], user_files=[project / "user.toml"]
        )


# ---- configuration selection -------------------------------------------------


SELECT = """
config_decl_setting = [["native", "native.opt", "release"]]
request_all = [["app"]]
"""


def needed_configs(facts):
    return {c for _, c in rows(facts / "needed.csv")}


def test_every_declared_configuration_is_built_by_default(tmp_path):
    project = exec_demo(tmp_path, SELECT, uses=("toy-platform", "exec-demo"))
    (project / "spec.toml").write_text(
        (project / "spec.toml").read_text().replace('request = [["app", "cross"]]\n', "")
    )
    facts, _ = configure_project(
        project, tmp_path / "build", package_paths=[DEFINED], user_files=[project / "user.toml"]
    )
    assert rows(facts / "config_active.csv") == {("cross",), ("native",)}
    assert needed_configs(facts) == {TOY_A, EXEC}


@pytest.mark.parametrize(
    ("spec", "configs", "active"),
    [
        ('config_default = [["native"]]\n', None, {"native"}),
        ('config_default = [["native"]]\n', ["cross"], {"cross"}),
        ("", ["native"], {"native"}),
    ],
)
def test_config_selects_and_the_spec_defaults(tmp_path, spec, configs, active):
    project = exec_demo(tmp_path, SELECT + spec, uses=("toy-platform", "exec-demo"))
    (project / "spec.toml").write_text(
        (project / "spec.toml").read_text().replace('request = [["app", "cross"]]\n', "")
    )
    build = tmp_path / "build"
    facts, _ = configure_project(
        project, build, package_paths=[DEFINED], configs=configs, user_files=[project / "user.toml"]
    )
    assert {r[0] for r in rows(facts / "config_active.csv")} == active
    # Only the selected configurations (and their exec configuration) are needed.
    if active == {"native"}:
        assert needed_configs(facts) == {EXEC}
        assert "toy-a" not in {r[0] for r in rows(facts / "build_platform.csv")}
    else:
        assert TOY_A in needed_configs(facts)


def test_a_reconfigure_keeps_the_config_selection(tmp_path, capsys):
    """Bug B2: --config is remembered like --user; a change of the active set is reported."""
    project = exec_demo(tmp_path, SELECT, uses=("toy-platform", "exec-demo"))
    (project / "spec.toml").write_text(
        (project / "spec.toml").read_text().replace('request = [["app", "cross"]]\n', "")
    )
    build = tmp_path / "build"
    kw = {"package_paths": [DEFINED], "user_files": [project / "user.toml"]}
    configure_project(project, build, configs=["cross"], **kw)
    capsys.readouterr()
    facts, _ = configure_project(project, build, **kw)
    assert rows(facts / "config_active.csv") == {("cross",)}
    assert "(was:" not in capsys.readouterr().err
    facts, _ = configure_project(project, build, configs=["native"], **kw)
    assert rows(facts / "config_active.csv") == {("native",)}
    assert "configuring native (was: cross)" in capsys.readouterr().err
    state = json.loads((build / "buildlog-state.json").read_text())
    assert state["configs"] == ["native"] and state["active_configs"] == ["native"]


def test_an_unknown_config_is_an_error(tmp_path, capsys):
    project = exec_demo(tmp_path, SELECT, uses=("toy-platform", "exec-demo"))
    with pytest.raises(SystemExit):
        configure_project(
            project,
            tmp_path / "build",
            package_paths=[DEFINED],
            configs=["nope"],
            user_files=[project / "user.toml"],
        )
    assert "unknown config: nope (--config)" in capsys.readouterr().err


def test_cli_config_flag_and_regeneration(tmp_path):
    project = exec_demo(tmp_path, SELECT, uses=("toy-platform", "exec-demo"))
    build = tmp_path / "build"
    argv = ["configure", "-C", str(build), "--package-path", str(DEFINED)]
    assert (
        main([*argv, "--user", str(project / "user.toml"), "--config", "native", str(project)]) == 0
    )
    text = (build / "souffle" / "build.ninja").read_text()
    assert "toy-a.debug.none" not in text
    # The regeneration edge configures the same selection again.
    regen = text.split("build build.ninja: regen")[1].split("\n\n")[0]
    assert "--config native" in regen


# ---- per-platform discovery ---------------------------------------------------


def discovery_project(tmp_path, search, request='request = [["app", "cross"]]', extra=""):
    """app (built for its configuration) depends on toy:lib; blob, which app
    depends on, is made by running toy:gen. Both are found by the toy
    provider, under the spec's search locations."""
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "buildlog.toml").write_text(
        'uses = ["toy-platform", "exec-demo", "test-provider"]\n'
        f"source_root = {json.dumps(str(PP))}\n"
        'spec = ["spec.toml"]\n'
    )
    covers = ", ".join(
        f"[{json.dumps(str(PP / d / f))}, {json.dumps(p)}]"
        for d, f in [("exec", "gen-exec.sh"), ("target", "gen-target.sh")]
        for p in ("toy", "toy-a")
    )
    (project / "spec.toml").write_text(
        f"""
target = [["app"], ["blob"]]
execdemo_tool = [["app", "app.txt"]]
execdemo_gen = [["blob", "toy:gen", "halide_blob", "input.txt"]]
dep = [["app", "blob", "private"], ["app", "toy:lib", "private"]]
config_decl = [["cross", "toy-a"], ["native", "toy"], ["other", "toy-b"]]
config_decl_setting = [["native", "native.opt", "release"]]
{request}
# The toy scripts carry no object headers: their coverage is stated.
artifact_covers = [{covers}]
"""
    )
    (project / "user.toml").write_text(search + "\n" + extra)
    return project


SCOPED = f"""package_search_dir_on = [
  ["toy", "toy", {json.dumps(str(PP / "exec"))}],
  ["toy-a", "toy", {json.dumps(str(PP / "target"))}],
  ["toy-b", "toy", {json.dumps(str(PP / "never"))}],
]"""


def selections(facts):
    """The selections where the name is wanted (a platform that shares a
    search root with a wanting one also selects, inertly)."""
    wanted = rows(facts / "import_want.csv")
    return {
        (n, p, Path(f).relative_to(PP).as_posix())
        for n, p, _, f in rows(facts / "import_select.csv")
        if (n, p) in wanted
    }


def test_a_tool_binds_on_the_exec_platform_and_a_library_on_the_target(tmp_path):
    project = discovery_project(tmp_path, SCOPED)
    build = tmp_path / "build"
    facts, _ = configure_project(
        project, build, package_paths=[DEFINED], user_files=[project / "user.toml"]
    )
    assert metrics_of(build)["errors"] == []
    # toy:gen is run by blob's action: searched and bound on the exec
    # platform (toy), from its search dir; toy:lib is a dep of app (toy-a):
    # bound on toy-a, from its own. Neither is searched where it is not used.
    assert selections(facts) == {
        ("toy:gen", "toy", "exec/gen.toy"),
        ("toy:lib", "toy-a", "target/lib.toy"),
    }
    versions = rows(facts / "package_version.csv")
    assert ("toy:gen", "toy", "1", "0", "0") in versions
    assert ("toy:lib", "toy-a", "2", "0", "0") in versions
    ninja = build / "souffle"
    text = (ninja / "build.ninja").read_text()
    assert f"{PP}/exec/gen-exec.sh halide_blob" in text
    assert "gen-target.sh" not in text
    subprocess.run(["ninja", "-C", str(ninja)], check=True, capture_output=True)
    assert (ninja / "toy-a.debug.none" / "blob").read_text() == "exec gen halide_blob\npayload\n"


def test_why_explains_the_tools_binding_from_the_exec_platform(tmp_path, capsys):
    project = discovery_project(tmp_path, SCOPED)
    build = tmp_path / "build"
    configure_project(project, build, package_paths=[DEFINED], user_files=[project / "user.toml"])
    capsys.readouterr()
    fact = f'import_select("toy:gen", "toy", "toy", "{PP / "exec" / "gen.toy"}")'
    want = 'import_want("toy:gen", "toy")'
    assert (
        main(["why", "-C", str(build), "--package-path", str(DEFINED), fact, want, str(project)])
        == 0
    )
    out = capsys.readouterr().out
    # Demanded on toy because blob, in toy-a, runs it and toy-a's exec
    # configuration is on toy; searched under toy's search dir.
    assert 'import_demand("toy:gen", "toy")' in out
    assert 'tool_ref("blob", ‹toy-a.debug.none›' in out
    assert "exec_config(‹toy-a.debug.none›, ‹toy.release.none›)" in out
    assert f'package_search_dir_on("toy", "toy", "{PP / "exec"}")' in out


def test_natively_the_platforms_coincide_one_binding(tmp_path):
    project = discovery_project(tmp_path, SCOPED, request='request = [["app", "native"]]')
    build = tmp_path / "build"
    facts, _ = configure_project(
        project, build, package_paths=[DEFINED], user_files=[project / "user.toml"]
    )
    assert metrics_of(build)["errors"] == []
    assert selections(facts) == {
        ("toy:gen", "toy", "exec/gen.toy"),
        ("toy:lib", "toy", "exec/lib.toy"),
    }
    metrics = metrics_of(build)
    assert metrics["composition_calls"] == metrics["evaluator_startups"] == 1


def test_an_unscoped_search_dir_serves_every_platform(tmp_path):
    search = f'package_search_dir = [["toy", {json.dumps(str(PP / "target"))}]]'
    project = discovery_project(tmp_path, search)
    build = tmp_path / "build"
    facts, _ = configure_project(
        project, build, package_paths=[DEFINED], user_files=[project / "user.toml"]
    )
    # Both bind from target/: the tool on toy, the library on toy-a (one
    # find per name and root, whatever the number of platforms).
    assert selections(facts) == {
        ("toy:gen", "toy", "target/gen.toy"),
        ("toy:lib", "toy-a", "target/lib.toy"),
    }


def test_an_unselected_configuration_does_not_demand_a_binding(tmp_path):
    project = discovery_project(tmp_path, SCOPED, request='request_all = [["app"]]')
    build = tmp_path / "build"
    facts, _ = configure_project(
        project,
        build,
        package_paths=[DEFINED],
        configs=["cross"],
        user_files=[project / "user.toml"],
    )
    # The declared roots remain available, including unselected branches,
    # but "other" (toy-b) causes no demand or effect.
    assert not any(p == "toy-b" for _, p in rows(facts / "import_want.csv"))
    assert selections(facts) == {
        ("toy:gen", "toy", "exec/gen.toy"),
        ("toy:lib", "toy-a", "target/lib.toy"),
    }


def test_a_pin_is_per_platform(tmp_path, capsys):
    # Both dirs for both platforms: each name is ambiguous on the platform
    # it is used on, until pinned there.
    both = f"""package_search_dir = [["toy", {json.dumps(str(PP / "exec"))}], ["toy", {json.dumps(str(PP / "target"))}]]"""
    project = discovery_project(tmp_path, both)
    with pytest.raises(SystemExit):
        configure_project(
            project, tmp_path / "build", package_paths=[DEFINED], user_files=[project / "user.toml"]
        )
    err = capsys.readouterr().err
    assert "ambiguous package: toy:gen on toy: " in err
    assert "ambiguous package: toy:lib on toy-a: " in err
    pins = f"""package_pin_on = [
  ["toy", "toy:gen", {json.dumps(str(PP / "exec" / "gen.toy"))}],
  ["toy-a", "toy:lib", {json.dumps(str(PP / "target" / "lib.toy"))}],
]"""
    project = discovery_project(tmp_path / "pinned", both, extra=pins)
    facts, _ = configure_project(
        project,
        tmp_path / "pinned" / "build",
        package_paths=[DEFINED],
        user_files=[project / "user.toml"],
    )
    assert selections(facts) == {
        ("toy:gen", "toy", "exec/gen.toy"),
        ("toy:lib", "toy-a", "target/lib.toy"),
    }


def test_a_name_bound_only_for_another_platform_is_an_error(tmp_path, capsys):
    # toy:lib is installed only under exec/ (toy's search dir); the product
    # on toy-a needs it: never the exec platform's binding.
    search = f"""package_search_dir_on = [["toy", "toy", {json.dumps(str(PP / "exec"))}]]"""
    project = discovery_project(
        tmp_path, search, request='request = [["app", "cross"], ["app", "native"]]'
    )
    with pytest.raises(SystemExit):
        configure_project(
            project, tmp_path / "build", package_paths=[DEFINED], user_files=[project / "user.toml"]
        )
    err = capsys.readouterr().err
    assert "dependency toy:lib of app has no definition" in err or (
        "imported target not bound for this platform" in err
    )
