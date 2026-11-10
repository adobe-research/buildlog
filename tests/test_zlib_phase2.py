# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: libz.1.3.2.dylib and the programs linked to it.

zlib, the library's shared flavour (its author default, in the release
configuration), is named by its output name and versioned by native_version ("1.3.2" with SOVERSION "1").
Compared with the CMake baseline (case-studies/zlib/baseline): the file name
and its symlinks, the install name, the Mach-O compatibility and current
versions (compat from the SOVERSION, current from the VERSION), the programs'
load commands, and the exported symbols (`nm -gU`). On Linux the same
comparisons with ELF's: libz.so.1.3.2, its soname and the programs' NEEDED
entries (`readelf -d`), and the dynamic symbols (`nm -D`).
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests import host
from tests.zlib_case import (
    BASELINE,
    CHECK_DEFINES,
    CMAKE_TARGET_FLAGS,
    OUR_TARGET_FLAGS,
    REL,
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

SHARED = ("zlib", "zlib_example", "minigzip")
LIBZ = host.shlib("z", "1.3.2")
METADATA = "dylib-metadata.txt" if host.MACOS else "elf-metadata.txt"


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib") / "build"
    configure(build)
    ninja(build, f"lib/{REL}/{host.shlib('z')}", f"bin/{REL}/zlib_example", f"bin/{REL}/minigzip")
    return build


def _sections(name: str) -> dict[str, list[str]]:
    out, cur = {}, None
    for line in (BASELINE / name).read_text().splitlines():
        if line.startswith("===== "):
            cur = out.setdefault(line[6:], [])
        else:
            cur.append(line)
    return out


def _symbols(lines) -> set[str]:
    return {line.split()[-1] for line in lines if line.split()}


def _otool_l(path) -> list[str]:
    return subprocess.check_output(["otool", "-L", str(path)], text=True).splitlines()[1:]


def _loads(path) -> list[str]:
    """What a binary loads: otool -L, or readelf -d's NEEDED and SONAME
    (not RUNPATH: CMake's is the absolute build dir, buildlog's $ORIGIN)."""
    if host.MACOS:
        return _otool_l(path)
    out = subprocess.check_output(["readelf", "-d", str(path)], text=True)
    return [
        " ".join(line.split()[1:])
        for line in out.splitlines()
        if "(NEEDED)" in line or "(SONAME)" in line
    ]


def _base_loads(section: str) -> list[str]:
    lines = _sections(METADATA)[section]
    if host.MACOS:
        return lines
    # GCC's aarch64 programs and libraries also name the loader (its stack
    # protector guard); clang's do not.
    skip = ("PATH)", "[ld-linux") if host.FAMILY != "gcc" else ("PATH)",)
    return [" ".join(x.split()) for x in lines if not any(s in x for s in skip)]


def test_shared_compiles_match_the_baseline(build):
    ours = {k: v for k, v in compiles(commands(build)).items() if k[0] in SHARED}
    theirs = {k: v for k, v in compiles(baseline_commands()).items() if k[0] in SHARED}
    assert set(ours) == set(theirs) and len(ours) == 17
    for key, cmake in theirs.items():
        mine = ours[key]
        # ZLIB_DLL (CMake's DEFINE_SYMBOL) on the library's objects only.
        assert ("ZLIB_DLL" in mine["defines"]) == (key[0] == "zlib"), key
        assert mine["defines"] - CHECK_DEFINES == cmake["defines"] - CHECK_DEFINES, key
        assert cmake["includes"] == [("-I", "GEN"), ("-I", "SRC")], key
        flag = "-I" if key[0] == "zlib" else "-isystem"
        assert mine["includes"] == [(flag, "GEN"), (flag, "SRC")], key
        # -fPIC on the library's objects, in both.
        pic = {"-fPIC"} if key[0] == "zlib" else set()
        assert cmake["other"] == {"-O3"} | CMAKE_TARGET_FLAGS | pic, key
        assert {w for w in mine["other"] if not w.startswith("-isysroot")} == {
            "-O3"
        } | OUR_TARGET_FLAGS | pic, key


def test_dylib_name_symlinks_install_name_and_versions(build):
    lib = build / "souffle" / "lib" / REL
    base = _sections(METADATA)
    for line in base["links"]:
        name, target = line.split(" -> ")
        assert os.readlink(lib / name) == target, name
    assert (lib / LIBZ).is_file() and not (lib / LIBZ).is_symlink()
    ours = links(commands(build))[LIBZ]
    theirs = links(baseline_commands())[LIBZ]
    if host.MACOS:
        # Install name and both versions, and the libSystem-only dependency.
        assert _otool_l(lib / LIBZ) == base[f"otool -L {LIBZ}"]
        for flag in ("-compatibility_version", "-current_version", "-install_name"):
            assert ours[ours.index(flag) + 1] == theirs[theirs.index(flag) + 1], flag
    else:
        # The soname and the libc-only dependency; the same -soname word.
        assert _loads(lib / LIBZ) == _base_loads(f"readelf -d {LIBZ}")
        assert "-Wl,-soname,libz.so.1" in ours and "-Wl,-soname,libz.so.1" in theirs
        # Upstream's -Wl,--version-script,zlib.map, in buildlog's spelling.
        assert any(w.startswith("-Wl,--version-script=") and w.endswith("zlib.map") for w in ours)
    assert ours[0].endswith(f"/{host.CC}") and theirs[0] == "/usr/bin/cc"


def test_programs_load_the_dylib_like_the_baseline(build):
    tool = "otool -L" if host.MACOS else "readelf -d"
    bin_dir = build / "souffle" / "bin" / REL
    for exe in ("zlib_example", "minigzip"):
        ninja(build, f"bin/{REL}/{exe}")
        assert _loads(bin_dir / exe) == _base_loads(f"{tool} test/{exe}"), exe
    # CMake's static twins are the same programs in release-static.
    for exe, twin in (("zlib_example", "zlib_static_example"), ("minigzip", "static_minigzip")):
        ninja(build, f"bin/{STATIC}/{exe}")
        got = _loads(build / "souffle" / "bin" / STATIC / exe)
        assert got == _base_loads(f"{tool} test/{twin}")


def _nm(path, archive=False) -> set[str]:
    """Global defined symbols: nm -gU; on ELF nm -D (-g for an archive),
    without the version nodes (type A)."""
    if host.MACOS:
        argv = ["nm", "-gU", str(path)]
    else:
        argv = ["nm", "-g" if archive else "-D", "--defined-only", str(path)]
    lines = subprocess.check_output(argv, text=True).splitlines()
    return _symbols(x for x in lines if not x.endswith(":") and " A " not in x)


def test_exported_symbols(build):
    base = _sections("exported-symbols.txt")
    lib = build / "souffle" / "lib" / REL
    ninja(build, f"lib/{STATIC}/libz.a")
    ours = _nm(lib / LIBZ)
    theirs = _symbols(x for x in base[LIBZ] if " A " not in x)
    # macOS: HAVE_HIDDEN makes ZLIB_INTERNAL hide the internal functions. Linux:
    # GCC answers no to it (as CMake's check does) and zlib.map, stated as
    # native_version_script, limits the exports to upstream's.
    assert ours == theirs
    # The archive: the same global symbols (member order differs: the ledger).
    archive = build / "souffle" / "lib" / STATIC / "libz.a"
    assert _nm(archive, archive=True) == _symbols(x for x in base["libz.a"] if not x.endswith(":"))


def test_shared_programs_run(build, tmp_path):
    bin_dir = build / "souffle" / "bin" / REL
    out = subprocess.run(
        [str(bin_dir / "zlib_example")], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert re.search(r"zlib version 1\.3\.2 = 0x1320", out.stdout), out.stdout
