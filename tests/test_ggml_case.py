# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""ggml case study: the CPU backend's ISA variants as dlopen'd modules.

case-studies/ggml/project builds ggml (v0.25.3) two ways: linked (the
default, GGML_NATIVE: one backend compiled for this machine) and, with
presets/variants.toml, as CMake's -DGGML_BACKEND_DL=ON
-DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF: one module per ISA level of
the configuration's platform, loaded by libggml at run time. The ground
truth is case-studies/ggml/baseline (run-cmake.sh's CMake builds).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "ggml"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang.toml"
VARIANTS = PROJECT / "presets" / "variants.toml"
BASELINE = host.baseline_dir(CASE)
SRC = Path(os.path.expanduser(os.environ.get("BUILDLOG_GGML_SRC", str(CASE / "upstream"))))
REL = "arm64-macos.release.none"
X86 = "x86_64-macos.release.none"
VARS = {"GGML_SRC": str(SRC), "GGML_BUILD_COMMIT": "353b63b4"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "include" / "ggml.h").exists(), reason="the ggml checkout is absent"
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]

APPLE_LEVELS = {"apple_m1", "apple_m2_m3", "apple_m4"}
LINUX_LEVELS = {
    "armv8.0_1", "armv8.2_1", "armv8.2_2", "armv8.2_3", "armv8.6_1", "armv8.6_2",
    "armv9.2_1", "armv9.2_2",
}  # fmt: skip
ARM_LEVELS = APPLE_LEVELS if host.MACOS else LINUX_LEVELS
# GCC 13 has no +sme, so it cannot build the two armv9.2 levels (README, "Linux").
GCC_NO_SME = host.LINUX and host.FAMILY == "gcc"
variants_run = pytest.mark.skipif(GCC_NO_SME, reason="GCC 13 has no +sme: the armv9.2 levels fail")
X86_LEVELS = {
    "x64", "sse42", "sandybridge", "ivybridge", "piledriver", "haswell", "skylakex",
    "cannonlake", "cascadelake", "icelake", "cooperlake", "zen4", "alderlake",
    "sapphirerapids",
}  # fmt: skip


def configure(build: Path, user: list[Path], configs: list[str] | None = None) -> dict:
    _, metrics = configure_project(
        PROJECT, build, user_files=user, configs=configs, var_overrides=VARS
    )
    assert metrics["errors"] == [] and metrics["global_errors"] == []
    return metrics


def ninja(build: Path, *args: str) -> str:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    ).stdout


def buildlog_test(build: Path) -> dict:
    out = build.parent / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(build), "--json", str(out)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.read_text())


def machine_flag(w: str) -> bool:
    return w.startswith(("-m", "-U", "-DGGML_")) or w == "-fopenmp"


def compile_flags(build: Path, cfg: str) -> dict[str, set[str]]:
    """target -> the -m/-U/-D words of its compiles in cfg (union over its sources)."""
    out: dict[str, set[str]] = {}
    for line in ninja(build, "-t", "commands").splitlines():
        words = shlex.split(line)
        if "-c" not in words:
            continue
        m = re.match(rf"obj/{re.escape(cfg)}/([^/]+)/", words[words.index("-o") + 1])
        if m:
            out.setdefault(m.group(1), set()).update(w for w in words if machine_flag(w))
    return out


