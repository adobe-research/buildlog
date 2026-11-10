# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: the tier-2 LLVM, Clang and LLD packages
(case-studies/halide/llvm-install, imports of Homebrew's own CMake exports plus
a shipped layer of facts and rules) and the halide-build package
(case-studies/halide/packages) against Homebrew's llvm@22.

Each test configures a small project (not libHalide itself, which needs the
Halide tree): the eight backends, a consumer of halide-build::llvm. The error
fixtures copy an overlay's share/buildlog into a tmp prefix with edited facts
(the lib/include/bin directories stay llvm@22's).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.engine import configure_project
from buildlog.errors import LoaderError
from buildlog.paths import souffle_path

from .test_why import spec_line

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "case-studies" / "halide" / "packages"
HB = "/opt/homebrew/opt"
LLVM22 = Path(os.path.realpath(f"{HB}/llvm@22"))
FACTS = "case-studies/halide/llvm-install/homebrew-llvm-22/share/buildlog/LLVM/facts.toml"
INSTALL = ROOT / "case-studies" / "halide" / "llvm-install"
OVERLAY_LLVM = INSTALL / "homebrew-llvm-22"
OVERLAY_LLD = INSTALL / "homebrew-lld-22"
OVERLAY_LLVM21 = INSTALL / "homebrew-llvm-21"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (LLVM22 / "lib/cmake/llvm/LLVMConfig.cmake").exists()
        or not os.path.isdir(f"{HB}/lld@22"),
        reason="Homebrew llvm@22 and lld@22 not installed",
    ),
]

BACKENDS = ["AArch64", "ARM", "Hexagon", "NVPTX", "PowerPC", "RISCV", "WebAssembly", "X86"]


@pytest.fixture(autouse=True)
def _package_path(monkeypatch):
    monkeypatch.setenv("BUILDLOG_PACKAGE_PATH", str(PACKAGES))


def project(tmp_path, bindings, extra=""):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.cpp").write_text("int main() { return 0; }\n")
    (proj / "buildlog.toml").write_text(
        'uses = ["buildlog-cpp", "halide-build"]\n'
        f"source_root = {json.dumps(str(proj))}\n"
        'spec = ["spec.toml"]\n'
    )
    backends = ", ".join(f'["{b}", "WITH_{b.upper()}"]' for b in BACKENDS)
    (proj / "spec.toml").write_text(
        f"""
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
halide_build_llvm_backend = [{backends}]
target = [["consumer"]]
native_kind = [["consumer", "executable"]]
cxx_source = [["consumer", "main.cpp"]]
dep = [["consumer", "halide-build::llvm", "private"]]
request = [["consumer", "release"]]
"""
        + extra
    )
    (proj / "user.toml").write_text(
        'toolchain_pin = [["c", "arm64-macos", "arm64-macos", "apple-clang"], '
        '["cxx", "arm64-macos", "arm64-macos", "apple-clang"], '
        '["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]\n' + bindings
    )
    return proj


def pins(*prefixes, linkage="shared"):
    rows = ", ".join(
        f'["buildlog", {json.dumps(str(Path(p) / "share/buildlog"))}]' for p in prefixes
    )
    return f'package_search_dir = [{rows}]\npackage_linkage = [["LLVM", "{linkage}"]]\n'


def configure_fails(proj, tmp_path, capsys):
    with pytest.raises(SystemExit):
        configure_project(
            proj, tmp_path / "build", update_lock=True, user_files=[proj / "user.toml"]
        )
    return capsys.readouterr().err


def why_leaves(tmp_path, proj, capsys, *facts):
    capsys.readouterr()
    assert main(["why", "-C", str(tmp_path / "build"), *facts, "--leaves", str(proj)]) == 0
    return capsys.readouterr().out


def fake_llvm(tmp_path, targets, clang_requires="22.1.8"):
    """A prefix with the overlay's share/buildlog (symlinks resolved) whose
    facts state `targets` as the backends built and whose Clang manifest
    requires LLVM ==`clang_requires`; lib/include/bin are llvm@22's."""
    prefix = tmp_path / "fake-llvm"
    shutil.copytree(OVERLAY_LLVM / "share", prefix / "share")
    for d in ("lib", "include", "bin"):
        (prefix / d).symlink_to(LLVM22 / d)
    facts = prefix / "share/buildlog/LLVM/facts.toml"
    text = re.sub(r"llvm_target = \[.*?\n\]\n", "", facts.read_text(), flags=re.DOTALL)
    rows = ", ".join(f'["{t}"]' for t in targets)
    facts.write_text(text + f"llvm_target = [{rows}]\n")
    clang = prefix / "share/buildlog/Clang/manifest.toml"
    clang.write_text(clang.read_text().replace("==22.1.8", f"=={clang_requires}"))
    return prefix


