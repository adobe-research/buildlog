# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide cross-compilation case studies: apps/HelloWasm and apps/HelloAndroid.

Each is one configure and one Ninja graph holding the host generator, the
generator runs, and the target code. The configure tests need an installed
Halide (BUILDLOG_HALIDE_PREFIX) and the SDK (EMSDK / ANDROID_NDK_ROOT); the
build tests also run the SDK's tools (Emscripten additionally needs
EM_BINARYEN_ROOT, a binaryen new enough for the LLVM Halide uses).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

from . import host
from .halide_env import halide_available, halide_define

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "case-studies" / "halide" / "apps"
PACKAGES = ROOT / "case-studies" / "halide" / "packages"
HOST = "arm64-macos.release.none"
# Homebrew's libpng and libjpeg on macOS, apt's on Linux
USER_PRESET = "homebrew-arm64.toml" if host.MACOS else "linux.toml"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not halide_available(),
        reason="BUILDLOG_HALIDE_PREFIX unset or not a directory, or the Halide submodule is missing",
    ),
]

EMSDK = os.environ.get("EMSDK")
NDK = os.environ.get("ANDROID_NDK_ROOT")
needs_emsdk = pytest.mark.skipif(
    not (EMSDK and (Path(EMSDK) / "emsdk_env.sh").exists()),
    reason="EMSDK unset or not an emsdk install",
)
needs_ndk = pytest.mark.skipif(
    not (NDK and Path(NDK, "build/cmake/android.toolchain.cmake").exists()),
    reason="ANDROID_NDK_ROOT unset or not an Android NDK",
)


def configure(app: str, tmp_path: Path) -> Path:
    build = tmp_path / "build"
    os.environ["BUILDLOG_PACKAGE_PATH"] = str(PACKAGES)
    try:
        _, metrics = configure_project(
            APPS / app,
            build,
            user_files=[APPS / app / "presets" / USER_PRESET],
            var_overrides={"HALIDE_PREFIX": halide_define()[1].split("=", 1)[1]},
        )
    finally:
        del os.environ["BUILDLOG_PACKAGE_PATH"]
    assert metrics["errors"] == []
    return build


def ninja_commands(build: Path) -> list[str]:
    out = subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "-t", "commands"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ln for ln in out.splitlines() if not ln.startswith("ninja:")]


def run_ninja(build: Path) -> None:
    r = subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "-j4"], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.fixture(scope="module")
def wasm(tmp_path_factory):
    return configure("hellowasm", tmp_path_factory.mktemp("hellowasm"))


@pytest.fixture(scope="module")
def android(tmp_path_factory):
    return configure("helloandroid", tmp_path_factory.mktemp("helloandroid"))


WASM = "wasm32-emscripten.release.none"
# configuration directory suffix -> whether SIMD / threads
VARIANTS = {
    "": (False, False),
    ".simd128": (True, False),
    ".threads": (False, True),
    ".simd128.threads": (True, True),
}


@needs_emsdk
def test_wasm_one_graph_host_generator_and_wasm_objects(wasm):
    cmds = ninja_commands(wasm)
    # 4 wasm configurations x (init, update, render) generator runs; the host
    # runner's three libraries are generated for the host in the same graph
    gens = [c for c in cmds if "reaction_diffusion_generator " in c and " -g " in c]
    assert sum("target=wasm-32-wasmrt" in c for c in gens) == 12
    assert sum("target=host" in c or "target=arm-64-osx" in c for c in gens) == 3
    # The generator is built for the host, the app for wasm, in the same graph.
    assert any(f"bin/{HOST}/reaction_diffusion_generator" in c for c in cmds)
    ninja = (wasm / "souffle" / "build.ninja").read_text()
    for suffix in VARIANTS:
        assert f"bin/{WASM}{suffix}/index.wasm" in ninja
    assert any("app/core.cpp" in c and "emscripten" in c for c in cmds)


@needs_emsdk
def test_wasm_variants_are_configurations_not_copies(wasm):
    # One index target and three libraries, not one set per variant, and no
    # per-variant file names: each configuration has its own directory.
    ninja = (wasm / "souffle" / "build.ninja").read_text()
    assert "index_simd" not in ninja and "index_threads" not in ninja
    for suffix in VARIANTS:
        assert f"lib/{WASM}{suffix}/libreaction_diffusion_init.a" in ninja


@needs_emsdk
def test_wasm_generator_targets_and_params_match_cmake(wasm):
    cmds = [c for c in ninja_commands(wasm) if "reaction_diffusion_generator " in c and " -g " in c]
    runs = {}
    for c in cmds:
        m = re.search(r"-d \S+ -g (\S+) .*target=(\S+) (threads=\S+)", c)
        runs.setdefault((m[2], m[3]), set()).add(m[1])
    for simd, threads in VARIANTS.values():
        feats = "-wasm_simd128" * simd + "-wasm_threads" * threads
        key = (f"wasm-32-wasmrt{feats}-no_runtime", f"threads={'true' if threads else 'false'}")
        assert runs[key] == {
            "reaction_diffusion_init",
            "reaction_diffusion_update",
            "reaction_diffusion_render",
        }
    # The host libraries (the benchmarks) are threaded; only they register.
    assert ("host-no_runtime", "threads=true") in runs
    registered = [c for c in cmds if "registration" in c]
    assert len(registered) == 3 and all("target=host" in c for c in registered)


