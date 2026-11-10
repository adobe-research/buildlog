# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: the generated zconf.h and zlib.pc.

Both are core configured files (L1_configure_file.dl): templates whose
variables are properties of the target that generates them, substituted by
`buildlog -E configure_file`. zconf.h's HAVE_STDARG_H and HAVE_UNISTD_H are
the configure checks' answers (buildlog-cpp cc_check_configure); zlib.pc's
version is the library's. The acceptance check is a byte comparison with the
files CMake generated (baseline/zconf.h, baseline/zlib.pc).
"""

from __future__ import annotations

import subprocess

import pytest

from tests.zlib_case import (
    BASELINE,
    PROJECT,
    REL,
    STATIC,
    commands,
    compiles,
    configure,
    needs_zlib,
    ninja,
)

pytestmark = needs_zlib

PREFIX = "/opt/zlib-case"
GEN = f"gen/{REL}"


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib") / "build"
    configure(build, var_overrides={"INSTALL_PREFIX": PREFIX})
    ninja(build)
    return build


def test_zconf_h_is_cmakes(build):
    ours = (build / "souffle" / GEN / "zlib" / "zconf.h").read_bytes()
    assert ours == (BASELINE / "zconf.h").read_bytes()


def test_zlib_pc_is_cmakes(build):
    # The baseline replaced CMake's install prefix with $PREFIX.
    ours = (build / "souffle" / GEN / "zlib_pc" / "zlib.pc").read_text()
    assert ours == (BASELINE / "zlib.pc").read_text().replace("$PREFIX", PREFIX)


def test_the_generated_dir_comes_first_where_cmake_puts_it(build):
    # target_include_directories(PUBLIC ${zlib_BINARY_DIR} ${zlib_SOURCE_DIR}):
    # the configured zconf.h is found before the source tree's.
    # One zconf.h per configuration, the library's: its dir is the library's
    # own (-I) and the programs' dependency dir (-isystem).
    ours = compiles(commands(build))
    for key, flag, cfg, target in (
        (("zlib", "adler32.c"), "-I", REL, "zlib"),
        (("zlibstatic", "adler32.c"), "-I", STATIC, "zlib"),
        (("zlib_example", "test/example.c"), "-isystem", REL, "zlib_example"),
    ):
        assert ours[key]["includes"] == [(flag, "GEN"), (flag, "SRC")], key
        sep = "" if flag == "-I" else " "
        assert f"{flag}{sep}gen/{cfg}/zlib " in " ".join(
            c for c in commands(build) if f"obj/{cfg}/{target}/" in c and key[1] in c
        )
    assert sum("configure_file -o" in c and "zconf.h" in c for c in commands(build)) == 2


def _why(build, flag, output):
    return subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "why",
            "-C",
            str(build),
            "--flag",
            flag,
            "--output",
            output,
            "--leaves",
            str(PROJECT),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_why_have_unistd_h_leads_to_the_check(build):
    out = _why(build, "-DHAVE_UNISTD_H=1", f"{GEN}/zlib/zconf.h")
    probe = [ln for ln in out.splitlines() if ln.strip().startswith("res_cc_check(")]
    assert len(probe) == 1 and ":7:include:8:unistd.h:" in probe[0] and '"yes")' in probe[0], out
    assert 'cc_check_configure("zlib", "HAVE_UNISTD_H")' in out, out


def test_why_the_version_leads_to_the_library(build):
    out = _why(build, "-Dzlib_VERSION=1.3.2", f"{GEN}/zlib_pc/zlib.pc")
    assert 'native_version("zlib", "1.3.2", "1")' in out, out


def test_why_the_generated_dir_leads_to_the_librarys_configured_file(build):
    # The dir is the library's own -I, from its configured header; the
    # order is stated (rules.dl's cc_include_before).
    gen = f"gen/{STATIC}"
    flag, obj = f"-I{gen}/zlib", f"obj/{STATIC}/zlib/adler32.c.o"
    out = _why(build, flag, obj)
    assert 'configure_file("zlib", "zconf.h")' in out, out
    assert 'cc_configured_header("zlib", "zconf.h")' in out, out
    full = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--flag", flag,
         "--output", obj, str(PROJECT)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert f'action_seq_before("out:{obj}", 30, "out:{gen}/zlib", "src:")' in full, full
    assert "cc_include_before" in full


def test_why_the_archive_names_the_level_that_chose_static(build):
    # The static flavour is the configuration's native.linkage (the user's
    # global default), above the author's default_linkage shared.
    out = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--output",
         f"lib/{STATIC}/libz.a", str(PROJECT)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert f'library_linkage_by("zlib", ‹{STATIC}›, "static", "user global")' in out, out
    assert 'config_decl_setting("release-static", "native.linkage", "static")' in out, out