def test_llvm22_binds_and_its_definitions_explain_to_the_packages_facts(tmp_path, capsys):
    proj = project(tmp_path, pins(OVERLAY_LLVM, OVERLAY_LLD))
    build = tmp_path / "build"
    _, metrics = configure_project(proj, build, update_lock=True, user_files=[proj / "user.toml"])
    assert metrics["errors"] == []
    facts = build / "souffle" / "facts"
    assert (facts / "package_linkage_selected.csv").read_text() == "LLVM\tarm64-macos\tshared\n"
    assert "LLVM\tarm64-macos\t22\t1\t8" in (facts / "package_version.csv").read_text()
    text = (build / "souffle" / "build.ninja").read_text()
    for token in [
        "-DLLVM_VERSION=221",
        "-D__STDC_LIMIT_MACROS",
        "-DWITH_WEBASSEMBLY",
        f"-isystem {OVERLAY_LLVM}/include",
    ]:
        assert token in text
    assert "_GLIBCXX_ASSERTIONS" not in text
    facts_file = OVERLAY_LLVM / "share/buildlog/LLVM/facts.toml"
    line = next(
        i
        for i, ln in enumerate(facts_file.read_text().splitlines(), 1)
        if ln.startswith("llvm_define = ")
    )
    out = why_leaves(tmp_path, proj, capsys, "--flag", "-D__STDC_LIMIT_MACROS")
    assert f"{FACTS}:{line + 3} (package LLVM)" in out
    out = why_leaves(tmp_path, proj, capsys, "--flag", "-DLLVM_VERSION=221")
    version_line = spec_line(facts_file, "llvm_version = ")
    assert f"{FACTS}:{version_line} (package LLVM)" in out


def test_missing_webassembly_is_an_explainable_error_not_a_downgrade(tmp_path, capsys):
    prefix = fake_llvm(tmp_path, [b for b in BACKENDS if b != "WebAssembly"])
    proj = project(tmp_path, pins(prefix, OVERLAY_LLD))
    err = configure_fails(proj, tmp_path, capsys)
    detail = (
        "WebAssembly (WITH_WEBASSEMBLY) is enabled but the bound LLVM does not build it "
        "(LLVM_TARGETS_TO_BUILD)"
    )
    assert f"Halide LLVM backend not built: {detail}" in err
    out = why_leaves(tmp_path, proj, capsys, f'error("Halide LLVM backend not built", "{detail}")')
    assert 'halide_build_llvm_backend("WebAssembly", "WITH_WEBASSEMBLY")' in out
    # The LLVM it was checked against: its facts file, at the version line.
    version_line = spec_line(prefix / "share/buildlog/LLVM/facts.toml", "llvm_version = ")
    assert re.search(
        rf'llvm_version\("22", "1", "8"\)  ← \S*fake-llvm\S*facts\.toml:{version_line}\b', out
    )


def test_conflicting_available_llvm_code_is_rejected_before_binding(tmp_path):
    proj = project(tmp_path, pins(OVERLAY_LLVM21, OVERLAY_LLVM, OVERLAY_LLD))
    with pytest.raises(LoaderError) as exc:
        configure_project(
            proj, tmp_path / "build", update_lock=True, user_files=[proj / "user.toml"]
        )
    assert any(
        e.startswith("LLVM: found twice") and "homebrew-llvm-21/" in e and "homebrew-llvm-22/" in e
        for e in exc.value.errors
    )
    assert not (tmp_path / "build" / "program" / "build.dl").exists()


def test_rtti_off_consumer_of_an_rtti_llvm_is_an_abi_error(tmp_path, capsys):
    extra = 'prop = [["consumer", "cxx.rtti", "off", "private"]]\n'
    proj = project(tmp_path, pins(OVERLAY_LLVM, OVERLAY_LLD), extra)
    err = configure_fails(proj, tmp_path, capsys)
    detail = "consumer is built with cxx.rtti=off but uses LLVM::LLVM, built with cxx.rtti=on"
    assert f"C++ RTTI mismatch: {detail}" in err
    out = why_leaves(tmp_path, proj, capsys, f'error("C++ RTTI mismatch", "{detail}")')
    assert 'prop("consumer", "cxx.rtti", "off", "private")' in out
    facts_file = OVERLAY_LLVM / "share/buildlog/LLVM/facts.toml"
    line = next(
        i
        for i, ln in enumerate(facts_file.read_text().splitlines(), 1)
        if ln.startswith("llvm_enable_rtti")
    )
    assert f'llvm_enable_rtti("on")  ← {FACTS}:{line} (package LLVM)' in out


def test_exceptions_are_directional_against_llvm(tmp_path):
    # Halide builds with exceptions; LLVM without. Allowed (and libHalide's case).
    extra = 'prop = [["consumer", "cxx.exceptions", "on", "private"]]\n'
    proj = project(tmp_path, pins(OVERLAY_LLVM, OVERLAY_LLD), extra)
    _, metrics = configure_project(
        proj, tmp_path / "build", update_lock=True, user_files=[proj / "user.toml"]
    )
    assert metrics["errors"] == []


