# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""GCC spells the ISA extensions as clang does (F_gcc.dl copies F_clang.dl's
family_isa_* facts): every x86 flag and every AArch64 -march piece that
F_clang.dl states compiles with GCC, except +sme, which GCC 13 lacks and
F_gcc.dl leaves out. Runs where a GCC for each architecture
is installed (the Linux image: gcc, gcc-x86-64-linux-gnu)."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

F_CLANG = (
    Path(__file__).resolve().parents[1] / "plugins/buildlog-cpp/src/buildlog_cpp/layers/F_clang.dl"
)


def gcc_for(arch: str) -> str | None:
    native = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64"}.get(platform.machine())
    if native == arch and shutil.which("gcc") and "gcc" in _version("gcc"):
        return "gcc"
    cross = {"arm64": "aarch64-linux-gnu-gcc", "x86_64": "x86_64-linux-gnu-gcc"}[arch]
    return cross if shutil.which(cross) else None


def _version(cc: str) -> str:
    out = subprocess.run([cc, "--version"], capture_output=True, text=True, check=False).stdout
    return "gcc" if "Free Software Foundation" in out else out


def facts(rel: str) -> list[tuple[str, ...]]:
    text = F_CLANG.read_text()
    rows = re.findall(rf'^{rel}\("clang", (.*)\)\.$', text, re.MULTILINE)
    return [tuple(x.strip().strip('"') for x in row.split(",")) for row in rows]


def compiles(cc: str, flags: list[str], tmp_path: Path) -> str:
    src = tmp_path / "isa.c"
    src.write_text("int f(int x) { return x + 1; }\n")
    r = subprocess.run(
        [cc, *flags, "-c", str(src), "-o", str(tmp_path / "isa.o")],
        capture_output=True,
        text=True,
        check=False,
    )
    return "" if r.returncode == 0 else r.stderr


def test_gcc_takes_clangs_x86_flags(tmp_path):
    cc = gcc_for("x86_64")
    if cc is None:
        pytest.skip("no GCC for x86_64 (the Linux image has gcc-x86-64-linux-gnu)")
    flags = facts("family_isa_flag")
    assert len(flags) >= 20
    failures = {f: err for _e, f in flags if (err := compiles(cc, [f], tmp_path))}
    assert failures == {}


def test_gcc_takes_clangs_aarch64_march(tmp_path):
    cc = gcc_for("arm64")
    if cc is None:
        pytest.skip("no GCC for AArch64")
    versions = {v: int(r) for _cpu, v, r in facts("family_isa_march_version")}
    need = dict(facts("family_isa_ext_version"))
    pieces = [(e, s) for e, s, _r in facts("family_isa_march_ext")]
    pieces += [(e, s) for e, s, _r in facts("family_isa_march_off")]
    assert versions and pieces
    failures = {}
    for v in versions:
        if err := compiles(cc, [f"-march={v}"], tmp_path):
            failures[v] = err
    for e, s in pieces:
        if e == "sme":
            assert compiles(cc, [f"-march={need[e]}+{s}"], tmp_path), "GCC now has +sme"
            continue
        flag = f"-march={need.get(e, 'armv8-a')}+{s}"
        if err := compiles(cc, [flag], tmp_path):
            failures[flag] = err
    assert failures == {}
