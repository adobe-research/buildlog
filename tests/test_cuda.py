# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""buildlog-cuda: nvcc objects, host compiler, architecture sets, the device
link and the runtime, against a fake toolkit (a stub nvcc and empty libraries
under a targets/<platform> directory), so no GPU or CUDA install is needed.
The real toolkit is exercised by linux/cuda/ and the stdgpu study."""

from __future__ import annotations

import json
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest
from buildlog_cpp.probes import host_platform

from . import host
from .test_cpp_mechanisms import cmd_of, configure, configure_fails, needs_souffle, ninja_text
from .test_cpp_mechanisms import project as cpp_project

pytestmark = [pytest.mark.integration, needs_souffle]

# The host's default C++ toolchain, and a second one beside it.
DEFAULT_TC = "apple-clang" if host.MACOS else host.FAMILY
ALT_TC = "llvm" if host.MACOS else ("clang" if host.FAMILY == "gcc" else "gcc")
ALT_FRAGMENT = "llvm" if host.MACOS else ALT_TC

NVCC = """#!{py}
import json, sys, pathlib
a = sys.argv[1:]
here = pathlib.Path(__file__).resolve().parent
if "--version" in a:
    print("nvcc: NVIDIA (R) Cuda compiler driver")
    print("Cuda compilation tools, release {ver}, V{ver}.0")
elif "--list-gpu-code" in a:
    print("\\n".join("sm_%d" % n for n in {codes}))
elif "--list-gpu-arch" in a:
    print("\\n".join("compute_%d" % n for n in {codes}))
elif "-ccbin" in a:
    host = a[a.index("-ccbin") + 1]
    refuse = json.loads((here / "refuse.json").read_text())
    for frag, msg in refuse.items():
        if frag in host:
            print(msg)
            sys.exit(1)
"""


def toolkit(tmp_path, platform=None, version="13.4", codes=(75, 80, 86, 89, 90, 100), refuse=None):
    platform = platform or host_platform()
    root = tmp_path / "cuda"
    (root / "bin").mkdir(parents=True)
    src = NVCC.format(py=sys.executable, ver=version, codes=list(codes))
    for tool in ("nvcc", "ptxas", "nvlink"):
        f = root / "bin" / tool
        f.write_text(src)
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (root / "bin" / "refuse.json").write_text(json.dumps(refuse or {}))
    t = root / "targets" / platform
    (t / "include").mkdir(parents=True)
    (t / "lib").mkdir()
    for lib in ("libcudart.so", "libcudart_static.a", "libcudadevrt.a"):
        (t / "lib" / lib).write_text("")
    return root


@pytest.fixture
def tk(tmp_path, monkeypatch):
    root = toolkit(tmp_path)
    monkeypatch.setenv("CUDAToolkit_ROOT", str(root))
    return root


SRC = {"main.cpp": "int main() { return 0; }\n", "k.cu": "__global__ void k() {}\n"}
ARCH = """
gpu_arch_set_code = [["ampere", "sm_80"], ["ampere", "compute_80"]]
config_decl_setting = [["rel", "native.opt", "release"], ["rel", "cuda.arch", "ampere"]]
"""
APP = """
request = [["app", "rel"]]
[target.app]
kind = "executable"
sources = ["main.cpp", "k.cu"]
"""


def project(tmp_path, spec, files=None, packages=None):
    proj = cpp_project(tmp_path, spec, files or SRC, packages)
    base = proj / "spec.toml"
    base.write_text(
        base.read_text().replace(
            'config_decl_setting = [["rel", "native.opt", "release"]]\n',
            "",
        )
    )
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('"buildlog-cpp"', '"buildlog-cpp", "buildlog-cuda"')
        + 'cuda_host_pin = "test fixture pins the CUDA host compiler"\n'
    )
    return proj


LLVM = "/opt/homebrew/opt/llvm/bin"
ALT_CXX = (
    f"{LLVM}/clang++" if host.MACOS else shutil.which("clang++" if ALT_TC == "clang" else "g++")
)


@pytest.fixture
def two_hosts(monkeypatch):
    """A second C++ toolchain next to the default one."""
    if not ALT_CXX or not Path(ALT_CXX).exists():
        pytest.skip(f"needs a second C++ toolchain ({ALT_TC})")
    if host.MACOS:
        monkeypatch.setenv("BUILDLOG_LLVM_BIN", LLVM)
    else:
        monkeypatch.setenv("BUILDLOG_LINUX_TOOLCHAINS", "gcc,clang")


def spec_of(*parts, kinds=None):
    """Top-level relations first, then the target tables."""
    return "\n".join(parts)


def app_with(extra_rel="", extra_tables="", arch=ARCH):
    return (
        arch
        + REQUEST_APP
        + extra_rel
        + """
