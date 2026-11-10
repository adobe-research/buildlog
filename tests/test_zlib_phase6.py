# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: install and export.

zlib's one library is installed and exported as the ZLIB package in the
configuration's linkage: release installs libz.1.3.2.dylib and its links,
release-static libz.a, each with zlib.h and the configuration's zconf.h, and
CPS and CMake package files. The ground truth is CMake's install
(baseline/dylib-metadata.txt, baseline/zconf.h) and upstream's own consumer,
test/find_package_no_components_test.cmake.in, which finds ZLIB and links
ZLIB::ZLIB. The compatibility claim is derived from native_version (1.3.2,
soversion 1: same_major, CPS compat_version 1.0.0), where upstream states
AnyNewerVersion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from buildlog.engine import configure_project
from tests import host
from tests.zlib_case import BASELINE, PRESET, PROJECT, SRC, needs_zlib

pytestmark = needs_zlib
LIBZ, SO, DEV = host.shlib("z", "1.3.2"), host.shlib("z", "1"), host.shlib("z")
needs_cmake = pytest.mark.skipif(shutil.which("cmake") is None, reason="cmake not installed")


def install(tmp_path_factory, config: str):
    root = tmp_path_factory.mktemp(f"zlib-{config}")
    prefix = root / "prefix"
    user = root / "prefix.toml"
    user.write_text(f'install_prefix = "{prefix}"\n')
    build = root / "build"
    _, metrics = configure_project(PROJECT, build, user_files=[PRESET, user], configs=[config])
    assert metrics["errors"] == []
    subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "install"], check=True, capture_output=True
    )
    return prefix


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    return install(tmp_path_factory, "release")


@pytest.fixture(scope="module")
def static(tmp_path_factory):
    return install(tmp_path_factory, "release-static")


def tree(prefix):
    return sorted(str(p.relative_to(prefix)) for p in prefix.rglob("*") if not p.is_dir())


def test_the_shared_flavour_installs_what_cmake_installs(shared):
    assert tree(shared) == [
        "include/zconf.h",
        "include/zlib.h",
        "lib/cmake/ZLIB/ZLIBConfig.cmake",
        "lib/cmake/ZLIB/ZLIBConfigVersion.cmake",
        "lib/cmake/ZLIB/ZLIBTargets.cmake",
        "lib/cps/ZLIB.cps",
        *sorted(f"lib/{f}" for f in (LIBZ, SO, DEV)),
        "share/buildlog/ZLIB/ZLIB.facts.dl",
        "share/buildlog/ZLIB/manifest.toml",
    ]
    lib = shared / "lib"
    meta = (BASELINE / ("dylib-metadata.txt" if host.MACOS else "elf-metadata.txt")).read_text()
    for link in (SO, DEV):
        assert f"{link} -> {os.readlink(lib / link)}" in meta
    if host.MACOS:
        argv = ["otool", "-L", str(lib / LIBZ)]
        want = "@rpath/libz.1.dylib (compatibility version 1.0.0, current version 1.3.2)"
    else:
        argv = ["readelf", "-d", str(lib / LIBZ)]
        want = "Library soname: [libz.so.1]"
    got = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    assert want in got and want in meta
    assert (shared / "include" / "zconf.h").read_bytes() == (BASELINE / "zconf.h").read_bytes()
    assert (shared / "include" / "zlib.h").read_bytes() == (SRC / "zlib.h").read_bytes()


def test_the_static_flavour_installs_the_archive(static):
    assert [f for f in tree(static) if f.startswith("lib/lib")] == ["lib/libz.a"]
    assert (static / "include" / "zconf.h").read_bytes() == (BASELINE / "zconf.h").read_bytes()


def test_the_compatibility_claim_is_derived_from_the_soversion(shared, static):
    for prefix, kind, loc in ((shared, "dylib", LIBZ), (static, "archive", "libz.a")):
        cps = json.loads((prefix / "lib" / "cps" / "ZLIB.cps").read_text())
        assert cps["version"] == "1.3.2" and cps["compat_version"] == "1.0.0"
        assert cps["components"]["ZLIB"]["type"] == kind
        assert cps["components"]["ZLIB"]["location"] == f"@prefix@/lib/{loc}"
        version = (prefix / "lib" / "cmake" / "ZLIB" / "ZLIBConfigVersion.cmake").read_text()
        assert "Compatibility: same_major." in version
        assert 'PACKAGE_FIND_VERSION_MAJOR STREQUAL "1"' in version


def consumer(tmp_path, prefix, want="1.3.2"):
    """Upstream's find_package_no_components_test, shared part on (it links
    ZLIB::ZLIB, whatever the installed flavour)."""
    text = (SRC / "test" / "find_package_no_components_test.cmake.in").read_text()
    text = text.replace("@zlib_VERSION@", want).replace("@zlib_SOURCE_DIR@", str(SRC))
    text = text.replace("@ZLIB_BUILD_SHARED@", "ON").replace("@ZLIB_BUILD_STATIC@", "OFF")
    src = tmp_path / "src"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(text)
    build = tmp_path / "build"
    return subprocess.run(
        ["cmake", "-S", str(src), "-B", str(build), f"-DCMAKE_PREFIX_PATH={prefix}", "-G", "Ninja"],
        capture_output=True,
        text=True,
        check=False,
    ), build


@needs_cmake
@pytest.mark.parametrize("flavour", ["shared", "static"])
def test_upstreams_find_package_consumer_builds_and_passes(tmp_path, request, flavour):
    prefix = request.getfixturevalue(flavour)
    configured, build = consumer(tmp_path, prefix)
    assert configured.returncode == 0, configured.stdout + configured.stderr
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
    ctest = subprocess.run(
        ["ctest", "--test-dir", str(build)], capture_output=True, text=True, check=False
    )
    assert ctest.returncode == 0 and "100% tests passed" in ctest.stdout, ctest.stdout
    loads = host.needed_libs(str(build / "test_example"))
    assert (("@rpath/libz.1.dylib" if host.MACOS else "[libz.so.1]") in loads) == (
        flavour == "shared"
    )


@needs_cmake
def test_a_consumer_asking_for_the_next_major_is_refused(tmp_path, shared):
    configured, _ = consumer(tmp_path, shared, want="2.0")
    assert configured.returncode != 0
    # CMake 4's wording, and 3.28's (Ubuntu 24.04).
    assert re.search(
        r"not compatible with the version requested|that is compatible with requested version",
        " ".join(configured.stderr.split()),
    ), configured.stderr