@needs_emsdk
def test_wasm_flags_come_from_the_plugin(wasm):
    cmds = ninja_commands(wasm)
    links = {}
    for c in cmds:
        m = re.search(r"-o bin/(wasm32-emscripten\S*)/index\.js", c)
        if m:
            links[m[1].removeprefix(WASM)] = c
    assert set(links) == set(VARIANTS)
    for suffix, (_, threads) in VARIANTS.items():
        link = links[suffix]
        # -O3 from native.opt, SDL2 from the port package, -matomics from the
        # threads setting; -sWASM=1 is emcc's default and is not spelled.
        assert " -O3 " in link and "--use-port=sdl2" in link and "-sTOTAL_MEMORY=512MB" in link
        assert ("-matomics" in link) == threads
        assert "-sWASM=1" not in link
    compiles = [c for c in cmds if " -c " in c and "app/core.cpp" in c and "emscripten" in c]
    assert len(compiles) == 4 and all("--use-port=sdl2" in c for c in compiles)


@needs_emsdk
def test_wasm_runtime_libraries_carry_the_oss_threads_and_dl_requirements(wasm):
    # add_halide_library gives each runtime library Threads::Threads and
    # ${CMAKE_DL_LIBS}: libc's on macOS and Emscripten, -pthread and -ldl on
    # Linux. The host `run` is the one program linked for the host; the wasm
    # programs never get either.
    cmds = ninja_commands(wasm)
    run = next(c for c in cmds if re.search(r"-o bin/\S+/run( |$)", c))
    assert (" -ldl" in run) == host.LINUX and (" -pthread" in run) == host.LINUX
    for c in cmds:
        if "/index.js" in c and " -o " in c:
            assert " -ldl" not in c and " -pthread" not in c


@needs_emsdk
@pytest.mark.skipif(
    not os.environ.get("EM_BINARYEN_ROOT") or not shutil.which("wasm-validate"),
    reason="EM_BINARYEN_ROOT unset or wabt's wasm-validate missing",
)
def test_wasm_builds_and_validates(wasm):
    run_ninja(wasm)
    for suffix, (_, threads) in VARIANTS.items():
        flags = ["--enable-threads"] if threads else []
        r = subprocess.run(
            [
                "wasm-validate",
                *flags,
                str(wasm / "souffle" / "bin" / f"{WASM}{suffix}" / "index.wasm"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0, (suffix, r.stderr)


@needs_ndk
def test_android_one_graph_host_generator_and_target_objects(android):
    cmds = "\n".join(ninja_commands(android))
    ninja = (android / "souffle" / "build.ninja").read_text()
    for gen in ("deinterleave_generator", "edge_detect_generator"):
        assert f"bin/{HOST}/{gen}" in ninja
    for abi, triple in (("arm64", "aarch64-linux-android23"), ("x86_64", "x86_64-linux-android23")):
        assert f"lib/{abi}-android.release.none/libHelloAndroid.so" in ninja
        assert f"--target={triple}" in cmds
    # The Halide targets are the Android ones; the generators run on the host.
    for t in ("arm-64-android", "arm-64-android-no_runtime", "x86-64-android"):
        assert f"target={t}" in cmds
    assert "toolchains/llvm/prebuilt" in cmds and "-static-libstdc++" in cmds
    # bionic has dl and the thread functions in libc: the runtime libraries add neither
    for c in cmds.splitlines():
        if "libHelloAndroid.so" in c and " -o " in c:
            assert " -ldl" not in c and " -pthread" not in c


@needs_ndk
def test_android_builds_and_so_is_sane(android):
    run_ninja(android)
    tools = Path(NDK) / "toolchains" / "llvm" / "prebuilt"
    bindirs = list(tools.glob("*/bin"))
    assert bindirs
    readelf, nm = bindirs[0] / "llvm-readelf", bindirs[0] / "llvm-nm"
    for abi, machine in (("arm64", "AArch64"), ("x86_64", "X86-64")):
        so = android / "souffle" / "lib" / f"{abi}-android.release.none" / "libHelloAndroid.so"
        hdr = subprocess.run(
            [readelf, "-h", "-d", so], capture_output=True, text=True, check=False
        ).stdout
        assert "DYN (Shared object file)" in hdr and machine in hdr
        needed = set(re.findall(r"NEEDED\)\s+Shared library: \[(\S+)\]", hdr))
        assert {"liblog.so", "libandroid.so", "libc.so"} <= needed
        assert not any("libc++" in n or "Halide" in n for n in needed)
        syms = subprocess.run(
            [nm, "-D", "--defined-only", so], capture_output=True, text=True, check=False
        ).stdout
        assert "Java_" in syms
