# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
DEFINED = ROOT / "tests" / "fixtures" / "defined"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def _project(tmp_path, buildlog, spec):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(buildlog)
    (project / "spec.toml").write_text(spec)
    return project


def test_author_user_relation_is_rejected(tmp_path):
    project = _project(
        tmp_path,
        'uses = ["toy-platform"]\nspec = ["spec.toml"]\n',
        'toolchain_pin = [["cxx", "toy", "toy", "tc"]]\n',
    )
    with pytest.raises(ValueError, match="toolchain_pin is a user relation.*--user"):
        configure_project(
            project, tmp_path / "build", emit=False, package_paths=[DEFINED], update_lock=True
        )


def test_author_decides_bypass_and_conflict_reason(tmp_path):
    project = _project(
        tmp_path,
        'uses = ["toy-platform"]\nspec = ["spec.toml"]\n'
        "[author_decides]\n"
        'toolchain_pin = "firmware: only tc-a boots"\n',
        'toolchain_pin = [["cxx", "toy", "toy", "tc-a"]]\n',
    )
    user = tmp_path / "user.toml"
    user.write_text('toolchain_pin = [["cxx", "toy", "toy", "tc-b"]]\n')
    build = tmp_path / "build"
    with pytest.raises(SystemExit):
        configure_project(
            project,
            build,
            emit=False,
            package_paths=[DEFINED],
            update_lock=True,
            user_files=[user],
        )
    errors = json.loads((build / "metrics.json").read_text())["errors"]
    assert errors == [
        [
            "toolchain pinned twice",
            'cxx running on toy for toy: tc-a and tc-b; author_decides.toolchain_pin = "firmware: only tc-a boots"',
        ]
    ]


def test_user_files_and_vars_are_recorded_and_reused(tmp_path):
    project = _project(
        tmp_path,
        'uses = ["toy-platform"]\nspec = ["spec.toml"]\n[vars]\nCFG = "bad"\n',
        'config_decl = [["release", "toy"]]\n',
    )
    user = tmp_path / "user.toml"
    user.write_text('config_default = [["${CFG}"]]\n')
    build = tmp_path / "build"
    _facts, _ = configure_project(
        project,
        build,
        emit=False,
        package_paths=[DEFINED],
        update_lock=True,
        user_files=[user],
        var_overrides={"CFG": "release"},
    )
    assert (build / "facts-in" / "round-0" / "config_default.facts").read_text() == "release\n"
    state = json.loads((build / "buildlog-state.json").read_text())
    assert state["user_files"] == [str(user.resolve())]
    assert state["vars"] == {"CFG": "release"}
    _facts2, _ = configure_project(project, build, emit=False, package_paths=[DEFINED])
    assert (build / "facts-in" / "round-0" / "config_default.facts").read_text() == "release\n"