# A runtime, so that Clang runs (only the Clang a configuration runs is
# checked: the exec configuration's, against the configuration's LLVM).
def with_runtime(proj):
    spec = proj / "spec.toml"
    text = spec.read_text()
    for old, new in [
        ('target = [["consumer"]]', 'target = [["consumer"], ["embed"]]'),
        (
            'native_kind = [["consumer", "executable"]]',
            'native_kind = [["consumer", "executable"], ["embed", "executable"]]',
        ),
        (
            'cxx_source = [["consumer", "main.cpp"]]',
            'cxx_source = [["consumer", "main.cpp"], ["embed", "main.cpp"]]',
        ),
    ]:
        text = text.replace(old, new)
    spec.write_text(
        text + 'halide_build_runtime = [["consumer", "embed", "rt", "22.0.0", "22", "0", "0"]]\n'
    )
    return proj


def test_clang_of_another_version_is_an_error(tmp_path):
    # The Clang package states the LLVM it was built against as a requirement.
    prefix = fake_llvm(tmp_path, BACKENDS, clang_requires="21.1.8")
    proj = with_runtime(project(tmp_path, pins(prefix, OVERLAY_LLD)))
    with pytest.raises(LoaderError, match=r"Clang: requires LLVM ==21\.1\.8, but LLVM 22\.1\.8"):
        configure_project(
            proj, tmp_path / "build", update_lock=True, user_files=[proj / "user.toml"]
        )


def test_unused_clang_still_must_satisfy_the_executable_union(tmp_path):
    prefix = fake_llvm(tmp_path, BACKENDS, clang_requires="21.1.8")
    proj = project(tmp_path, pins(prefix, OVERLAY_LLD))
    with pytest.raises(LoaderError, match=r"Clang: requires LLVM ==21\.1\.8, but LLVM 22\.1\.8"):
        configure_project(proj, tmp_path / "build", emit=False, user_files=[proj / "user.toml"])


ZSTD_CMAKE = "/opt/homebrew/lib/cmake/zstd/zstdTargets.cmake"
X86_MAIN = """#include <llvm/MC/TargetRegistry.h>
#include <llvm/Support/TargetSelect.h>
#include <llvm/TargetParser/Triple.h>
#include <cstdio>
#include <string>
int main() {
  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86Target();
  LLVMInitializeX86TargetMC();
  std::string err;
  const llvm::Target *t = llvm::TargetRegistry::lookupTarget(llvm::Triple("x86_64-unknown-linux-gnu"), err);
  std::printf("%s\\n", t ? t->getName() : err.c_str());
  return t ? 0 : 1;
}
"""


def static_project(tmp_path, linkage_rows):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.cpp").write_text(X86_MAIN)
    (proj / "buildlog.toml").write_text(
        f'uses = ["buildlog-cpp"]\nsource_root = {json.dumps(str(proj))}\nspec = ["spec.toml"]\n'
    )
    (proj / "spec.toml").write_text(
        """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
target = [["app"], ["system-zlib"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
dep = [["app", "LLVM:X86", "private"], ["app", "LLVM:Config", "private"]]
prop = [["app", "cxx.std", "17", "private"], ["system-zlib", "native.link_flag", "-lz", "interface"]]
request = [["app", "release"]]
"""
    )
    (proj / "user.toml").write_text(
        f"""
toolchain_pin = [["c", "arm64-macos", "arm64-macos", "apple-clang"], ["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]
package_search_dir = [["buildlog", "{OVERLAY_LLVM}/share/buildlog"]]
{linkage_rows}
# The static component libraries link zstd::libzstd_shared and ZLIB::ZLIB.
package_file = [["cmake", "{ZSTD_CMAKE}"]]
alias = [["ZLIB::ZLIB", "system-zlib"]]
"""
    )
    return proj


@pytest.mark.skipif(not os.path.exists(ZSTD_CMAKE), reason="Homebrew zstd not installed")
def test_static_linkage_links_the_component_libraries(tmp_path):
    import subprocess

    proj = static_project(tmp_path, 'package_linkage = [["LLVM", "static"]]')
    build = tmp_path / "build"
    _, metrics = configure_project(proj, build, update_lock=True, user_files=[proj / "user.toml"])
    assert metrics["errors"] == []
    text = (build / "souffle" / "build.ninja").read_text()
    assert (
        f"{OVERLAY_LLVM}/lib/libLLVMX86CodeGen.a" in text
        and f"{OVERLAY_LLVM}/lib/libLLVMSupport.a" in text
    )
    assert "libLLVM.dylib" not in text
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    exe = next((build / "souffle" / "bin").glob("*/app"))
    assert subprocess.check_output([str(exe)], text=True) == "x86-64\n"
    assert "libLLVM" not in subprocess.check_output(["otool", "-L", str(exe)], text=True)


@pytest.mark.skipif(not os.path.exists(ZSTD_CMAKE), reason="Homebrew zstd not installed")
def test_unpinned_linkage_is_an_error_when_both_are_offered(tmp_path, capsys):
    err = configure_fails(static_project(tmp_path, ""), tmp_path, capsys)
    assert (
        "package linkage not chosen: LLVM on arm64-macos offers static and shared libraries" in err
    )