[target.app]
kind = "executable"
sources = ["main.cpp", "k.cu"]
"""
        + extra_tables
    )


REQUEST_APP = 'request = [["app", "rel"]]\n'


def build_of(tmp_path, spec, **kw):
    proj = project(tmp_path, spec, **kw)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    return build


def fails(tmp_path, spec, capsys, env_unpinned=False):
    proj = project(tmp_path, spec)
    if env_unpinned:
        f = proj / "spec.toml"
        f.write_text(
            "\n".join(x for x in f.read_text().splitlines() if not x.startswith("toolchain_pin"))
        )
    return configure_fails(proj, tmp_path, capsys)


# ---- the compile and the host compiler ------------------------------------------


def test_nvcc_compile_uses_the_configurations_cxx_as_host(tmp_path, tk):
    build = build_of(tmp_path, app_with())
    cmd = cmd_of(build, "k.cu.o")
    assert str(tk / "bin" / "nvcc") in cmd
    assert re.search(r"-ccbin \S*(clang\+\+|g\+\+) ", cmd)
    assert "-x cu -c " in cmd
    assert "--generate-code='arch=compute_80,code=[compute_80,sm_80]'" in cmd
    assert f"-isystem {tk}/targets/{host_platform()}/include" in cmd
    assert "-rdc=true" not in cmd
    assert "libcudart_static.a" in cmd_of(build, "bin/")


def test_two_cxx_toolchains_are_an_ambiguous_host(tmp_path, tk, two_hosts, capsys):
    err = fails(tmp_path, app_with(), capsys, env_unpinned=True)
    assert "ambiguous toolchain" in err


def test_a_pin_names_the_host_compiler(tmp_path, tk, two_hosts):
    build = build_of(tmp_path, app_with(f'cuda_host_pin = [["*", "{ALT_TC}"]]\n'))
    assert f"-ccbin {ALT_CXX} " in cmd_of(build, "k.cu.o")


def test_a_pin_for_a_missing_configuration_is_an_error(tmp_path, tk, capsys):
    err = fails(tmp_path, app_with(f'cuda_host_pin = [["nowhere", "{DEFAULT_TC}"]]\n'), capsys)
    assert "does not exist" in err


def test_a_pin_that_is_no_toolchain_is_an_error(tmp_path, tk, capsys):
    err = fails(tmp_path, app_with('cuda_host_pin = [["*", "ghc"]]\n'), capsys)
    assert "pin is not a C++ toolchain" in err


def test_the_toolkit_refusing_the_host_is_an_error_in_its_own_words(
    tmp_path, monkeypatch, two_hosts, capsys
):
    msg = "#error -- unsupported clang version! clang version must be less than 99."
    root = toolkit(tmp_path, refuse={ALT_FRAGMENT: msg})
    monkeypatch.setenv("CUDAToolkit_ROOT", str(root))
    err = fails(tmp_path, app_with(f'cuda_host_pin = [["*", "{ALT_TC}"]]\n'), capsys)
    assert "host compiler not supported by toolkit" in err
    assert "unsupported clang version! clang version must be less than 99" in err


def test_a_refusal_of_one_host_does_not_touch_another(tmp_path, monkeypatch, two_hosts):
    root = toolkit(tmp_path, refuse={ALT_FRAGMENT: "#error -- unsupported clang version!"})
    monkeypatch.setenv("CUDAToolkit_ROOT", str(root))
    build_of(tmp_path, app_with(f'cuda_host_pin = [["*", "{DEFAULT_TC}"]]\n'))


def test_a_missing_toolkit_is_an_error(tmp_path, monkeypatch, capsys):
    if Path("/usr/local/cuda/bin/nvcc").exists():
        pytest.skip("a toolkit is installed at /usr/local/cuda, the last place looked")
    monkeypatch.setenv("CUDAToolkit_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    err = fails(tmp_path, app_with(), capsys)
    assert "CUDA toolkit not found" in err


def test_no_cuda_source_asks_nothing_of_the_toolkit(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDAToolkit_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    spec = """request = [["app", "rel"]]
[target.app]
kind = "executable"
sources = ["main.cpp"]
"""
    build_of(tmp_path, spec)


# ---- architectures ---------------------------------------------------------------


def test_a_configuration_with_cuda_and_no_set_is_an_open_decision(tmp_path, tk, capsys):
    err = fails(
        tmp_path,
        app_with(arch='config_decl_setting = [["rel", "native.opt", "release"]]\n'),
        capsys,
    )
    assert "no GPU architecture set" in err


def test_a_set_spells_one_gencode_per_virtual_architecture(tmp_path, tk):
    arch = """
