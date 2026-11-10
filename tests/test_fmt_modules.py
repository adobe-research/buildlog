# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""{fmt} built as a C++20 module (case-studies/fmt-modules), with Homebrew
llvm@22; needs `case-studies/fmt-modules/fetch.sh` to have run."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import host
from tests.test_cxx_modules import ninja
from tests.test_include_order import LLVM_BIN, configure, needs_llvm

STUDY = Path(__file__).resolve().parent.parent / "case-studies" / "fmt-modules"

pytestmark = [
    pytest.mark.skipif(not host.MACOS, reason="macOS llvm@22 only"),
    pytest.mark.skipif(not (STUDY / "upstream" / "src" / "fmt.cc").exists(), reason="run fetch.sh"),
    needs_llvm,
]


def test_fmt_module(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDLOG_LLVM_BIN", str(LLVM_BIN))
    proj = tmp_path / "proj"
    shutil.copytree(STUDY / "project", proj, ignore=shutil.ignore_patterns(".git", "test", "doc"))
    build, _ = configure(proj, proj / "user.toml", tmp_path)
    r = ninja(build)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr
    app = next((build / "souffle" / "bin").rglob("app"))
    out = subprocess.run([app], capture_output=True, text=True, check=True).stdout
    assert out == "Hello, modules!    42 3.14\n"
    assert ninja(build).stdout.strip().endswith("no work to do.")
