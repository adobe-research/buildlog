# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Source groups (buildlog-cpp): a named set of a
target's sources with its own condition and compile properties. The
objects stay the target's and compile with its properties plus the group's;
nothing is copied into a separate target."""

from __future__ import annotations

import subprocess

import pytest

from buildlog.cli import main
from tests.test_cpp_mechanisms import (
    cmd_of,
    configure,
    configure_fails,
    needs_souffle,
    ninja_text,
    project,
)

REL = "arm64-macos.release.none"

FILES = {
    "common.c": "int fast(void);\nint common(void) { return fast() + BASE; }\n",
    "fast.c": "int fast(void) { return FAST * 10; }\n",
    "slow.c": "#error the off group must not compile\n",
    "extra.cpp": 'extern "C" int extra() { return EXTRA; }\n',
    "main.c": '#include <stdio.h>\nint common(void);\nint extra(void);\nint main(void) { printf("%d\\n", common() + extra()); }\n',
}

SPEC = """
request = [["app", "rel"]]
condition = [["is_rel", "$Eq(native.opt, release)"], ["is_debug", "$Eq(native.opt, debug)"]]

[target.lib]
kind = "library"
sources = ["common.c", "fast.c", "slow.c", "extra.cpp"]
private = { "cc.define" = ["BASE=1"], "cc.compile_flag" = ["-Wall"] }
source_group = ["simd", "legacy", "cxx"]
group_source = [["simd", "fast.c"], ["legacy", "slow.c"], ["legacy", "fast.c"], ["cxx", "extra.cpp"]]
group_when = [["simd", "is_rel"], ["legacy", "is_debug"]]
group_prop = [["simd", "cc.define", "FAST=4"], ["legacy", "cc.define", "LEGACY"], ["simd", "cc.compile_flag", "-Wextra"],
  ["cxx", "cxx.compile_flag", "-fno-rtti"], ["cxx", "cc.compile_flag", "-Wshadow"]]
group_prop_when = [["cxx", "cc.define", "EXTRA=2", "is_rel"], ["cxx", "cc.define", "EXTRA=3", "is_debug"]]

[target.app]
kind = "executable"
sources = ["main.c"]
deps = { private = ["lib"] }
"""


@needs_souffle
def test_groups_gate_sources_and_add_properties_to_their_objects_only(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    common = cmd_of(build, f"obj/{REL}/lib/common.c.o")
    fast = cmd_of(build, f"obj/{REL}/lib/fast.c.o")
    extra = cmd_of(build, f"obj/{REL}/lib/extra.cpp.o")
    # The target's own properties reach every object; the group's only its own,
    # after the target's.
    assert "-DBASE=1" in common and "-Wall" in common
    assert "-DFAST=4" not in common and "-Wextra" not in common
    assert (
        "-DBASE=1" in fast
        and "-Wall -Wextra" in fast
        and fast.index("-DBASE=1") < fast.index("-DFAST=4")
    )
    # fast.c is also in legacy, which does not apply: simd's properties only.
    assert "-DLEGACY" not in fast
    assert (
        "-fno-rtti" in extra
        and "-Wshadow" in extra
        and "-DEXTRA=2" in extra
        and "-DEXTRA=3" not in extra
    )
    # The legacy group's condition fails in release: slow.c is not compiled,
    # and the objects are lib's (no separate target).
    text = ninja_text(build)
    assert "slow.c" not in text
    assert f"obj/{REL}/lib/fast.c.o" in cmd_of(build, f"lib/{REL}/liblib.a")
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    out = subprocess.check_output([str(build / "souffle" / "bin" / REL / "app")], text=True)
    assert out == "43\n"  # fast()=40, BASE=1, EXTRA=2


CFG = '["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]]'


@needs_souffle
def test_why_names_the_group_and_its_condition(tmp_path, capsys):
    proj = project(tmp_path, SPEC, FILES)
    build, _ = configure(proj, tmp_path)
    capsys.readouterr()
    assert main(["why", "-C", str(build), f'src_on("lib", {CFG}, "fast.c")', str(proj)]) == 0
    out = capsys.readouterr().out
    assert 'group_on("lib"' in out and '"simd")' in out and '"is_rel"' in out


@needs_souffle
@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (('["simd", "fast.c"]', '["simd", "nope.c"]'), "group source is not a C or C++ source"),
        (('["legacy", "is_debug"]', '["legacy", "is_nothing"]'), "unknown condition"),
        (
            ('group_when = [["simd"', 'group_when = [["nope"'),
            "group setting for an undeclared source group",
        ),
        (
            ('["simd", "cc.define", "FAST=4"]', '["simd", "cc.include_dir", "x"]'),
            "not a per-object compile key",
        ),
        (
            ('["legacy", "is_debug"]]', '["legacy", "is_debug"], ["legacy", "is_rel"]]'),
            "two conditions",
        ),
    ],
)
def test_source_group_errors(tmp_path, capsys, edit, message):
    old, new = edit
    assert old in SPEC
    proj = project(tmp_path, SPEC.replace(old, new, 1), FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert message in err, err
