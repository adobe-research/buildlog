# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""buildlog-cuda against a real CUDA toolkit: compile and link with nvcc, and
read what came out (cuobjdump, readelf). Nothing here runs a GPU program: the
suite's machines have no GPU, so run-tests are deferred to a GPU machine."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from . import host
from .test_cpp_mechanisms import cmd_of, configure, needs_souffle, run_ninja
from .test_cuda import ARCH, project

NVCC = shutil.which("nvcc") or (
    "/usr/local/cuda/bin/nvcc" if Path("/usr/local/cuda/bin/nvcc").exists() else None
)

pytestmark = [
    pytest.mark.integration,
    needs_souffle,
    pytest.mark.skipif(not NVCC or not host.LINUX, reason="needs a Linux machine with nvcc"),
]

FILES = {
    "main.cpp": "void launch();\nint main() { launch(); return 0; }\n",
    "k.cu": "__global__ void k() {}\nvoid launch() { k<<<1, 1>>>(); }\n",
    "dev.cu": "__device__ int twice(int x) { return 2 * x; }\n",
    "use.cu": "extern __device__ int twice(int);\n__global__ void u(int *p) { *p = twice(*p); }\nvoid launch() { int *p = nullptr; u<<<1, 1>>>(p); }\n",
}

APP = """
request = [["app", "rel"]]
[target.app]
kind = "executable"
sources = ["main.cpp", "k.cu"]
"""

RDC = """
request = [["app", "rel"]]
cuda_rdc = [["app"], ["lib"]]
dep = [["app", "lib", "private"]]
[target.app]
kind = "executable"
sources = ["main.cpp", "use.cu"]
[target.lib]
kind = "library"
sources = ["dev.cu"]
"""


def built(tmp_path, spec):
    proj = project(tmp_path, ARCH + spec, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    run_ninja(build)
    exe = next((build / "souffle" / "bin").glob("*/app"))
    return build, exe


def tool(*argv):
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def test_a_real_nvcc_compiles_and_links_the_static_runtime(tmp_path):
    _, exe = built(tmp_path, APP)
    assert "sm_80" in tool(str(Path(NVCC).parent / "cuobjdump"), "--list-elf", str(exe))
    needed = host.needed_libs(str(exe))
    assert "libcudart" not in needed


def test_the_shared_runtime_is_a_needed_library(tmp_path):
    _, exe = built(
        tmp_path, APP.replace("[target.app]", 'cuda_runtime = [["app", "shared"]]\n[target.app]')
    )
    assert "libcudart.so" in host.needed_libs(str(exe))


def test_rdc_across_a_static_library_device_links_at_the_executable(tmp_path):
    build, exe = built(tmp_path, RDC)
    assert "-dlink" in cmd_of(build, "app/cuda_dlink.o")
    assert "sm_80" in tool(str(Path(NVCC).parent / "cuobjdump"), "--list-elf", str(exe))


def host_of(tmp_path, gxx_flags):
    """A host compiler that is g++ claiming another version: what nvcc's
    host_config.h reads is the compiler's own macros."""
    wrapper = tmp_path / "g++-claims"
    wrapper.write_text(f'#!/bin/sh\nexec g++ {gxx_flags} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def check(tmp_path, cxx):
    from types import SimpleNamespace

    from buildlog_cuda.cuda import host_check

    ctx = SimpleNamespace(workdir=tmp_path / "work")
    ctx.workdir.mkdir()
    rows = host_check(ctx, "req", (NVCC, "gcc", cxx), {})["res_cuda_host_check"]
    return rows[0][2:]


def test_the_real_toolkit_takes_the_distributions_gcc(tmp_path):
    assert check(tmp_path, "g++") == ("ok", "")


def test_the_real_toolkit_refuses_a_host_it_does_not_know_in_its_own_words(tmp_path):
    status, message = check(tmp_path, host_of(tmp_path, "-U__GNUC__ -D__GNUC__=99"))
    assert status == "refused"
    assert "unsupported GNU version" in message
