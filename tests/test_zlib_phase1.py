# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: libz.a and the programs linked to it.

zlib is one library whose linkage is chosen; its static flavour is the
release-static configuration (native.linkage = static), named libz.a by its
output name, and CMake's static twins (zlibstatic, zlib_static_example,
static_minigzip) are zlib, zlib_example and minigzip there. Compile lines are compared with the CMake baseline
(case-studies/zlib/baseline/commands.txt): the same sources, defines and
include-dir order per object; the remaining flag differences are the ones
the README's ledger explains.
"""

from __future__ import annotations

import subprocess

import pytest

from tests import host
from tests.zlib_case import (
    CHECK_DEFINES,
    CMAKE_TARGET_FLAGS,
    OUR_TARGET_FLAGS,
    STATIC,
    baseline_commands,
    commands,
    compiles,
    configure,
    links,
    needs_zlib,
    ninja,
)

pytestmark = needs_zlib

TWINS = ("zlibstatic", "zlib_static_example", "static_minigzip")


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib") / "build"
    configure(build)
    ninja(build, f"lib/{STATIC}/libz.a", f"bin/{STATIC}/zlib_example", f"bin/{STATIC}/minigzip")
    return build


def test_static_compiles_match_the_baseline(build):
    ours = {k: v for k, v in compiles(commands(build)).items() if k[0] in TWINS}
    theirs = {k: v for k, v in compiles(baseline_commands()).items() if k[0] in TWINS}
    # The same objects: 15 library sources and one per program.
    assert set(ours) == set(theirs) and len(ours) == 17
    for key, cmake in theirs.items():
        mine = ours[key]
        # Defines, except what a configure check decides (see the configure-check tests).
        assert mine["defines"] - CHECK_DEFINES == cmake["defines"] - CHECK_DEFINES, key
        # <generated-header dir> <source dir>, in that order. CMake: -I
        # everywhere. buildlog: the library's own dirs (-I) in its objects,
        # a dependency's (-isystem) in the programs'.
        assert cmake["includes"] == [("-I", "GEN"), ("-I", "SRC")], key
        flag = "-I" if key[0] == "zlibstatic" else "-isystem"
        assert mine["includes"] == [(flag, "GEN"), (flag, "SRC")], key
        # The ledger's residual difference: how the target is spelled; both
        # optimize with -O3 and put no -fPIC on static objects.
        assert cmake["other"] == {"-O3"} | CMAKE_TARGET_FLAGS, key
        assert {w for w in mine["other"] if not w.startswith("-isysroot")} == {
            "-O3"
        } | OUR_TARGET_FLAGS, key


def test_archive_is_libz_a_with_the_fifteen_objects(build):
    ar = next(c for c in commands(build) if c.split()[0].endswith(("/ar", "-ar")))
    words = ar.split()
    assert words[1:3] == ["rcs", f"lib/{STATIC}/libz.a"]
    assert sorted(w.rsplit("/", 1)[1] for w in words[3:]) == sorted(
        f"{s}.o"
        for s in [
            *("adler32.c", "compress.c", "crc32.c", "deflate.c", "gzclose.c", "gzlib.c"),
            *("gzread.c", "gzwrite.c", "inflate.c", "infback.c", "inftrees.c", "inffast.c"),
            *("trees.c", "uncompr.c", "zutil.c"),
        ]
    )


def test_c_programs_link_with_the_c_driver(build):
    ours, theirs = links(commands(build)), links(baseline_commands())
    for exe, ours_exe in (("zlib_static_example", "zlib_example"), ("static_minigzip", "minigzip")):
        assert theirs[exe][0] == "/usr/bin/cc"
        assert ours[exe][0].endswith(f"/{host.CC}"), ours[exe][0]
        assert ours[exe][-1] == f"lib/{STATIC}/libz.a" and theirs[exe][-1] == "libz.a"
        deps = host.needed_libs(str(build / "souffle" / "bin" / STATIC / ours_exe))
        # No C++ runtime: CMake's baseline loads the C library only.
        assert host.CXX_RUNTIME not in deps
        assert ("libSystem" if host.MACOS else "libc.so") in deps


def test_static_programs_run(build, tmp_path):
    bin_dir = build / "souffle" / "bin" / STATIC
    out = subprocess.run(
        [str(bin_dir / "zlib_example")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "zlib version 1.3.2" in out.stdout
    gz = subprocess.run(
        [str(bin_dir / "minigzip")], input=b"hello zlib\n", capture_output=True, check=True
    ).stdout
    back = subprocess.run(
        [str(bin_dir / "minigzip"), "-d"], input=gz, capture_output=True, check=True
    ).stdout
    assert back == b"hello zlib\n"