gpu_arch_set_code = [["multi", "sm_80"], ["multi", "compute_80"], ["multi", "sm_90"], ["multi", "sm_100"], ["multi", "compute_100"]]
config_decl_setting = [["rel", "native.opt", "release"], ["rel", "cuda.arch", "multi"]]
"""
    cmd = cmd_of(build_of(tmp_path, app_with(arch=arch)), "k.cu.o")
    assert "--generate-code='arch=compute_80,code=[compute_80,sm_80]'" in cmd
    assert "--generate-code=arch=compute_90,code=sm_90" in cmd
    assert "--generate-code='arch=compute_100,code=[compute_100,sm_100]'" in cmd


def test_an_unknown_set_and_a_malformed_code_are_errors(tmp_path, tk, capsys):
    err = fails(tmp_path, app_with('cuda_arch_of = [["app", "nosuch"]]\n'), capsys)
    assert "not declared" in err
    arch = """
gpu_arch_set_code = [["bad", "sm_eighty"]]
config_decl_setting = [["rel", "native.opt", "release"], ["rel", "cuda.arch", "bad"]]
"""
    err = (
        fails(tmp_path / "b", app_with(arch=arch), capsys)
        if (tmp_path / "b").mkdir() is None
        else ""
    )
    assert "not a GPU code" in err


def test_a_code_the_toolkit_does_not_list_is_an_error(tmp_path, tk, capsys):
    arch = """
