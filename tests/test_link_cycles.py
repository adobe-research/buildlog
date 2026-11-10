# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cycles among static libraries (L3 link_scc; a "library" is static by
default): the members of each cycle in a linked target's closure, per
configuration, and the delimiters a linker with a "group" cycle policy
puts around them. Apple's ld needs none ("none"), so the cyclic link builds
and runs as is."""

from __future__ import annotations

import subprocess

from tests.test_cpp_mechanisms import (
    cmd_of,
    configure,
    needs_souffle,
    project,
    run_ninja,
)

from .host import MACOS

REL = "arm64-macos.release.none"

FILES = {
    "a.c": "int b(int);\nint a(int n) { return n ? b(n - 1) + 1 : 0; }\n",
    "b.c": "int a(int);\nint b(int n) { return n ? a(n - 1) + 10 : 0; }\n",
    "c.c": "int a(int);\nint c(void) { return a(4); }\n",
    "d.c": "int e(void);\nint d(void) { return e() + 1; }\n",
    "e.c": "int e(void) { return 100; }\n",
    "main.c": (
        '#include <stdio.h>\nint c(void);\nint d(void);\nint main(void) { printf("%d\\n", c() + d()); }\n'
    ),
}

# a and b need each other; c needs a; d needs e (acyclic); app needs c and d.
SPEC = """
target = [["a"], ["b"], ["c"], ["d"], ["e"], ["app"]]
native_kind = [["a", "library"], ["b", "library"], ["c", "library"],
  ["d", "library"], ["e", "library"], ["app", "executable"]]
c_source = [["a", "a.c"], ["b", "b.c"], ["c", "c.c"], ["d", "d.c"], ["e", "e.c"], ["app", "main.c"]]
dep = [["a", "b", "private"], ["b", "a", "private"], ["c", "a", "private"],
  ["d", "e", "private"], ["app", "c", "private"], ["app", "d", "private"]]
request = [["app", "rel"]]
"""

GROUP_MANIFEST = """\
name = "group-linker"
version = "0.1.0"
rank = 5
layers = ["group-linker.dl"]
requires = { buildlog-cpp = ">=0.1" }
extends = ["linker_group_delims"]
"""
GROUP_DL = """\
linker_group_delims(tc, p, "-Wl,-GROUP-OPEN", "-Wl,-GROUP-CLOSE") :-
  toolchain(tc, "clang", _), platform_constraint(p, "os", "macos").
"""


def scc_rows(build):
    rows = (build / "souffle" / "facts" / "link_scc.csv").read_text().splitlines()
    return sorted((r.split("\t")[0], r.split("\t")[2], r.split("\t")[3]) for r in rows)


@needs_souffle
def test_a_library_cycle_is_one_group_and_links_with_apple_ld(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert scc_rows(build) == [
        ("app", "a", "a"),
        ("app", "a", "b"),
        ("app", "b", "a"),
        ("app", "b", "b"),
    ]
    assert "GROUP" not in cmd_of(build, f"bin/{REL}/app")
    run_ninja(build)
    exe = build / "souffle" / "bin" / REL / "app"
    assert subprocess.check_output([str(exe)], text=True) == "123\n"


@needs_souffle
def test_a_group_policy_delimits_only_the_cycle(tmp_path):
    # macOS: a test package supplies the delimiters (Apple's ld needs none).
    # Linux: GNU ld's "group" policy is the toolchain's own, so no package.
    packages = {"group-linker": (GROUP_MANIFEST, GROUP_DL)} if MACOS else None
    open_flag, close_flag = (
        ("-Wl,-GROUP-OPEN", "-Wl,-GROUP-CLOSE")
        if MACOS
        else ("-Wl,--start-group", "-Wl,--end-group")
    )
    proj = project(tmp_path, SPEC, FILES, packages=packages)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cmd = cmd_of(build, f"bin/{REL}/app")
    opened = cmd.index(open_flag)
    closed = cmd.index(close_flag)
    inside = cmd[opened:closed]
    assert "liba.a" in inside and "libb.a" in inside
    assert all(f"lib{x}.a" not in inside for x in "cde")
