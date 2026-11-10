# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers of the zlib case study tests (tests/test_zlib_phase*.py).

The project is case-studies/zlib/project, configured with its preset user
file against the pinned zlib checkout (buildlog.toml's ZLIB_SRC); the ground
truth is case-studies/zlib/baseline (baseline/run-cmake.sh's normalized
output of zlib's own CMake build), recorded on macOS, and on Linux in
baseline/linux (linux/run.sh baselines, Ubuntu 24.04 and GCC 13).
"""

from __future__ import annotations

import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "zlib"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang-arm64.toml"
BASELINE = CASE / "baseline" if host.MACOS else CASE / "baseline" / "linux"
# How each build spells the target, beside -O3: CMake's -arch and
# buildlog's --target on macOS (the ledger's residual); nothing on Linux,
# where both use the host's GCC as is.
CMAKE_TARGET_FLAGS = {"-arch arm64"} if host.MACOS else set()
if host.MACOS:
    OUR_TARGET_FLAGS = {"--target=arm64-apple-macos13"}
elif host.FAMILY == "clang":
    OUR_TARGET_FLAGS = {f"--target={platform.machine()}-linux-gnu"}
else:
    OUR_TARGET_FLAGS = set()
SRC = ROOT / "case-studies" / "zlib" / "upstream"
REL = "arm64-macos.release.none"
# The static flavour is the release-static configuration (native.linkage =
# static); CMake builds it as twin targets in the same tree.
STATIC = f"{REL}.static"
# buildlog's target in STATIC -> CMake's twin.
CMAKE_STATIC_NAME = {
    "zlib": "zlibstatic",
    "zlib_example": "zlib_static_example",
    "minigzip": "static_minigzip",
}

needs_zlib = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(not (SRC / "zlib.h").exists(), reason="the pinned zlib checkout is absent"),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]

# Compile definitions a configure-time check decides.
CHECK_DEFINES = {"HAVE_HIDDEN", "NO_FSEEKO"}


def configure(build: Path, **kw) -> dict:
    _, metrics = configure_project(PROJECT, build, user_files=[PRESET], **kw)
    assert metrics["errors"] == []
    return metrics


def ninja(build: Path, *args: str) -> str:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    ).stdout


def commands(build: Path) -> list[str]:
    return ninja(build, "-t", "commands").splitlines()


def baseline_commands() -> list[str]:
    return (BASELINE / "commands.txt").read_text().splitlines()


def _include_role(path: str) -> str:
    """An include dir as a role: the source tree or the generated-header dir."""
    p = path.rstrip("/")
    if p in ("$SRC",) or p.endswith(("case-zlib/zlib", "case-studies/zlib/upstream")):
        return "SRC"
    return "GEN"


def compiles(lines: list[str]) -> dict[tuple[str, str], dict]:
    """(target, source) -> defines, include dirs in order (flag, role), other flags, of
    every C compile in a command list (CMake's or buildlog's)."""
    out = {}
    for line in lines:
        words = shlex.split(line)
        if "-c" not in words:
            continue
        src = words[words.index("-c") + 1]
        o = words[words.index("-o") + 1]
        m = re.search(r"CMakeFiles/([^/]+)\.dir/", o)
        name = m.group(1) if m else None
        if not m:
            m = re.search(r"obj/([^/]+)/([^/]+)/", o)
            assert m, line
            name = cmake_name(m.group(2), m.group(1))
        rel = re.split(r"\$SRC/|case-zlib/zlib/|case-studies/zlib/upstream/", src)[-1]
        defines, includes, other = set(), [], []
        it = iter(words[1:])
        for w in it:
            if w.startswith("-D"):
                defines.add(w[2:])
            elif w.startswith("-I"):
                includes.append(("-I", _include_role(w[2:])))
            elif w == "-isystem":
                includes.append(("-isystem", _include_role(next(it))))
            elif w in ("-o", "-c", "-MT", "-MF", "-isysroot", "-arch"):
                v = next(it)
                if w in ("-isysroot", "-arch"):
                    other.append(f"{w} {v}")
            elif w != "-MD":
                other.append(w)
        out[(name, rel)] = {"defines": defines, "includes": includes, "other": set(other)}
    return out


def links(lines: list[str]) -> dict[str, list[str]]:
    """Output file name -> argv of every link (CMake's lines start with `: &&`)."""
    out = {}
    for line in lines:
        words = shlex.split(line.replace(": && ", "").replace(" && :", ""))
        if "-c" in words or "-o" not in words or words[0].endswith("ar"):
            continue
        path = Path(words[words.index("-o") + 1])
        out[cmake_name(path.name, path.parent.name)] = words
    return out


def cmake_name(target: str, cfg: str) -> str:
    """The CMake target (or file) name of buildlog's target in cfg."""
    return CMAKE_STATIC_NAME.get(target, target) if cfg == STATIC else target