gpu_arch_set_code = [["volta", "sm_70"], ["volta", "compute_70"]]
config_decl_setting = [["rel", "native.opt", "release"], ["rel", "cuda.arch", "volta"]]
"""
    err = fails(tmp_path, app_with(arch=arch), capsys)
    assert "GPU architecture not supported by the CUDA toolkit" in err
    assert "sm_70" in err


# ---- RDC and the device link -----------------------------------------------------

LIB = """
[target.lib]
kind = "library"
sources = ["lib.cu"]
"""
RDC_FILES = {
    **SRC,
    "lib.cu": "__device__ int f() { return 1; }\n",
    "lib2.cu": "__device__ int g() { return 2; }\n",
}


def test_rdc_objects_of_an_executable_are_device_linked_into_it(tmp_path, tk):
    build = build_of(tmp_path, app_with('cuda_rdc = [["app"]]\n'))
    assert "-rdc=true" in cmd_of(build, "k.cu.o")
    dl = cmd_of(build, "cuda_dlink.o")
    assert (
        "-dlink" in dl and "-ccbin" in dl and "obj/arm64-macos.release.none.ampere/app/k.cu.o" in dl
    )
    assert "app/cuda_dlink.o" in cmd_of(build, "bin/")


def test_no_rdc_means_no_device_link(tmp_path, tk):
    assert "cuda_dlink" not in ninja_text(build_of(tmp_path, app_with()))


def test_rdc_in_a_static_library_is_device_linked_at_the_executable(tmp_path, tk):
    spec = app_with('cuda_rdc = [["lib"]]\ndep = [["app", "lib", "private"]]\n', LIB)
    build = build_of(tmp_path, spec, files=RDC_FILES)
    text = ninja_text(build)
    assert "lib/cuda_dlink.o" not in text
    dl = cmd_of(build, "app/cuda_dlink.o")
    assert "liblib.a" in dl
    assert "app/cuda_dlink.o" in cmd_of(build, "bin/")


def test_a_shared_library_is_a_boundary_for_the_device_link(tmp_path, tk):
    rel = 'cuda_rdc = [["lib"], ["inner"]]\ndep = [["app", "lib", "private"], ["lib", "inner", "private"]]\ndefault_linkage = [["lib", "shared"]]\n'
    inner = """
[target.inner]
kind = "library"
sources = ["lib2.cu"]
"""
    build = build_of(tmp_path, app_with(rel, LIB + inner), files=RDC_FILES)
    text = ninja_text(build)
    assert "libinner.a" in cmd_of(build, "lib/cuda_dlink.o")
    assert "app/cuda_dlink.o" not in text


def test_a_static_library_may_state_it_resolves_device_symbols(tmp_path, tk):
    rel = 'cuda_rdc = [["lib"]]\ncuda_resolve_device_symbols = [["lib"]]\ndep = [["app", "lib", "private"]]\n'
    build = build_of(tmp_path, app_with(rel, LIB), files=RDC_FILES)
    text = ninja_text(build)
    assert "lib/cuda_dlink.o" in text
    assert "app/cuda_dlink.o" not in text


def test_resolving_device_symbols_is_for_static_libraries(tmp_path, tk, capsys):
    rel = 'cuda_rdc = [["app"]]\ncuda_resolve_device_symbols = [["app"]]\n'
    assert "not a static library" in fails(tmp_path, app_with(rel), capsys)


def test_device_code_in_an_object_library_is_an_error(tmp_path, tk, capsys):
    lib = LIB.replace("library", "object_library")
    rel = 'cuda_rdc = [["lib"]]\ndep = [["app", "lib", "private"]]\n'
    assert "object library" in fails(tmp_path, app_with(rel, lib), capsys)


WIDE = """
gpu_arch_set_code = [["ampere", "sm_80"], ["ampere", "compute_80"], ["wide", "sm_80"], ["wide", "compute_80"], ["wide", "sm_90"], ["hopper", "sm_90"]]
config_decl_setting = [["rel", "native.opt", "release"], ["rel", "cuda.arch", "ampere"]]
"""


def test_a_device_link_that_drops_architectures_is_an_error(tmp_path, tk, capsys):
    rel = 'cuda_rdc = [["lib"], ["app"]]\ncuda_arch_of = [["lib", "wide"]]\ndep = [["app", "lib", "private"]]\n'
    err = fails(tmp_path, app_with(rel, LIB, arch=WIDE), capsys)
    assert "device link drops architectures" in err and "sm_90" in err


def test_a_stated_drop_is_a_fact_not_an_error(tmp_path, tk):
    rel = (
        'cuda_rdc = [["lib"], ["app"]]\ncuda_arch_of = [["lib", "wide"]]\n'
        'dep = [["app", "lib", "private"]]\ncuda_dlink_drops = [["app", "sm_90"]]\n'
    )
    build_of(tmp_path, app_with(rel, LIB, arch=WIDE), files=RDC_FILES)


def test_an_object_that_does_not_serve_the_link_is_an_error(tmp_path, tk, capsys):
    rel = 'cuda_rdc = [["lib"], ["app"]]\ncuda_arch_of = [["lib", "hopper"]]\ndep = [["app", "lib", "private"]]\n'
    err = fails(tmp_path, app_with(rel, LIB, arch=WIDE), capsys)
    assert "does not serve" in err


# ---- the runtime -------------------------------------------------------------------


def test_the_runtime_is_static_by_default_and_none_adds_nothing(tmp_path, tk):
    link = cmd_of(build_of(tmp_path, app_with()), "bin/")
    assert "libcudart_static.a" in link and "libcudart.so" not in link
    link = (
        cmd_of(build_of(tmp_path / "n", app_with('cuda_runtime = [["app", "none"]]\n')), "bin/")
        if (tmp_path / "n").mkdir() is None
        else ""
    )
    assert "libcudart" not in link


def test_the_shared_runtime_is_a_fact_about_the_target(tmp_path, tk):
    link = cmd_of(build_of(tmp_path, app_with('cuda_runtime = [["app", "shared"]]\n')), "bin/")
    assert "libcudart.so" in link and "libcudart_static" not in link


def test_two_runtimes_in_one_link_are_an_error(tmp_path, tk, capsys):
    rel = 'cuda_runtime = [["lib", "shared"]]\ndep = [["app", "lib", "private"]]\n'
    err = fails(tmp_path, app_with(rel, LIB), capsys)
    assert "runtime mismatch" in err


def test_a_cpp_target_that_calls_the_runtime_states_its_own(tmp_path, tk):
    rel = 'cuda_runtime = [["host", "static"]]\ndep = [["app", "host", "private"]]\n'
    host_lib = """
