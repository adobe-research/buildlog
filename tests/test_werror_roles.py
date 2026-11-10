# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""cc_werror is per role: "*" covers the
target-role configurations, and exec configurations, where build-only tools
are built, need an explicit cc_werror_exec, as sanitizers do."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")

CROSS = Path(__file__).resolve().parents[1] / "examples" / "cross"
PRESET = CROSS / "presets" / "apple-clang.toml"


def build_cross(tmp_path, extra):
    user = tmp_path / "user.toml"
    user.write_text(PRESET.read_text() + "\n" + extra)
    build = tmp_path / "build"
    shutil.rmtree(build, ignore_errors=True)
    _, metrics = configure_project(CROSS, build, update_lock=True, user_files=[user])
    assert metrics["errors"] == []
    return build


def cc_commands(build):
    lines = (build / "souffle" / "build.ninja").read_text().splitlines()
    out = {}
    for i, ln in enumerate(lines):
        if ln.startswith("build obj/") and ".o:" in ln:
            cmd = next(x for x in lines[i + 1 :] if x.startswith("  cmd = "))
            out[ln.split(":")[0][6:]] = cmd.split()
    return out


def split(cmds, tool):
    mine = {o: c for o, c in cmds.items() if f"/{tool}/" in o}
    assert mine, tool
    return mine


@host.cross
def test_star_skips_exec_configurations(tmp_path):
    build = build_cross(tmp_path, 'cc_werror = [["*", "*"]]\n')
    cmds = cc_commands(build)
    tool = split(cmds, "blurgen")
    assert all("-Werror" not in c for c in tool.values())
    others = {o: c for o, c in cmds.items() if o not in tool}
    assert others and all("-Werror" in c for c in others.values())


@host.cross
def test_exec_opt_in(tmp_path):
    build = build_cross(tmp_path, 'cc_werror = [["*", "*"]]\ncc_werror_exec = [["blurgen"]]\n')
    cmds = cc_commands(build)
    assert all("-Werror" in c for c in split(cmds, "blurgen").values())
    build = build_cross(tmp_path, 'cc_werror_exec = [["*"]]\n')
    cmds = cc_commands(build)
    assert all("-Werror" in c for c in split(cmds, "blurgen").values())
    # Without cc_werror the exec opt-in touches only exec configurations.
    assert all("-Werror" not in c for o, c in cmds.items() if "/blurgen/" not in o)


def test_exec_opt_in_names_a_real_target(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text(PRESET.read_text() + '\ncc_werror_exec = [["ghost"]]\n')
    with pytest.raises(SystemExit):
        configure_project(CROSS, tmp_path / "build", update_lock=True, user_files=[user])
    errors = json.loads((tmp_path / "build" / "metrics.json").read_text())["errors"]
    assert ["unknown target", "ghost (cc_werror_exec)"] in errors
