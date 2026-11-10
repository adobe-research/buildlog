# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The machine the suite runs on, for tests whose expectations are the
platform's conventions (file names, install dirs) rather than buildlog's."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

# examples/cross and the universal2 configurations of the case studies
CROSS_REASON = (
    "macOS only: builds x86_64-macos and universal2-macos slices (lipo, Rosetta), "
    "which have no Linux counterpart here"
)


def macos_only(reason: str):
    return pytest.mark.skipif(not MACOS, reason=f"macOS only: {reason}")


def linux_only(fn):
    return pytest.mark.skipif(not LINUX, reason="Linux only: ELF symbols")(fn)


def require_macos(reason: str = CROSS_REASON) -> None:
    if not MACOS:
        pytest.skip(reason if reason.startswith("macOS only") else f"macOS only: {reason}")


def shlib(name: str, version: str | None = None) -> str:
    """A shared library's file name: Mach-O lib<n>.<v>.dylib, ELF lib<n>.so.<v>."""
    if MACOS:
        return f"lib{name}.{version}.dylib" if version else f"lib{name}.dylib"
    return f"lib{name}.so.{version}" if version else f"lib{name}.so"


SHLIB_SUFFIX = ".dylib" if MACOS else ".so"

# The toolchain an unpinned configure finds (Linux: BUILDLOG_LINUX_TOOLCHAINS).
FAMILY = "clang" if MACOS else os.environ.get("BUILDLOG_LINUX_TOOLCHAINS", "gcc").split(",")[0]
CC = "clang" if FAMILY == "clang" else "gcc"
CXX = "clang++" if FAMILY == "clang" else "g++"
CXX_RUNTIME = "libc++" if MACOS else "libstdc++"
MODULE_FLAG = "-bundle" if MACOS else "-shared"
SHARED_FLAG = "-dynamiclib" if MACOS else "-shared"


def force_load(path: str) -> str:
    return f"-Wl,-force_load,{path}" if MACOS else f"-Wl,--whole-archive,{path},--no-whole-archive"


def rpath(rel: str) -> str:
    """A build-tree rpath as build.ninja spells it (Ninja doubles `$`)."""
    return f"-Wl,-rpath,@loader_path/{rel}" if MACOS else f"'-Wl,-rpath,$$ORIGIN/'{rel}"


def needed_libs(path: str) -> str:
    """The dynamic libraries a binary names (otool -L, readelf -d NEEDED)."""
    argv = ["otool", "-L", path] if MACOS else ["readelf", "-d", path]
    return subprocess.check_output(argv, text=True)


# A test of examples/cross or another macOS cross configuration.
cross = pytest.mark.skipif(not MACOS, reason=CROSS_REASON)


def baseline_dir(case: Path) -> Path:
    """A case study's recorded upstream build for this host: baseline/ (macOS),
    baseline/linux (Linux with GCC) or baseline/linux-clang (Linux with Clang),
    each written by the study's baseline script (linux/run.sh baselines)."""
    base = case / "baseline"
    if MACOS:
        return base
    return base / ("linux-clang" if FAMILY == "clang" else "linux")
