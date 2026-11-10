# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cache fingerprints of the buildlog-cpp probes (inventory, toolchain
capability probes) change when anything the probes read changes in place."""

from __future__ import annotations

import os
import sys

import pytest
from buildlog_cpp import probes

from buildlog.engine import configure_project

from . import host
from .test_probe_seeding import CROSS, USER, pytestmark  # noqa: F401


def _results(tool: str) -> dict:
    return {
        "res_toolchain_tool": {("r", "tc", "cxx", tool), ("r", "tc", "cc", tool)},
        "res_toolchain": {("r", "tc", "clang", "1.0")},
    }


def test_toolchain_fingerprint_follows_the_tool_binary(tmp_path):
    tool = tmp_path / "cxx"
    tool.write_text("#!/bin/sh\n")
    fp = lambda: probes.toolchain_fingerprint(("tc",), _results(str(tool)), toolchain_arg=0)
    before = fp()
    assert fp() == before
    tool.write_text("#!/bin/sh\n# upgraded in place\n")
    assert fp() != before
    after = fp()
    st = tool.stat()
    os.utime(tool, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert fp() != after


def test_toolchain_fingerprint_follows_compiler_environment(tmp_path, monkeypatch):
    tool = tmp_path / "cxx"
    tool.write_text("x")
    monkeypatch.delenv("CPATH", raising=False)
    before = probes.toolchain_fingerprint(("tc",), _results(str(tool)), toolchain_arg=0)
    monkeypatch.setenv("CPATH", "/somewhere")
    assert probes.toolchain_fingerprint(("tc",), _results(str(tool)), toolchain_arg=0) != before


@pytest.mark.parametrize("var", ["PATH", "CC", "CXX", "SDKROOT", "DEVELOPER_DIR"])
def test_inventory_fingerprint_follows_environment(monkeypatch, var):
    monkeypatch.delenv(var, raising=False)
    before = probes.inventory_fingerprint((), {})
    assert probes.inventory_fingerprint((), {}) == before
    monkeypatch.setenv(var, os.environ.get("PATH", "") + os.pathsep + "/elsewhere")
    assert probes.inventory_fingerprint((), {}) != before


@pytest.mark.skipif(sys.platform == "darwin", reason="Linux finds its tools on PATH")
def test_inventory_fingerprint_follows_a_tool_on_path(tmp_path, monkeypatch):
    gxx = tmp_path / "g++"
    gxx.write_text("x")
    gxx.chmod(0o755)  # shutil.which finds executables only
    monkeypatch.setenv("PATH", str(tmp_path))
    before = probes.inventory_fingerprint((), {})
    gxx.write_text("a new compiler")
    assert probes.inventory_fingerprint((), {}) != before


@host.cross
def test_warm_configure_reruns_probes_when_a_tool_changes_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    build = tmp_path / "build"

    def configure() -> dict:
        return configure_project(CROSS, build, user_files=[USER])[1]

    configure()
    warm = configure()
    assert warm["executed"] == 0 and len(warm["rounds"]) == 1
    assert warm["cache_hits"] > 0

    real = probes.file_identity
    state = {"bump": 0}

    def changed(path: str) -> list:
        ident = real(path)
        if os.path.basename(path) in ("clang", "clang++") and ident[2] is not None:
            ident[2] += state["bump"]
        return ident

    monkeypatch.setattr(probes, "file_identity", changed)
    assert configure()["executed"] == 0  # the wrapper alone changes nothing
    state["bump"] = 1  # the compilers are replaced in place
    rerun = configure()
    assert rerun["executed"] > 0
    assert configure()["executed"] == 0

    monkeypatch.setenv("CC", "some-other-compiler")
    assert configure()["executed"] >= 1  # inventory is asked again
