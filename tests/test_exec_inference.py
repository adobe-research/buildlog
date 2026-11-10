# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exec configuration inferred from use.

A command names the target it runs (or loads) by reference (action_run);
core derives tool_dep and builds that target for the exec configuration,
with the toolchain that runs on the exec platform and targets it. Nothing
in the fixture says "host" or "exec", and nothing pins a toolchain.
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
PROJECT = ROOT / "tests" / "fixtures" / "exec-demo" / "project"
CROSS = ROOT / "examples" / "cross"

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


def _configure(tmp_path, extra="", request=None):
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    spec = (project / "spec.toml").read_text()
    if request is not None:
        spec = spec.replace('request = [["app", "cross"]]', request)
    (project / "spec.toml").write_text(spec + extra)
    build = tmp_path / "build"
    try:
        facts, _ = configure_project(project, build, package_paths=[DEFINED])
    except SystemExit:
        facts = None
    return project, build, facts, json.loads((build / "metrics.json").read_text())


def _needed(facts):
    return {(t, c) for t, c in rows(facts / "needed.csv")}


def test_cross_build_infers_exec_for_tools(tmp_path):
    _, build, facts, metrics = _configure(tmp_path)
    assert metrics["errors"] == []
    # gen runs and fmt is loaded by blob's action: both are built once, for the
    # exec configuration; the product (app, blob) for the target.
    assert _needed(facts) == {("app", TOY_A), ("blob", TOY_A), ("gen", EXEC), ("fmt", EXEC)}
    assert rows(facts / "tool_dep.csv") == {("blob", TOY_A, "gen"), ("blob", TOY_A, "fmt")}
    # No pin: the only toolchain that runs on toy and targets toy is tcy; the
    # only one that targets toy-a is tcx.
    ninja = build / "souffle"
    text = (ninja / "build.ninja").read_text()
    assert "cmd = /bin/cp ../../project/gen.sh toy.release.none/gen\n" in text  # tcy
    assert "cmd = cp ../../project/app.txt toy-a.debug.none/app\n" in text  # tcx
    subprocess.run(["ninja", "-C", str(ninja)], check=True, capture_output=True)
    assert (ninja / "toy-a.debug.none" / "blob").read_text() == (
        "symbol halide_blob\nplugin: formatted\npayload\n"
    )


def test_native_build_shares_the_tool_instance(tmp_path):
    request = (
        'config_decl_setting = [["native", "native.opt", "release"]]\nrequest = [["app", "native"]]'
    )
    _, build, facts, metrics = _configure(tmp_path, request=request)
    assert metrics["errors"] == []
    # The exec configuration of toy.release.none is itself: one instance each.
    assert _needed(facts) == {(t, EXEC) for t in ("app", "blob", "gen", "fmt")}
    text = (build / "souffle" / "build.ninja").read_text()
    assert "cmd = /bin/cp ../../project/app.txt toy.release.none/app\n" in text  # tcy
    assert text.count("build toy.release.none/gen:") == 1


def test_redirection_and_program_are_emitted(tmp_path):
    _, build, _, _ = _configure(tmp_path)
    text = (build / "souffle" / "build.ninja").read_text()
    edge = text.split("build toy-a.debug.none/blob: ")[1].split("\n\n")[0]
    # stdin is an explicit input; the program and the plugin are implicit.
    src = "../../project/input.txt"
    assert edge.startswith(f"run {src} | toy.release.none/fmt toy.release.none/gen\n")
    assert (
        "cmd = toy.release.none/gen halide_blob toy.release.none/fmt"
        f" < {src} > toy-a.debug.none/blob\n"
    ) in edge + "\n"


def test_why_explains_a_tools_configuration_and_toolchain(tmp_path, capsys):
    project, build, _, _ = _configure(tmp_path)
    capsys.readouterr()
    argv = ["why", "-C", str(build), "--package-path", str(DEFINED)]
    query = ["--flag", "/bin/cp", "--output", "toy.release.none/gen", str(project)]
    # By default the existence of the action is one line (--demand explains it).
    assert main([*argv, *query]) == 0
    assert "tool_dep(" not in capsys.readouterr().out
    assert main([*argv, "--with-demand", *query]) == 0
    out = capsys.readouterr().out
    # gen is needed for exec because blob's action runs it ...
    assert 'needed("gen", ‹toy.release.none›)' in out
    assert 'tool_dep("blob", ‹toy-a.debug.none›, "gen")' in out
    assert 'action_run("out:toy-a.debug.none/blob", 0, "", "gen", "run")' in out
    # ... and tcy compiles it because it runs on and targets toy; no pin.
    assert 'tc_select("toy", "toy", "toy", "tcy")' in out
    assert 'toolchain_can_target("tcy", "toy", "toy")' in out
    assert '!pinned("toy", "toy", "toy")' in out
    assert main(["why-not", "-C", str(build), 'tc_candidate("toy", "toy", "toy", "tcx")']) == 0
    assert "toolchain_can_target" in capsys.readouterr().out


def test_unresolved_tool_is_an_error(tmp_path):
    spec = (PROJECT / "spec.toml").read_text().replace('"gen", "halide_blob"', '"nope", "s"')
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    (project / "spec.toml").write_text(spec)
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", package_paths=[DEFINED])
    metrics = json.loads((tmp_path / "build" / "metrics.json").read_text())
    assert ["unresolved dependency", "tool nope of blob has no definition"] in metrics["errors"]


def test_cross_example_builds_blurgen_only_for_exec(cross_build):
    facts = cross_build / "souffle" / "facts"
    metrics = json.loads((cross_build / "metrics.json").read_text())
    assert metrics["errors"] == []
    configs = {c for t, c in rows(facts / "needed.csv") if t == "blurgen"}
    assert configs == {
        "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], nil]]]]"
    }
    users = {c for t, c, g in rows(facts / "tool_dep.csv") if (t, g) == ("blur", "blurgen")}
    assert len(users) == 3
