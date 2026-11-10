# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A check request's id is injective in its arguments: two checks whose fields
differ only in where a `|` falls get distinct ids and independent answers."""

from __future__ import annotations

import pytest

from .test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

pytestmark = [pytest.mark.integration, needs_souffle]

SPEC = """
cc_check_headers = [["SPLIT_LATE", "stdio.h"], ["SPLIT_EARLY", "0;}|stdio.h"]]
prop_when = [
  ["app", "cc.define", "LATE", "private", "cc.check.SPLIT_LATE"],
  ["app", "cc.define", "EARLY", "private", "cc.check.SPLIT_EARLY"],
]
request = [["app", "rel"]]

# subject + "|" + headers is "int main(void){return 1|0;}|stdio.h" for both
[cc_check.SPLIT_LATE]
lang = "c"
kind = "compiles"
subject = "int main(void){return 1|0;}"

[cc_check.SPLIT_EARLY]
lang = "c"
kind = "compiles"
subject = "int main(void){return 1"

[target.app]
kind = "executable"
sources = ["main.c"]
"""


def test_checks_differing_only_where_the_separator_falls_do_not_collide(tmp_path):
    build, metrics = configure(
        project(tmp_path, SPEC, {"main.c": "int main(void) { return 0; }\n"}), tmp_path
    )
    assert metrics["errors"] == []
    cc = cmd_of(build, "app/main.c.o")
    assert "-DLATE" in cc and "-DEARLY" not in cc