[target.host]
kind = "library"
sources = ["util.cpp"]
"""
    files = {**SRC, "util.cpp": "int u() { return 0; }\n"}
    build = build_of(tmp_path, app_with(rel, host_lib), files=files)
    assert "libcudart_static.a" in cmd_of(build, "bin/")


@pytest.mark.skip(
    reason="no NVIDIA GPU on this machine; running CUDA programs is deferred to the GPU machine"
)
def test_cuda_programs_run_on_a_gpu():
    raise AssertionError("deferred")


# ---- composing with other plugins' settings --------------------------------------


@pytest.mark.parametrize("other", ["buildlog-emsdk", "buildlog-python"])
def test_cuda_composes_with_a_plugin_claiming_the_same_setting_priority(tmp_path, tk, other):
    """cuda.arch, emscripten.simd and python.interpreter are all priority 3: the
    settings list is ordered by (priority, key), so none of them collides."""
    proj = project(tmp_path, app_with())
    toml = proj / "buildlog.toml"
    toml.write_text(toml.read_text().replace('"buildlog-cuda"', f'"buildlog-cuda", "{other}"'))
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert "--generate-code='arch=compute_80,code=[compute_80,sm_80]'" in cmd_of(build, "k.cu.o")


# ---- include directories go through buildlog-cpp's relations ---------------------


def test_nvcc_include_dirs_follow_the_include_priority_facts(tmp_path, tk):
    spec = app_with(
        extra_rel=(
            'prop = [["app", "cc.system_include_dir", "src:aa_sys", "private"],'
            ' ["app", "cc.system_include_dir", "src:zz_sys", "private"]]\n'
            'cc_include_before = [["src:zz_sys", "src:aa_sys"]]\n'
        )
    )
    cmd = cmd_of(build_of(tmp_path, spec), "k.cu.o")
    assert cmd.index("zz_sys") < cmd.index("aa_sys")
