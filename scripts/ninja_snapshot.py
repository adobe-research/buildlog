#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Snapshot and diff the build.ninja files of the example and test projects.

The refactor invariant: every emitted build.ninja stays
byte-identical unless a change is intended and explained.

    uv run python scripts/ninja_snapshot.py take LABEL [--pytest] [-- PYTEST_ARGS]
    uv run python scripts/ninja_snapshot.py diff LABEL_A LABEL_B

`take` configures the committed projects into .snap/work/ and copies their
build.ninja to .snap/LABEL/projects/. With --pytest it also runs the test
suite with a fixed --basetemp (so tmp paths are stable), and the conftest
hook (BUILDLOG_NINJA_SNAPSHOT) copies every build.ninja a test left in its
tmp_path to .snap/LABEL/pytest/. `diff` compares two labels byte by byte,
and again with the regeneration edge's input list masked (moving a layer
file between packages changes that list and nothing else).
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / ".snap"
RULES = str(ROOT / "tests" / "fixtures" / "rule-packages")
PROJECTS = [
    ("cross", ROOT / "examples" / "cross", []),
    ("bilateral_grid", ROOT / "tests/projects/halide/bilateral_grid", ["--package-path", RULES]),
    ("linear_blur", ROOT / "tests/projects/halide/linear_blur", ["--package-path", RULES]),
    (
        "bilateral_grid_coverage",
        ROOT / "tests/projects/halide/bilateral_grid_coverage",
        ["--package-path", RULES, "--keep-going"],
    ),
    (
        "libhalide",
        ROOT / "case-studies/halide/libhalide",
        ["--package-path", str(ROOT / "case-studies/halide/packages")],
    ),
    ("collision", ROOT / "tests/fixtures/collision", []),
    (
        "target_ids",
        ROOT / "tests/fixtures/target-ids/project",
        ["--package-path", str(ROOT / "tests/fixtures/target-ids")],
    ),
]
REGEN = re.compile(r"^(build build\.ninja: regen) .*$", re.MULTILINE)


def take(label: str, pytest: bool, pytest_args: list[str]) -> int:
    out = SNAP / label
    if out.exists():
        shutil.rmtree(out)
    (out / "projects").mkdir(parents=True)
    status = 0
    for name, project, extra in PROJECTS:
        build = SNAP / "work" / name
        shutil.rmtree(build, ignore_errors=True)
        cmd = ["uv", "run", "--frozen", "buildlog", "configure", "--update-lock", *extra]
        cp = subprocess.run([*cmd, "-C", str(build), str(project)], cwd=ROOT, check=False)
        ninja = build / "souffle" / "build.ninja"
        if ninja.exists():
            shutil.copy(ninja, out / "projects" / f"{name}.ninja")
        else:
            print(f"{name}: no build.ninja (configure exit {cp.returncode})", file=sys.stderr)
            status = 1
    if pytest:
        env = {**os.environ, "BUILDLOG_NINJA_SNAPSHOT": str(out / "pytest")}
        # Two levels away from the repository: `why` prints paths near the
        # working directory relative to it, which some tests compare against.
        base = Path.home() / ".cache" / "buildlog-ninja-snapshot"
        cp = subprocess.run(
            ["uv", "run", "--frozen", "pytest", "-q", f"--basetemp={base}", *pytest_args],
            cwd=ROOT,
            env=env,
            check=False,
        )
        status = status or cp.returncode
    return status


def _files(d: Path) -> dict[str, Path]:
    return {str(p.relative_to(d)): p for p in sorted(d.rglob("*.ninja"))} if d.is_dir() else {}


def diff(a: str, b: str) -> int:
    fa, fb = _files(SNAP / a), _files(SNAP / b)
    only_a, only_b = sorted(set(fa) - set(fb)), sorted(set(fb) - set(fa))
    same = changed_regen = 0
    changed: list[str] = []
    for key in sorted(set(fa) & set(fb)):
        if filecmp.cmp(fa[key], fb[key], shallow=False):
            same += 1
            continue
        ta, tb = (REGEN.sub(r"\1 ...", p.read_text()) for p in (fa[key], fb[key]))
        if ta == tb:
            changed_regen += 1
            print(f"regen inputs only: {key}")
        else:
            changed.append(key)
            print(f"DIFFERS: {key}")
    for key in only_a:
        print(f"only in {a}: {key}")
    for key in only_b:
        print(f"only in {b}: {key}")
    print(
        f"{same} identical, {changed_regen} differ only in the regeneration inputs, "
        f"{len(changed)} differ, {len(only_a)}+{len(only_b)} unpaired"
    )
    return 1 if changed or only_a or only_b else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take")
    t.add_argument("label")
    t.add_argument("--pytest", action="store_true")
    t.add_argument("pytest_args", nargs="*")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    args = ap.parse_args()
    if args.cmd == "take":
        return take(args.label, args.pytest, args.pytest_args)
    return diff(args.a, args.b)


if __name__ == "__main__":
    raise SystemExit(main())
