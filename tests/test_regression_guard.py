# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression guard: how much work a configure does, against a committed budget.

The counts are deterministic: the composed and pruned clause counts, the passes
and rounds of a cold configure, and the rounds of a warm one (nothing changed,
the up-to-date skip off, so the run is evaluated) and of one after an edit of a
user file. A change that makes the composed program grow, pruning stop pruning,
or a configure need another round fails here before it shows in a clock.

After an intended change, rewrite the budget and commit it:

    BUILDLOG_UPDATE_BUDGET=1 uv run pytest tests/test_regression_guard.py

The timing budget (BUILDLOG_TIMING_BUDGET=1) is opt-in: clocks depend on the
machine."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CS = ROOT / "case-studies"
BUILDLOG = ROOT / ".venv" / "bin" / "buildlog"
BUDGET = Path(__file__).parent / "budgets" / "configure.json"
TRIVIAL = ROOT / "tests" / "projects" / "trivial"
CLAUSE_TOLERANCE = 0.02  # of the composed or pruned clause count

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(sys.platform != "darwin", reason="the studies' presets pin Apple clang"),
]

# name -> (project, user preset, BUILDLOG_PACKAGE_PATH)
STUDIES = {
    "trivial": (TRIVIAL, TRIVIAL / "presets" / "apple-clang-arm64.toml", None),
    "zlib": (
        CS / "zlib/project",
        CS / "zlib/project/presets/apple-clang-arm64.toml",
        None,
    ),
    "libhalide": (
        CS / "halide/libhalide",
        CS / "halide/libhalide/presets/homebrew-arm64.toml",
        CS / "halide/packages",
    ),
}


def measure(name, tmp_path):
    project, preset, packages = STUDIES[name]
    if not (project / "buildlog.toml").exists():
        pytest.skip(f"{name}: tree unavailable")
    build = tmp_path / "build"
    user = tmp_path / "user.toml"
    shutil.copy(preset, user)
    env = {
        **os.environ,
        "BUILDLOG_CACHE_DIR": str(tmp_path / "cache"),
        "BUILDLOG_UPTODATE": "0",
        "BUILDLOG_PRUNE_REPORT": "1",
    }
    env.pop("BUILDLOG_PACKAGE_PATH", None)
    env.pop("BUILDLOG_NO_PRUNE", None)
    if packages:
        env["BUILDLOG_PACKAGE_PATH"] = str(packages)
    cmd = [str(BUILDLOG), "configure", "-C", str(build), "--user", str(user), str(project)]

    def configure():
        t0 = time.perf_counter()
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
        seconds = time.perf_counter() - t0
        if done.returncode:
            pytest.skip(f"{name}: not configurable here ({done.stderr[-300:]})")
        return json.loads((build / "metrics.json").read_text()), seconds

    cold, cold_s = configure()
    warm, warm_s = configure()
    user.write_text(user.read_text() + "\n# an edit that changes no setting\n")
    edit, edit_s = configure()
    composed, pruned = cold["prune"]["clauses"]
    counts = {
        "composed_clauses": composed,
        "pruned_clauses": pruned,
        "cold_passes": len(cold["outer"]),
        "cold_rounds": len(cold["rounds"]),
        "warm_rounds": len(warm["rounds"]),
        "edit_rounds": len(edit["rounds"]),
    }
    return counts, {"cold": cold_s, "warm": warm_s, "edit": edit_s}


def update_message(name):
    return (
        f"\nIf this change is intended, rewrite the budget and commit it:\n"
        f"    BUILDLOG_UPDATE_BUDGET=1 uv run pytest tests/test_regression_guard.py -k {name}\n"
    )


@pytest.mark.parametrize("name", list(STUDIES))
def test_configure_work_is_within_budget(name, tmp_path):
    counts, _ = measure(name, tmp_path)
    budget = json.loads(BUDGET.read_text()) if BUDGET.exists() else {}
    if os.environ.get("BUILDLOG_UPDATE_BUDGET") == "1":
        budget[name] = counts
        BUDGET.parent.mkdir(exist_ok=True)
        BUDGET.write_text(json.dumps(budget, indent=2, sort_keys=True) + "\n")
        return
    assert name in budget, f"no budget for {name}.{update_message(name)}"
    want = budget[name]
    problems = []
    for key in ("composed_clauses", "pruned_clauses"):
        slack = max(2, round(want[key] * CLAUSE_TOLERANCE))
        if abs(counts[key] - want[key]) > slack:
            problems.append(f"{key}: {counts[key]}, budget {want[key]} (+-{slack})")
    for key in ("cold_passes", "cold_rounds", "warm_rounds", "edit_rounds"):
        if counts[key] > want[key]:
            problems.append(f"{key}: {counts[key]}, budget at most {want[key]}")
    assert not problems, (
        f"{name} left its budget:\n  " + "\n  ".join(problems) + update_message(name)
    )


@pytest.mark.skipif(
    os.environ.get("BUILDLOG_TIMING_BUDGET") != "1",
    reason="clocks depend on the machine: set BUILDLOG_TIMING_BUDGET=1",
)
def test_trivial_project_is_within_its_time_budget(tmp_path):
    _, seconds = measure("trivial", tmp_path)
    budget = json.loads(BUDGET.read_text())["trivial_seconds"]
    slow = {k: round(v, 2) for k, v in seconds.items() if v > budget[k]}
    assert not slow, (
        f"slower than the budget {budget}: {slow} (seconds; the machine may just be loaded)"
        "\nTo change the budget, edit trivial_seconds in tests/budgets/configure.json."
    )
