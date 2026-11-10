# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line order guarantees that the fidelity comparison (scripts/fidelity_order.py)
relies on: a link line lists a library before the libraries it depends on
(topological over the dependency DAG, lexical among unordered siblings), whatever
order the spec rows come in; -I / -isystem flags follow the class order (own, then
dependencies, then system) and are lexical within a class; -D flags are one lexical
set, so a define cannot depend on the spec's row order."""

from __future__ import annotations

import re
import shlex

from tests import test_include_order as io
from tests.test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

REL = "arm64-macos.release.none"


def libs_in(cmd: str) -> list[str]:
    return [m.group(1) for w in shlex.split(cmd) if (m := re.search(r"lib([A-Za-z0-9_]+)\.a$", w))]


def link_spec(rows: list[str], kinds: list[str]) -> str:
    names = sorted({n for r in rows for n in re.findall(r'"(\w+)"', r)[:2]} | set(kinds))
    return f"""
target = [{", ".join(f'["{n}"]' for n in names)}]
native_kind = [{", ".join(f'["{n}", "{"executable" if n == "app" else "library"}"]' for n in names)}]
c_source = [{", ".join(f'["{n}", "{n}.c"]' for n in names)}]
dep = [{", ".join(rows)}]
request = [["app", "rel"]]
"""


def c_files(names: list[str]) -> dict[str, str]:
    files = {f"{n}.c": f"int {n}(void) {{ return 0; }}\n" for n in names if n != "app"}
    files["app.c"] = "int main(void) { return 0; }\n"
    return files


def link_line(tmp_path, rows):
    spec = link_spec(rows, ["app"])
    names = sorted({n for r in rows for n in re.findall(r'"(\w+)"', r)[:2]} | {"app"})
    build, metrics = configure(project(tmp_path, spec, c_files(names)), tmp_path)
    assert metrics["errors"] == []
    return libs_in(cmd_of(build, f"bin/{REL}/app"))


@needs_souffle
def test_link_order_is_dependency_order_not_lexical(tmp_path):
    # lexical order of the names is the reverse of the dependency order
    rows = [
        '["app", "z_top", "private"]',
        '["z_top", "m_mid", "private"]',
        '["m_mid", "a_low", "private"]',
    ]
    assert link_line(tmp_path, rows) == ["z_top", "m_mid", "a_low"]


@needs_souffle
def test_link_order_diamond_puts_the_shared_base_last(tmp_path):
    rows = [
        '["app", "p_left", "private"]',
        '["app", "q_right", "private"]',
        '["p_left", "a_base", "private"]',
        '["q_right", "a_base", "private"]',
    ]
    assert link_line(tmp_path, rows) == ["p_left", "q_right", "a_base"]


@needs_souffle
def test_link_order_ties_between_unordered_siblings_are_lexical(tmp_path):
    rows = [
        '["app", "b_sib", "private"]',
        '["app", "a_sib", "private"]',
        '["app", "c_sib", "private"]',
    ]
    assert link_line(tmp_path, rows) == ["a_sib", "b_sib", "c_sib"]


@needs_souffle
def test_link_order_does_not_depend_on_spec_row_order(tmp_path):
    rows = [
        '["app", "z_top", "private"]',
        '["z_top", "m_mid", "private"]',
        '["m_mid", "a_low", "private"]',
        '["app", "b_sib", "private"]',
        '["b_sib", "a_low", "private"]',
    ]
    first = link_line(tmp_path / "one", rows)
    second = link_line(tmp_path / "two", list(reversed(rows)))
    assert first == second
    # a_low is needed by both chains, so it follows every library that uses it
    assert first.index("a_low") == len(first) - 1
    assert first.index("z_top") < first.index("m_mid") < first.index("a_low")
    assert first.index("b_sib") < first.index("a_low")


# Class order itself (own -I, dependency -isystem, system; lexical within a class) is pinned
# by tests/test_include_order.py; here the rows arrive in opposite orders.
COMPILE_FILES = {
    "main.c": "int main(void) { return 0; }\n",
    "dep.c": "int dep(void) { return 0; }\n",
}

PROPS = [
    '["app", "cc.include_dir", "src:own_b", "private"]',
    '["app", "cc.include_dir", "src:own_a", "private"]',
    '["dep", "cc.include_dir", "src:dep_y", "public"]',
    '["dep", "cc.include_dir", "src:dep_x", "public"]',
    '["dep", "cc.system_include_dir", "src:sys_a", "interface"]',
    '["app", "cc.define", "ZED=1", "private"]',
    '["app", "cc.define", "ALPHA=2", "private"]',
]


def compile_argv(tmp_path, props):
    spec = f"""
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
target = [["app"], ["dep"]]
native_kind = [["app", "executable"], ["dep", "library"]]
c_source = [["app", "main.c"], ["dep", "dep.c"]]
dep = [["app", "dep", "private"]]
prop = [
  {",\n  ".join(props)}
]
request = [["app", "rel"]]
"""
    proj, user = io.project(tmp_path, spec, COMPILE_FILES)
    build, metrics = io.configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    return io.compile_cmd(build, f"obj/{REL}/app/main.c.o")


@needs_souffle
def test_include_and_define_flags_do_not_depend_on_row_order(tmp_path):
    first = compile_argv(tmp_path / "one", PROPS)
    second = compile_argv(tmp_path / "two", list(reversed(PROPS)))
    assert (
        io.includes(first)
        == io.includes(second)
        == [
            ("-I", "own_a"),
            ("-I", "own_b"),
            ("-isystem", "dep_x"),
            ("-isystem", "dep_y"),
            ("-isystem", "sys_a"),
        ]
    )
    defines = lambda c: [w for w in shlex.split(c) if w[2:].split("=")[0] in ("ALPHA", "ZED")]
    assert defines(first) == defines(second) == ["-DALPHA=2", "-DZED=1"]
