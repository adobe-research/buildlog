# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Where the Halide tests find the user's Halide install.

The install location is a user decision, so it comes from the environment
(BUILDLOG_HALIDE_PREFIX) and reaches the specs through `-D HALIDE_PREFIX=...`;
the tests that need it skip when it is unset or absent.

The Halide source is the submodule case-studies/halide/upstream. The phase tests
that compare with CMake's output also need the CMake release tree of
case-studies/halide/baseline/run-cmake.sh (BUILDLOG_HALIDE_CMAKE_TREE, default
build/case-halide/cmake-release); they skip when it is absent."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = os.environ.get("BUILDLOG_HALIDE_PREFIX")
HALIDE_SRC = ROOT / "case-studies" / "halide" / "upstream"
CMAKE_TREE = Path(
    os.environ.get(
        "BUILDLOG_HALIDE_CMAKE_TREE", str(ROOT / "build" / "case-halide" / "cmake-release")
    )
).expanduser()
SKIP_REASON = (
    "BUILDLOG_HALIDE_PREFIX unset or not a directory, or the case-studies/halide/upstream "
    "submodule is not initialised"
)


def halide_prefix() -> Path | None:
    return Path(PREFIX).expanduser() if PREFIX else None


def halide_available() -> bool:
    prefix = halide_prefix()
    return prefix is not None and prefix.is_dir() and (HALIDE_SRC / "apps").exists()


def halide_define() -> list[str]:
    return ["-D", f"HALIDE_PREFIX={halide_prefix()}"]


def compare_env() -> dict[str, str]:
    """The environment for phase1/compare_libhalide.py: where CMake's trees are."""
    return {**os.environ, "HALIDE_BUILD_ROOT": str(CMAKE_TREE.parent)}


def cmake_source_dir() -> str:
    """The source directory CMake's tree was configured from, which its generated
    files spell out (the tests compare with it as if it were HALIDE_SRC)."""
    for line in (CMAKE_TREE / "CMakeCache.txt").read_text().splitlines():
        if line.startswith("CMAKE_HOME_DIRECTORY:"):
            return line.split("=", 1)[1]
    raise AssertionError(f"no CMAKE_HOME_DIRECTORY in {CMAKE_TREE}/CMakeCache.txt")


def project_copy(build: Path) -> Path:
    """The libHalide project copied beside a build directory, without its lock: the
    configure with a user preset records the packages it discovers in buildlog.lock,
    and the committed lock must not change."""
    return build.parent / "libhalide"


def configure_libhalide(tmp_path_factory, user_preset: Path | None = None, **kw):
    """Configure a copy of case-studies/halide/libhalide into a temporary build
    directory; returns (build, metrics). The copy is `project_copy(build)`."""
    from buildlog.engine import configure_project

    root = tmp_path_factory.mktemp("libhalide")
    project = root / "libhalide"
    shutil.copytree(
        ROOT / "case-studies" / "halide" / "libhalide",
        project,
        ignore=shutil.ignore_patterns("buildlog.lock", "__pycache__"),
    )
    preset = user_preset or project / "presets" / "homebrew-arm64.toml"
    _, metrics = configure_project(project, root / "build", user_files=[preset], **kw)
    return root / "build", metrics
