# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Execution-totality hypotheses against the unmodified shipped L0/L1 rules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "buildlog" / "layers" / "core"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="Souffle not installed")


@pytest.mark.parametrize(
    ("case", "policy", "expected_error", "has_exec"),
    [
        ("inherit", 'setting_exec_inherit("k").', None, True),
        ("fixed", 'setting_exec_value("k","on").', None, True),
        ("override-inherit", 'exec_setting("k","inherit").', None, True),
        ("override-fixed", 'exec_setting("k","on").', None, True),
        (
            "inherit-in-domain",
            'exec_setting("k","inherit"). setting_value("k","inherit").',
            "setting value is reserved",
            True,
        ),
        ("multi-platform", 'setting_exec_inherit("k").', None, True),
        ("equal-priorities", 'setting_exec_inherit("k").', None, True),
        ("empty-settings", "", None, True),
        ("no-host", 'setting_exec_inherit("k").', None, False),
        ("missing-policy", "", "setting key without exec policy", False),
        (
            "mixed-policy",
            'setting_exec_inherit("k"). setting_exec_value("k","on").',
            "setting key with two exec policies",
            False,
        ),
        (
            "double-policy",
            'setting_exec_value("k","off"). setting_exec_value("k","on").',
            "setting key with two exec policies",
            False,
        ),
        (
            "double-override",
            'exec_setting("k","off"). exec_setting("k","on").',
            "exec setting given twice",
            False,
        ),
        ("invalid-override", 'exec_setting("k","ghost").', "exec setting outside domain", False),
        (
            "invalid-default",
            'setting_exec_inherit("k").',
            "setting default outside domain",
            False,
        ),
        (
            "invalid-fixed-policy",
            'setting_exec_value("k","ghost").',
            "exec transition not idempotent",
            True,
        ),
    ],
)
def test_shipped_execution_totality(case, policy, expected_error, has_exec, tmp_path):
    program = tmp_path / "exec.dl"
    includes = "".join(
        f'#include "{CORE / name}"\n' for name in ("types.dl", "L0_actions.dl", "L1_targets.dl")
    )
    declarations = (
        'config_decl("cfg","multi"). platform_slice("multi","target").'
        if case == "multi-platform"
        else 'config_decl("cfg","target").'
    )
    settings = ""
    if case != "empty-settings":
        default = "ghost" if case == "invalid-default" else "off"
        settings = (
            'setting_key("k",0). setting_value("k","off").'
            + ("" if case == "inherit-in-domain" else 'setting_value("k","on").')
            + f'setting_default("k","{default}").'
        )
    if case == "equal-priorities":
        settings += (
            'setting_key("j",0). setting_value("j","off").'
            'setting_default("j","off"). setting_exec_inherit("j").'
        )
    host = "" if case == "no-host" else 'host_platform("host").'
    if "exec_setting(" in policy:
        policy = 'setting_exec_inherit("k").' + policy
    program.write_text(
        includes
        # Unrelated request-gate boundaries are empty in this config-only fixture.
        + ".decl request_gated(t:symbol)\n.decl request_open(t:symbol)\n"
        + 'platform("target","target"). platform("host","host").\n'
        + declarations
        + settings
        + host
        + policy
        + "\n.output base_config\n.output exec_config\n.output exec_value\n.output error\n"
    )
    result = subprocess.run(
        [str(souffle_path()), "-D", str(tmp_path), str(program)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr[-12000:]

    def rows(relation):
        return {
            tuple(line.split("\t"))
            for line in (tmp_path / f"{relation}.csv").read_text().splitlines()
            if line
        }

    bases, execution, errors = rows("base_config"), rows("exec_config"), rows("error")
    assert len(bases) == 1
    for (base,) in bases:
        destinations = {e for c, e in execution if c == base}
        assert len(destinations) == int(has_exec), (case, bases, execution, errors)
        for destination in destinations:
            assert (destination, destination) in execution
    if expected_error:
        assert expected_error in {kind for kind, _ in errors}, errors
    else:
        assert not errors
    if case == "inherit-in-domain":
        # "inherit" is reserved for the policy: the override keeps the value.
        assert ("k", "off", "inherit") not in rows("exec_value")
        assert (
            "[target, [[k, off], nil]]",
            "[host, [[k, off], nil]]",
        ) in execution
    if case == "equal-priorities":
        assert (
            "[target, [[j, off], [[k, off], nil]]]",
            "[host, [[j, off], [[k, off], nil]]]",
        ) in execution