def baseline_flags(name: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in (BASELINE / name / "compile-commands.jsonl").read_text().splitlines():
        e = json.loads(line)
        t = re.search(r"CMakeFiles/([^/]+)\.dir/", e["output"]).group(1)
        out.setdefault(t, set()).update(w for w in e["args"] if machine_flag(w))
    return out


def native_cpu_file(tmp: Path) -> list[Path]:
    """Linux: the machine's CPU as upstream's CMake reads it (native_cpu.py)."""
    if host.MACOS:
        return []
    out = tmp / "native-cpu.toml"
    out.write_text(
        subprocess.check_output([sys.executable, str(CASE / "native_cpu.py"), host.CC], text=True)
    )
    return [out]


@pytest.fixture(scope="module")
def default_build(tmp_path_factory):
    root = tmp_path_factory.mktemp("ggml-default")
    build = root / "build"
    configure(build, [PRESET, *native_cpu_file(root)])
    return build


@pytest.fixture(scope="module")
def variants_build(tmp_path_factory):
    build = tmp_path_factory.mktemp("ggml-variants") / "build"
    configure(
        build, [PRESET, VARIANTS], configs=["release", "x86_64"] if host.MACOS else ["release"]
    )
    return build


def test_default_is_one_linked_native_backend(default_build):
    flags = compile_flags(default_build, REL)
    assert not any(t.startswith("ggml-cpu-") for t in flags)
    base = baseline_flags("cpu")
    assert flags["ggml-cpu"] == base["ggml-cpu"]
    assert flags["ggml-base"] == base["ggml-base"]
    assert "-DGGML_USE_CPU" in flags["test-backend-ops"]


def test_default_tests_pass(default_build):
    results = buildlog_test(default_build)
    assert results["counts"] == {"passed": 2, "skipped": 0, "failed": 0, "not run": 0}


@variants_run
def test_one_module_per_isa_level_with_cmakes_flags(variants_build):
    cases = [(REL, "variants-arm64", ARM_LEVELS)]
    if host.MACOS:
        cases.append((X86, "variants-x86_64", X86_LEVELS))
    for cfg, base, levels in cases:
        flags = compile_flags(variants_build, cfg)
        cmake = baseline_flags(base)
        mods = {t for t in flags if t.startswith("ggml-cpu-") and not t.endswith("-feats")}
        assert mods == {f"ggml-cpu-{lv}" for lv in levels}
        for t in mods | {f"{m}-feats" for m in mods}:
            assert flags[t] == cmake[t], t


@variants_run
def test_variant_modules_sit_next_to_the_loader(variants_build):
    loader = host.shlib("ggml", "0.25.3")
    ninja(variants_build, f"lib/{REL}/{loader}", *(
        f"bin/{REL}/libggml-cpu-{lv}.so" for lv in ARM_LEVELS
    ))  # fmt: skip
    souffle = variants_build / "souffle"
    for lv in ARM_LEVELS:
        link = souffle / "bin" / REL / f"libggml-cpu-{lv}.so"
        assert link.is_symlink() and link.resolve() == (souffle / "lib" / REL / link.name).resolve()
        loads = host.needed_libs(str(link.resolve()))
        assert (
            host.shlib("ggml", "0") not in loads
        )  # a module is loaded, not linked against the loader
    exe_loads = host.needed_libs(str(souffle / "lib" / REL / loader))
    assert "ggml-cpu" not in exe_loads  # nor is it linked into the loader


@variants_run
def test_the_best_arm64_variant_is_loaded(variants_build):
    """buildlog test passes the arm64 cases only if the loader reports the module
    rules.dl predicts from this machine's features; x86_64 is not run (cross)."""
    results = buildlog_test(variants_build)
    cross = 2 if host.MACOS else 0
    assert results["counts"] == {"passed": 2, "skipped": 0, "failed": 0, "not run": cross}
    out = subprocess.run(
        [str(variants_build / "souffle" / "bin" / REL / "test-backend-ops"), "-o", "ADD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=variants_build / "souffle" / "bin" / REL,
    )
    loaded = re.findall(
        r"loaded CPU backend from \S*libggml-cpu-([\w.]+)\.so", out.stdout + out.stderr
    )
    assert len(loaded) == 1 and loaded[0] in ARM_LEVELS


@pytest.mark.skipif(not GCC_NO_SME, reason="Linux with GCC 13")
def test_gcc_13_cannot_build_the_sme_levels(tmp_path):
    """buildlog says so at configure time; upstream's CMake configures and then
    fails to compile exactly those two modules (baseline/linux/variants-arm64)."""
    r = subprocess.run(
        [sys.executable, "-m", "buildlog", "configure", "-C", str(tmp_path / "b"),
         "--config", "release", "--user", str(PRESET), "--user", str(VARIANTS),
         "-D", f"GGML_SRC={SRC}", str(PROJECT)],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "gcc: sme (armv9.2_1)" in out and "gcc: sme (armv9.2_2)" in out
    assert out.count("ISA extension has no flag") == 2
    failed = (BASELINE / "variants-arm64" / "build-status.txt").read_text().splitlines()
    mods = {m.group(1) for ln in failed if (m := re.search(r"ggml-cpu-([\w.]+)\.dir", ln))}
    assert mods == {"armv9.2_1", "armv9.2_2"}


ERRORS = {
    "haswell-on-arm64": "ISA scope incompatible with the configuration's cpu: ggml-cpu-haswell (haswell) needs avx2, which arm64",
    "variants-without-dl": "option requirement not met: GGML_CPU_ALL_VARIANTS is on",
    "native-variants": "options conflict: GGML_NATIVE and GGML_CPU_ALL_VARIANTS are both on",
}


@pytest.mark.parametrize("name", sorted(ERRORS))
def test_incompatible_requests_are_errors(tmp_path, name):
    r = subprocess.run(
        [sys.executable, "-m", "buildlog", "configure", "-C", str(tmp_path / "b"),
         "--user", str(PRESET), "--user", str(PROJECT / "errors" / f"{name}.toml"),
         "-D", f"GGML_SRC={SRC}", str(PROJECT)],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 1
    assert ERRORS[name] in r.stdout + r.stderr


@host.cross
def test_native_backend_is_not_cross_compiled(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "buildlog", "configure", "-C", str(tmp_path / "b"),
         "--config", "x86_64", "--user", str(PRESET), "-D", f"GGML_SRC={SRC}", str(PROJECT)],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 1
    assert "GGML_NATIVE for a configuration the build machine does not run" in r.stdout + r.stderr
