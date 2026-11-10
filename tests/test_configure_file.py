# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configured files (core L1_configure_file.dl and `buildlog -E
configure_file`): a template's variables substituted from a target's
properties, CMake's configure_file dialect as far as projects here use it."""

from __future__ import annotations

import os
import re

import pytest

from buildlog.builtins import ConfigureError, configure_file
from tests.test_cpp_mechanisms import (
    cmd_of,
    configure,
    configure_fails,
    needs_souffle,
    project,
    run_ninja,
)

TEMPLATE = """\
prefix=@PREFIX@
libdir=${exec_prefix}/@LIBDIR@
#cmakedefine HAVE_A 1
#  cmakedefine\tHAVE_B yes @LIBDIR@
#cmakedefine HAVE_C
brace=${LIBDIR}
"""


def run(tmp_path, *args, template=TEMPLATE, pieces=None):
    src = tmp_path / "t.in"
    src.write_text(template)
    out = tmp_path / "out" / "t"
    configure_file("-o", str(out), *args, *(pieces or [f"--lines=1:0:{src}"]))
    return out


def test_dialect(tmp_path):
    out = run(
        tmp_path,
        "-DPREFIX=/p",
        "-DLIBDIR=lib",
        "-Dexec_prefix=/e",
        "-DHAVE_A=1",
        "-UHAVE_B",
        "-DHAVE_C=yes",
    )
    assert out.read_text() == (
        "prefix=/p\nlibdir=/e/lib\n#define HAVE_A 1\n/* #  undef HAVE_B */\n"
        "#define HAVE_C\nbrace=lib\n"
    )


def test_at_only_leaves_braces(tmp_path):
    out = run(
        tmp_path, "--at-only", "-DPREFIX=/p", "-DLIBDIR=lib", "-UHAVE_A", "-DHAVE_B=1", "-UHAVE_C"
    )
    assert out.read_text() == (
        "prefix=/p\nlibdir=${exec_prefix}/lib\n/* #undef HAVE_A */\n"
        "#  define\tHAVE_B yes lib\n/* #undef HAVE_C */\nbrace=${LIBDIR}\n"
    )


@pytest.mark.parametrize(
    ("args", "template", "message"),
    [
        # An unstated variable is an error, not CMake's empty string.
        (["-DA=1"], "x=@B@\n", "variable B is not given"),
        (["-UB"], "x=@B@\n", "variable B is undefined"),
        (["-DA=1"], "x=${B}\n", "variable B is not given"),
        (["-DA=1"], "#cmakedefine B\n", "B is neither defined nor undefined"),
        (["-DA=1"], "#cmakedefine01 A\n", "#cmakedefine01 is not supported"),
        # CMake reads these as false; the variable is stated undefined instead.
        (["-DA=OFF"], "#cmakedefine A\n", "A=OFF is false to CMake; state -UA"),
        (["-DA="], "#cmakedefine A 1\n", "A= is false to CMake"),
        (["-DA=x-NOTFOUND"], "#cmakedefine A\n", "is false to CMake"),
        (["-DA=1", "-UA"], "x\n", "defined and undefined: A"),
        (["-DA=1", "-DA=2"], "x\n", "conflicting definition -DA=2"),
    ],
)
def test_errors(tmp_path, args, template, message):
    with pytest.raises(ConfigureError, match=message):
        run(tmp_path, *args, template=template)


def test_pieces_and_restat(tmp_path):
    src = tmp_path / "t.in"
    src.write_text("1\n2\n3\n4\n")
    pieces = [f"--lines=1:2:{src}", "--text=#cmakedefine A 1", "--text=", f"--lines=3:0:{src}"]
    out = run(tmp_path, "-DA=1", template="1\n2\n3\n4\n", pieces=pieces)
    assert out.read_text() == "1\n2\n#define A 1\n\n3\n4\n"
    # Unchanged content leaves the file alone (the action is restat).
    os.utime(out, (1, 1))
    run(tmp_path, "-DA=1", template="1\n2\n3\n4\n", pieces=pieces)
    assert out.stat().st_mtime == 1
    run(tmp_path, "-UA", template="1\n2\n3\n4\n", pieces=pieces)
    assert out.stat().st_mtime != 1 and "/* #undef A */" in out.read_text()
    with pytest.raises(ConfigureError, match="lines 3..9 outside 1..4"):
        run(tmp_path, template="1\n2\n3\n4\n", pieces=[f"--lines=3:9:{src}"])


# ---- in a project: variables are properties, a header for consumers ----------

HEADER_SPEC = """
target = [["app"], ["config"]]
native_kind = [["app", "executable"]]
c_source = [["app", "main.c"]]
dep = [["app", "config", "private"]]
request = [["app", "rel"]]
configure_file = [["config", "config.h"]]
cc_configured_header = [["config", "config.h"]]
configure_file_lines = [["config", "config.h", 0, "config.h.in", 1, 0]]
configure_file_text = [["config", "config.h", 1, "#define ANSWER @ANSWER@"]]
prop = [
  ["config", "configure.define", "HAVE_STDIO_H=1", "private"],
  ["config", "configure.undefine", "HAVE_NOTHING", "private"],
  ["config", "configure.define", "ANSWER=42", "private"],
]
"""
FILES = {
    "config.h.in": "#cmakedefine HAVE_STDIO_H 1\n#cmakedefine HAVE_NOTHING 1\n",
    "main.c": '#include "config.h"\n#include <stdio.h>\n'
    "int main(void) { return HAVE_STDIO_H + ANSWER == 43 ? 0 : 1; }\n",
}


@needs_souffle
def test_a_configured_header_for_its_consumers(tmp_path):
    proj = project(tmp_path, HEADER_SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert re.search(r" -isystem gen/[^ /]+/config ", cmd_of(build, "app/main.c.o"))
    run_ninja(build)
    gen = next((build / "souffle").glob("gen/*/config/config.h"))
    assert gen.read_text() == (
        "#define HAVE_STDIO_H 1\n/* #undef HAVE_NOTHING */\n#define ANSWER 42\n"
    )


PROP = "\nprop = ["


@needs_souffle
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            '"config.h", 1, "#define',
            '"config.h", 0, "#define',
            "template piece position used twice",
        ),
        (
            '[["config", "config.h"]]\ncc',
            '[["config", "../x.h"]]\ncc',
            "configured file path is not relative",
        ),
        (
            'cc_configured_header = [["config"',
            'cc_configured_header = [["app"',
            "configured header is not a configured file",
        ),
        (
            PROP,
            '\ncc_check_configure = [["config", "HAVE_X"]]' + PROP,
            "configured variable from an unstated check",
        ),
        (
            PROP,
            '\nconfigure_file_at_only = [["config", "other.h"]]' + PROP,
            "at-only of an unstated configured file",
        ),
        (
            '"config", "config.h", 1, "#define',
            '"config", "none.h", 1, "#define',
            "template piece of an unstated configured file",
        ),
        (
            'configure_file_lines = [["config", "config.h", 0, "config.h.in", 1, 0]]\nconfigure_file_text = [["config", "config.h", 1, "#define ANSWER @ANSWER@"]]\n',
            "",
            "configured file has no template",
        ),
    ],
)
def test_configured_file_errors(tmp_path, capsys, old, new, message):
    assert old in HEADER_SPEC
    proj = project(tmp_path, HEADER_SPEC.replace(old, new, 1), FILES)
    assert message in configure_fails(proj, tmp_path, capsys)


# ---- an owner's configured file: one per configuration, shared by its targets --

OWNER_SPEC = """
target = [["app"], ["a"], ["b"]]
native_kind = [["app", "executable"], ["a", "library"], ["b", "library"]]
default_linkage = [["b", "shared"]]
c_source = [["app", "main.c"], ["a", "lib.c"], ["b", "lib.c"]]
dep = [["app", "a", "private"]]
request = [["app", "rel"], ["b", "rel"]]
owner_member = [["cfg", "a"], ["cfg", "b"]]
configure_file = [["cfg", "config.h"]]
cc_configured_header = [["cfg", "config.h"]]
configure_file_lines = [["cfg", "config.h", 0, "config.h.in", 1, 0]]
configure_file_text = [["cfg", "config.h", 1, "#define ANSWER @ANSWER@"]]
prop = [
  ["a", "configure.define", "HAVE_STDIO_H=1", "private"],
  ["a", "configure.undefine", "HAVE_NOTHING", "private"],
  ["a", "configure.define", "ANSWER=42", "private"],
  ["b", "configure.define", "HAVE_STDIO_H=1", "private"],
  ["b", "configure.undefine", "HAVE_NOTHING", "private"],
  ["b", "configure.define", "ANSWER=42", "private"],
]
"""
OWNER_FILES = {
    **FILES,
    "lib.c": '#include "config.h"\nint lib(void) { return ANSWER; }\n',
}


@needs_souffle
def test_an_owners_configured_header_is_each_members_own(tmp_path):
    proj = project(tmp_path, OWNER_SPEC, OWNER_FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    text = (build / "souffle" / "build.ninja").read_text()
    # One file per configuration, under the owner's name.
    assert len(re.findall(r"^build gen/[^ /]+/cfg/config\.h\b", text, re.MULTILINE)) == 1
    # The members' own dir (-I), with their compiles waiting for the file;
    # the consumer's dependency dir (-isystem).
    for obj in ("a/lib.c.o", "b/lib.c.o"):
        assert re.search(r" -Igen/[^ /]+/cfg ", cmd_of(build, obj)), obj
    assert re.search(r" -isystem gen/[^ /]+/cfg ", cmd_of(build, "app/main.c.o"))
    assert re.search(
        r"^build obj/[^ ]+/a/lib\.c\.o: .*\|\| .*gen/[^ /]+/cfg/config\.h", text, re.MULTILINE
    )
    run_ninja(build)


@needs_souffle
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            '  ["b", "configure.define", "ANSWER=42", "private"],\n',
            '  ["b", "configure.define", "ANSWER=43", "private"],\n',
            "owner members disagree on a configured file's variable",
        ),
        (
            'owner_member = [["cfg", "a"], ["cfg", "b"]]',
            'owner_member = [["cfg", "a"], ["cfg", "b"], ["cfg", "nobody"]]',
            "owner member is not a target",
        ),
        (
            'owner_member = [["cfg", "a"], ["cfg", "b"]]',
            "",
            "configured file owner has no targets",
        ),
    ],
)
def test_owner_errors(tmp_path, capsys, old, new, message):
    assert old in OWNER_SPEC
    proj = project(tmp_path, OWNER_SPEC.replace(old, new, 1), OWNER_FILES)
    assert message in configure_fails(proj, tmp_path, capsys)


@needs_souffle
def test_owner_named_after_a_member_is_harmless(tmp_path):
    spec = OWNER_SPEC.replace('["cfg", "a"], ["cfg", "b"]]', '["a", "a"], ["a", "b"]]', 1)
    spec = spec.replace('"cfg"', '"a"')
    proj = project(tmp_path, spec, OWNER_FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert re.search(
        r"^build gen/[^ /]+/a/config\.h\b",
        (build / "souffle" / "build.ninja").read_text(),
        re.MULTILINE,
    )


@needs_souffle
@pytest.mark.parametrize(
    ("group", "message"),
    [
        # The owner `a` names a target but groups only b.
        ('[["a", "b"]]', "configured file owner is also a target outside its own members"),
        # An imported target cannot own a configured file.
        ('[["a", "a"], ["a", "b"]]', "configured file owner is an imported target"),
    ],
)
def test_owner_name_clashing_with_a_target_id(tmp_path, capsys, group, message):
    spec = OWNER_SPEC.replace(
        'owner_member = [["cfg", "a"], ["cfg", "b"]]', f"owner_member = {group}"
    )
    spec = spec.replace('"cfg"', '"a"')
    if "imported" in message:
        spec = spec.replace(
            'target = [["app"], ["a"], ["b"]]', 'target = [["app"], ["b"]]\nimported = [["a"]]'
        )
    proj = project(tmp_path, spec, OWNER_FILES)
    assert message in configure_fails(proj, tmp_path, capsys)
