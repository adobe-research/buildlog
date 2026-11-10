# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: the tests, run by `buildlog test`.

test/CMakeLists.txt registers 14 ctest tests. On this Mac two of them run a
program zlib builds: zlib_example (against the dylib) and
zlib_static_example (against libz.a). Here they are one `test` fact
(tests.toml), zlib_example, run in both configurations: release (the dylib)
and release-static (libz.a).
The other twelve install zlib or configure consumer projects with
find_package/add_subdirectory; install and export are out of scope (the
README's ledger). The acceptance check is the same pass set as ctest's and
the same output as CMake's programs (baseline/example-output.txt).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.zlib_case import BASELINE, REL, STATIC, configure, needs_zlib

pytestmark = needs_zlib

# CMake's test -> (buildlog's test, configuration).
IN_SCOPE = {"zlib_example": ("zlib_example", REL), "zlib_static_example": ("zlib_example", STATIC)}


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib") / "build"
    configure(build)
    return build


@pytest.fixture(scope="module")
def results(build):
    """`buildlog test` once; it builds and runs the programs in test/<config>/<name>."""
    out = build.parent / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(build), "--json", str(out)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.read_text())


def test_buildlog_test_passes_ctests_in_scope_set(results):
    assert results["counts"] == {"passed": 2, "skipped": 0, "failed": 0, "not run": 0}
    assert {(r["name"], r["config"]) for r in results["results"]} == set(IN_SCOPE.values())
    passed_by_ctest = {
        line.split(": ")[1].split(" ")[0]
        for line in (BASELINE / "ctest.txt").read_text().splitlines()
        if "Passed" in line
    }
    assert set(IN_SCOPE) <= passed_by_ctest


@pytest.mark.parametrize("name", sorted(IN_SCOPE))
def test_programs_print_what_cmakes_print(build, results, name):
    # Each case ran in test/<config>/<name>; rerun the program to read its output.
    ours, cfg = IN_SCOPE[name]
    exe = build / "souffle" / "bin" / cfg / ours
    work = build / "souffle" / "test" / cfg / ours
    got = subprocess.run([str(exe)], cwd=work, check=True, capture_output=True, text=True).stdout
    expected = (BASELINE / "example-output.txt").read_text().split(f"===== {name}\n")[1]
    assert got == expected.split("=====")[0]
