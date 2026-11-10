# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""cc.family and cxx.family: the family (clang, gcc) of the C and the C++
toolchain a configuration selects, as configuration attributes (buildlog-cpp
L2_toolchains.dl), CMake's $<C_COMPILER_ID:...> and $<CXX_COMPILER_ID:...>.
Conditions that decide what is built may not test them."""

from __future__ import annotations

import pytest

from buildlog.engine import configure_project

from . import host
from .test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

pytestmark = [pytest.mark.integration, needs_souffle]

SOURCES = {"main.cpp": "int main() { return 0; }\n", "util.c": "int util(void) { return 0; }\n"}
REQUEST = 'request = [["app", "rel"]]\n'
APP = """
[target.app]
kind = "executable"
sources = ["main.cpp", "util.c"]
"""
OTHER = "gcc" if host.FAMILY == "clang" else "clang"
F = host.FAMILY


def test_a_property_follows_the_family_of_the_selected_toolchain(tmp_path):
    proj = project(
        tmp_path,
        REQUEST
        + APP
        + f"""when."cxx.family == {F}".private."cc.define" = "CXX_IS_{F.upper()}"
when."cxx.family == {OTHER}".private."cc.define" = "CXX_IS_{OTHER.upper()}"
when."cc.family == {F}".private."cc.define" = "C_IS_{F.upper()}"
when."!(cc.family == {F})".private."cc.define" = "C_IS_NOT_{F.upper()}"
""",
        SOURCES,
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cxx = cmd_of(build, "app/main.cpp.o")
    assert f"-DCXX_IS_{F.upper()}" in cxx
    assert f"-DCXX_IS_{OTHER.upper()}" not in cxx
    c = cmd_of(build, "app/util.c.o")
    assert f"-DC_IS_{F.upper()}" in c
    assert "C_IS_NOT" not in c


def test_the_family_is_a_condition_like_any_other(tmp_path):
    proj = project(
        tmp_path,
        f"""
condition = [["is_{F}", "$Eq(cxx.family, {F})"]]
prop_when = [["app", "cc.define", "FAMILY_{F.upper()}", "private", "is_{F}"]]
"""
        + REQUEST
        + APP,
        SOURCES,
    )
    build, _ = configure(proj, tmp_path)
    assert f"-DFAMILY_{F.upper()}" in cmd_of(build, "app/main.cpp.o")


def test_a_condition_that_decides_what_is_built_may_not_test_the_family(tmp_path, capsys):
    proj = project(
        tmp_path,
        REQUEST
        + f"""request_when = [["extra", "cxx.family == {F}"]]
"""
        + APP
        + """
[target.extra]
kind = "library"
sources = ["util.c"]
""",
        SOURCES,
    )
    with pytest.raises(BaseException) as exc:
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    text = str(getattr(exc.value, "errors", "")) + capsys.readouterr().err
    print(text)
    assert "cxx.family" in text


def test_an_edge_may_depend_on_the_family(tmp_path):
    proj = project(
        tmp_path,
        REQUEST
        + f"""dep_when = [["app", "extra", "private", "cxx.family == {F}"], ["app", "other", "private", "cxx.family == {OTHER}"]]
"""
        + APP
        + """
[target.extra]
kind = "library"
sources = ["util2.c"]

[target.other]
kind = "library"
sources = ["util3.c"]
""",
        {
            **SOURCES,
            "util2.c": "int u2(void) { return 0; }\n",
            "util3.c": "int u3(void) { return 0; }\n",
        },
    )
    build, _ = configure(proj, tmp_path)
    text = (build / "souffle" / "build.ninja").read_text()
    assert "extra/util2.c.o" in text
    assert "other/util3.c.o" not in text


def test_a_check_may_state_its_arguments_per_family(tmp_path):
    """cc_check_args_family: the check's arguments for a toolchain family replace
    its own where it states them (a flag one family rejects, like Clang's
    -Werror=unguarded-availability under GCC)."""
    bad = "-Werror -fthis-flag-does-not-exist"
    proj = project(
        tmp_path,
        f"""
cc_check_flag = [["HAS_PLAIN", "{bad}"], ["HAS_OWN", "{bad}"], ["HAS_OTHERS", "{bad}"]]
cc_check_args_family = [["HAS_OWN", "{F}", ""], ["HAS_OTHERS", "{OTHER}", ""]]
prop_when = [
  ["app", "cc.define", "HAS_PLAIN", "private", "cc.check.HAS_PLAIN"],
  ["app", "cc.define", "HAS_OWN", "private", "cc.check.HAS_OWN"],
  ["app", "cc.define", "HAS_OTHERS", "private", "cc.check.HAS_OTHERS"],
]
"""
        + REQUEST
        + APP,
        SOURCES,
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cxx = cmd_of(build, "app/main.cpp.o")
    assert "-DHAS_OWN" in cxx
    assert "-DHAS_PLAIN" not in cxx
    assert "-DHAS_OTHERS" not in cxx
