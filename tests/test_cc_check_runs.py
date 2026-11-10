# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `runs` check kind (buildlog-cpp L4_checks.dl): the program is built and
then run on this machine; the answer is yes only if it exits 0
(CMake's check_cxx_source_runs)."""

from __future__ import annotations

import pytest

from .test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

pytestmark = [pytest.mark.integration, needs_souffle]

SOURCES = {"main.cpp": "int main() { return 0; }\n"}
SPEC = """
cc_check_compiles = [["BUILDS", "int main(void) { return 1; }"]]
prop_when = [
  ["app", "cc.define", "OK", "private", "cc.check.RUNS_OK"],
  ["app", "cc.define", "FAILS", "private", "cc.check.RUNS_FAILS"],
  ["app", "cc.define", "NOT_BUILT", "private", "cc.check.RUNS_NOT_BUILT"],
  ["app", "cc.define", "ARGS", "private", "cc.check.RUNS_NEEDS_ARGS"],
  ["app", "cc.define", "COMPILES_ONLY", "private", "cc.check.BUILDS"],
]
request = [["app", "rel"]]

[cc_check.RUNS_OK]
lang = "c"
kind = "runs"
subject = "int main(void) { return 0; }"

[cc_check.RUNS_FAILS]
lang = "c"
kind = "runs"
subject = "int main(void) { return 1; }"

[cc_check.RUNS_NOT_BUILT]
lang = "c"
kind = "runs"
subject = "int main(void) { return not_declared(); }"

[cc_check.RUNS_NEEDS_ARGS]
lang = "c"
kind = "runs"
subject = "int main(void) { return WANT; }"
cc_check_args = "-DWANT=0"

[target.app]
kind = "executable"
sources = ["main.cpp"]
"""


def test_a_runs_check_is_yes_only_if_the_program_runs_and_exits_zero(tmp_path):
    build, metrics = configure(project(tmp_path, SPEC, SOURCES), tmp_path)
    assert metrics["errors"] == []
    cxx = cmd_of(build, "app/main.cpp.o")
    assert "-DOK" in cxx and "-DARGS" in cxx
    assert "-DFAILS" not in cxx and "-DNOT_BUILT" not in cxx
    assert "-DCOMPILES_ONLY" in cxx  # a compiles check builds the program and stops there
