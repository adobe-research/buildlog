# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The 7c cross build end to end: libHalide and its 32-test sample for aarch64-linux-gnu,
built on macOS arm64 in one tree (host generators built for the host), run in the Linux
container. Slow (minutes), so it runs only with BUILDLOG_HALIDE_CROSS=1 and the 7c
environment: Docker with the 7b image, the sysroot and Halide export on the case-sensitive
volume (docker/run-buildlog-cross.sh setup), and Homebrew llvm@22 and lld@22."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "case-studies" / "halide" / "docker"
BREW = Path("/opt/homebrew/opt")
READY = (
    os.environ.get("BUILDLOG_HALIDE_CROSS") == "1"
    and shutil.which("docker") is not None
    and (DOCKER / "xvol" / "sysroot" / "usr").is_dir()
    and (DOCKER / "xvol" / "src" / "src").is_dir()
    and (BREW / "llvm@22" / "bin" / "clang++").exists()
    and (BREW / "lld@22" / "bin" / "ld.lld").exists()
)


@pytest.mark.skipif(not READY, reason="BUILDLOG_HALIDE_CROSS=1 and the 7c environment required")
def test_cross_build_and_tests() -> None:
    out = ROOT / "build" / "halide-cross-test"
    out.mkdir(parents=True, exist_ok=True)
    # The container mounts the volume elsewhere than the host has it, so a path
    # of the host that leaked into a product would not exist there.
    env = {**os.environ, "OUT": str(out), "BUILDLOG_XVOL_MOUNT": "/mnt/elsewhere"}
    subprocess.run(
        [str(DOCKER / "run-buildlog-cross.sh"), "setup", "configure", "build", "test"],
        check=True,
        env=env,
    )
    counts = json.loads((out / "buildlog-test.json").read_text())["counts"]
    # CMake's 7c ground truth: 30 pass, 2 skip (no GPU).
    assert counts["failed"] == 0
    assert counts["passed"] == 30 and counts["skipped"] == 2
    # The rpath of libHalide is the LLVM directory on the target, not in the sysroot.
    lib = (
        DOCKER
        / "xvol"
        / "build"
        / "buildlog-cross"
        / "souffle"
        / "lib"
        / "arm64-linux.release.none"
        / "libHalide.so.22.0.0"
    )
    readelf = BREW / "llvm@22" / "bin" / "llvm-readelf"
    dyn = subprocess.run([str(readelf), "-d", str(lib)], check=True, capture_output=True, text=True)
    assert "[/usr/lib/llvm-22/lib]" in dyn.stdout and "xvol" not in dyn.stdout
