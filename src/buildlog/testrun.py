# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`buildlog test`: run the test cases of a configured build (tests.json).

ctest's semantics, from typed facts (L1 `test`, L0 `test_case`):
  - a case listed with a reason not to run (a cross configuration, a broken
    program) is "not run", unless the user states the build machine runs
    the platform (platform_runs_on), when it runs through its launcher;
  - output matching a skip regex: skipped;
  - output matching a fail regex: failed;
  - with pass regexes, the output decides and the exit status is ignored;
    without, exit status 0 passes;
  - a timeout fails.
  - a case's env_path entries are prepended to the variable's path list
    (ctest's ENVIRONMENT_MODIFICATION path_list_prepend).
Each case runs in its working directory (created) with the environment plus
its own variables; serial cases run alone, after the others. The files a
case needs are built with ninja first, unless --no-build.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FORMAT = "buildlog-tests"


def load(ninja_dir: Path) -> list[dict]:
    path = ninja_dir / "tests.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    if doc.get("format") != FORMAT or doc.get("version") != 1:
        raise SystemExit(f"error: {path} is not a version 1 {FORMAT} file (reconfigure)")
    return doc["tests"]


def select(tests, include=None, exclude=None, label=None):
    def keep(t):
        if include and not re.search(include, t["name"]):
            return False
        if exclude and re.search(exclude, t["name"]):
            return False
        return not label or any(re.search(label, lb) for lb in t["labels"])

    return [t for t in tests if keep(t)]


def verdict(t: dict, code, out: str) -> str:
    if any(re.search(r, out) for r in t["skip"]):
        return "skipped"
    if code == "timeout" or any(re.search(r, out) for r in t["fail"]):
        return "failed"
    if t["pass"]:
        return "passed" if any(re.search(r, out) for r in t["pass"]) else "failed"
    return "passed" if code == 0 else "failed"


def run_case(ninja_dir: Path, t: dict, timeout: float) -> dict:
    if t["not_run"]:
        return {**_key(t), "status": "not run", "reason": "; ".join(t["not_run"]), "seconds": 0.0}
    cwd = ninja_dir / t["cwd"]
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **t["env"]}
    for key, paths in t.get("env_path", {}).items():
        # path_list_prepend: the listed directories, then what was there
        entries = [str((ninja_dir / p).resolve()) for p in paths]
        env[key] = os.pathsep.join([*entries, *([env[key]] if env.get(key) else [])])
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [*t.get("launcher", []), *t["argv"]],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        code, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = "timeout"
        out = exc.stdout if isinstance(exc.stdout, str) else ""
    except OSError as exc:
        code, out = "error", str(exc)
    seconds = round(time.monotonic() - start, 2)
    return {**_key(t), "status": verdict(t, code, out), "exit": code, "seconds": seconds,
            "output": out}  # fmt: skip


def _key(t):
    return {"name": t["name"], "config": t["config"]}


def run(ns) -> int:
    builddir = Path(ns.builddir or (Path(ns.project) / "buildlog-build"))
    ninja_dir = builddir / "souffle"
    if not (ninja_dir / "build.ninja").exists():
        print(
            f"error: {ninja_dir / 'build.ninja'} not found (run buildlog configure)",
            file=sys.stderr,
        )
        return 1
    tests = select(load(ninja_dir), ns.tests_regex, ns.exclude_regex, ns.label_regex)
    if ns.list:
        for t in tests:
            print(
                f"{t['name']} [{t['config']}]"
                + (f"  (not run: {'; '.join(t['not_run'])})" if t["not_run"] else "")
                + (f"  (via {shlex.join(t['launcher'])})" if t.get("launcher") else "")
            )
        return 0
    if not tests:
        print("no tests")
        return 0
    needs = sorted({p for t in tests if not t["not_run"] for p in t["needs"]})
    if needs and not ns.no_build:
        built = subprocess.run(["ninja", "-C", str(ninja_dir), *needs], check=False)
        if built.returncode != 0:
            print("error: building the tests' files failed", file=sys.stderr)
            return 1
    parallel = [t for t in tests if not t["serial"]]
    serial = [t for t in tests if t["serial"]]
    with ThreadPoolExecutor(max_workers=max(1, ns.jobs)) as pool:
        results = list(pool.map(lambda t: run_case(ninja_dir, t, ns.timeout), parallel))
    results += [run_case(ninja_dir, t, ns.timeout) for t in serial]
    order = {(t["name"], t["config"]): i for i, t in enumerate(tests)}
    results.sort(key=lambda r: order[(r["name"], r["config"])])
    for r in results:
        line = f"{r['status']:8} {r['seconds']:8.2f}s  {r['name']} [{r['config']}]"
        if r["status"] == "not run":
            line += f"  ({r['reason']})"
        print(line)
        if r["status"] == "failed" and ns.output_on_failure:
            tail = r.get("output", "").strip().splitlines()[-20:]
            print("\n".join("    " + x for x in tail))
    counts = {
        s: sum(r["status"] == s for r in results)
        for s in ("passed", "skipped", "failed", "not run")
    }
    total = round(sum(r["seconds"] for r in results), 1)
    print(", ".join(f"{v} {k}" for k, v in counts.items()) + f" ({total} s)")
    if ns.json:
        slim = [{k: v for k, v in r.items() if k != "output"} for r in results]
        Path(ns.json).write_text(json.dumps({"counts": counts, "results": slim}, indent=1) + "\n")
    return 1 if counts["failed"] else 0


def add_parser(sub) -> None:
    p = sub.add_parser("test", help="run the configured build's tests (ctest's semantics)")
    p.add_argument("-C", "--builddir")
    p.add_argument("-R", "--tests-regex", help="only tests whose name matches")
    p.add_argument("-E", "--exclude-regex", help="not tests whose name matches")
    p.add_argument("-L", "--label-regex", help="only tests with a matching label")
    p.add_argument("-j", "--jobs", type=int, default=1)
    p.add_argument(
        "--timeout", type=float, default=1500.0, help="seconds per test (ctest's default)"
    )
    p.add_argument("--no-build", action="store_true", help="do not build the tests' files first")
    p.add_argument("--list", action="store_true", help="list the selected tests")
    p.add_argument("--output-on-failure", action="store_true")
    p.add_argument("--json", help="write the results as JSON")
    p.add_argument("project", nargs="?", default=".")
    p.set_defaults(func=run)
