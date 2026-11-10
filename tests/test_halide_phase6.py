# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: the sampled tests.

case-studies/halide/libhalide/tests.toml declares the 32 sampled ctest tests
(baseline/scope.toml) and what they need: the interface targets for the test
directories' settings, 24 Halide libraries (add_halide_library: multitarget,
features, params, plugins, autoschedulers, gradients, C backend,
registration), their runtimes (implicit and derived, add_halide_runtime),
two generator stubs and the externs libraries. Every generator, plugin and
gengen they run is named by reference, so it is built for the exec
configuration without an annotation. Needs the pinned Halide export,
Homebrew's llvm@22, flatbuffers, libpng and libjpeg; the comparison also
needs the baseline. BUILDLOG_HALIDE_BUILD=1 also builds the graph and runs
the tests (`buildlog test`), as ctest does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.paths import souffle_path

from .halide_env import HALIDE_SRC, configure_libhalide, project_copy

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "halide"
LIBHALIDE = CASE / "libhalide"
PACKAGES = CASE / "packages"
SRC = HALIDE_SRC
REL = "arm64-macos.release.none.none"
CFG = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], [[python.interpreter, none], nil]]]]]"
CFG_WHY = '["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], [["python.interpreter", "none"], nil]]]]]'

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "test/generator/alias_generator.cpp").exists(),
        reason="the pinned Halide export is not present",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/opt/homebrew/opt/llvm@22")
        or not os.path.exists("/opt/homebrew/bin/flatc")
        or not os.path.isdir("/opt/homebrew/opt/libpng")
        or not os.path.isdir("/opt/homebrew/opt/jpeg-turbo"),
        reason="Homebrew llvm@22, flatbuffers, libpng or jpeg-turbo not installed",
    ),
]


@pytest.fixture(autouse=True)
def _package_path(monkeypatch):
    monkeypatch.setenv("BUILDLOG_PACKAGE_PATH", str(PACKAGES))


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    os.environ["BUILDLOG_PACKAGE_PATH"] = str(PACKAGES)
    try:
        build, metrics = configure_libhalide(tmp_path_factory)
    finally:
        del os.environ["BUILDLOG_PACKAGE_PATH"]
    assert metrics["errors"] == []
    return build


def rows(path):
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def cmd_of(ninja: str, out: str) -> str:
    m = re.search(rf"^build {re.escape(out)}[ :](?:.*\n)*?  cmd = (.*)$", ninja, re.MULTILINE)
    assert m, out
    return m[1]


def test_generators_plugins_and_stubs_are_exec_by_inference(build):
    tool_dep = rows(build / "souffle" / "facts" / "tool_dep.csv")
    assert {
        ("alias", CFG, "alias.generator"),
        ("alias_Adams2019", CFG, "autoschedule_adams2019"),
        ("alias_Li2018", CFG, "autoschedule_li2018"),
        ("autograd_grad", CFG, "autograd.generator"),
        ("autograd_grad", CFG, "autoschedule_mullapudi2016"),
        ("stubtest.stub", CFG, "stubtest.generator"),
        ("multitarget.runtime", CFG, "gengen"),
        ("rniso_rt_a", CFG, "gengen"),
        ("adams2019_demo", CFG, "adams2019_demo.generator"),
        ("adams2019_demo", CFG, "autoschedule_adams2019"),
    } <= tool_dep
    # Native build: exec is the configuration itself, so each tool is built
    # once, and the tests' programs are not tools.
    exec_config = rows(build / "souffle" / "facts" / "exec_config.csv")
    assert (CFG, CFG) in exec_config
    assert not any(g.startswith(("correctness_", "generator_aot")) for _, _, g in tool_dep)


@pytest.mark.skipif(
    not (ROOT / "build/py310/bin/python").exists(), reason="Halide Python environment not installed"
)
def test_python_preset_shares_halide_with_native_generators(tmp_path):
    from buildlog.engine import configure_project

    project = tmp_path / "libhalide"
    shutil.copytree(
        LIBHALIDE, project, ignore=shutil.ignore_patterns("buildlog.lock", "__pycache__")
    )
    build = tmp_path / "build"
    _, metrics = configure_project(
        project,
        build,
        user_files=[
            project / "presets/homebrew-arm64.toml",
            project / "presets/python.toml",
        ],
    )
    assert metrics["errors"] == []
    facts = build / "souffle/facts"
    needed = rows(facts / "needed.csv")
    (cfg,) = {c for t, c in needed if t == "Halide"}
    assert (cfg, "python.interpreter", "py310") in rows(facts / "config_setting.csv")
    assert (cfg, cfg) in rows(facts / "exec_config.csv")
    assert {("Halide_PyStubs", cfg), ("Python::Module", cfg)} <= needed
    assert {
        ("autograd_grad", cfg, "autograd.generator"),
        ("autograd_grad", cfg, "autoschedule_mullapudi2016"),
    } <= rows(facts / "tool_dep.csv")


def test_generator_commands_match_add_halide_library(build):
    ninja = (build / "souffle" / "build.ninja").read_text()
    g = f"gen/{REL}"
    # Multitarget: the wrapper names the action, one object per target, the
    # target list comma-joined in the declared order.
    mt = cmd_of(ninja, f"{g}/multitarget/multitarget_wrapper.o")
    assert mt.endswith(
        " -o . target=arm-64-macos-no_bounds_query-c_plus_plus_name_mangling-no_runtime,"
        "arm-64-macos-c_plus_plus_name_mangling-no_runtime"
    )
    assert re.search(
        rf"^build {g}/multitarget/multitarget_wrapper\.o \| .*"
        rf"{g}/multitarget/multitarget-arm-64-macos-c_plus_plus_name_mangling-no_runtime\.o",
        ninja,
        re.MULTILINE,
    )
    # The derived runtime: the users' targets without the per-library
    # features (no_bounds_query, no_runtime, ...), deduplicated.
    assert cmd_of(ninja, f"{g}/multitarget.runtime/multitarget.runtime.o").endswith(
        "gengen -r multitarget.runtime -o . -e object target=arm-64-macos-c_plus_plus_name_mangling"
    )
    # Gradient, an autoscheduler (appended to the plugins, as CMake does,
    # so the same plugin appears twice) and "host" on a native build.
    ag = cmd_of(ninja, f"{g}/autograd_grad/autograd_grad.o")
    so = f"../../../lib/{REL}/libautoschedule_mullapudi2016.so"
    assert " -d 1 -g autograd " in ag
    assert f" -p {so},{so} -o . target=host-no_runtime autoscheduler=Mullapudi2016" in ag
    # C backend: the generated C++ names the action and is compiled with -fPIC.
    cpp = f"{g}/example_cpp/example_cpp.halide_generated.cpp"
    assert " -e c_header,c_source,function_info_header " in cmd_of(ninja, cpp)
    obj = cmd_of(ninja, f"obj/{REL}/example_cpp/{cpp}.o")
    assert " -fPIC " in obj and " -Wno-psabi " in obj
    # Params, and a runtime of its own with params.
    assert cmd_of(ninja, f"{g}/rniso_rt_a/rniso_rt_a.o").endswith(
        "target=host runtime_prefixes.export=runtime_a_ runtime_prefixes.internal=runtime_ai_"
    )
    # A stub: an interface target whose header the generator writes.
    assert cmd_of(ninja, f"{g}/stubtest.stub/stubtest.stub.h").endswith(
        f"bin/{REL}/stubtest.generator -g stubtest -n stubtest -o . -e cpp_stub"
    )


@pytest.mark.parametrize(
    ("target", "source"),
    [
        ("lesson_01_basics", "lesson_01_basics"),
        ("lesson_10_aot_compilation_run", "lesson_10_aot_compilation_run"),
        ("lesson_15_generate", "lesson_15_generators"),
        ("lesson_16_rgb_run", "lesson_16_rgb_run"),
    ],
)
def test_tutorials_keep_release_flags_instead_of_test_directory_defaults(build, target, source):
    ninja = (build / "souffle" / "build.ninja").read_text()
    compile_cmd = cmd_of(ninja, f"obj/{REL}/{target}/tutorial/{source}.cpp.o")
    link_cmd = cmd_of(ninja, f"bin/{REL}/{target}")
    assert " -std=c++17 " in compile_cmd
    for command in (compile_cmd, link_cmd):
        assert " -O3 " in command and " -DNDEBUG " in command
        assert "-fvisibility=hidden" not in command
        assert "-fvisibility-inlines-hidden" not in command


def test_why_explains_a_plugins_exec_use(build, capsys):
    capsys.readouterr()
    fact = f'tool_dep("autograd_grad", {CFG_WHY}, "autoschedule_mullapudi2016")'
    assert main(["why", "-C", str(build), fact, str(project_copy(build))]) == 0
    out = capsys.readouterr().out
    first, second = out.splitlines()[:2]
    assert first.startswith(f'tool_dep("autograd_grad", ‹{REL}›, "autoschedule_mullapudi2016")')
    assert '"autoschedule_mullapudi2016", "load")' in second and "tool_ref(" in second
    assert 'action_run_item("out:' in out
    assert 'halide_build_library("autograd_grad"' in out and "libhalide/generators.toml:" in out


def test_phase6_comparison(build, tmp_path):
    out = tmp_path / "cmp.json"
    subprocess.run(
        [sys.executable, str(CASE / "phase1/compare_libhalide.py"), str(build), "--phase", "6",
         "--details", "0", "--json", str(out)],
        check=False, capture_output=True,
    )  # fmt: skip
    doc = json.loads(out.read_text())
    counts = {(r["phase"], r["kind"]): r for r in doc["rows"]}
    # Every CMake command of the sample has a buildlog counterpart except
    # the PCH and example.stub.h (generator_jit_example's, not declared).
    assert counts[(6, "custom")] == {"phase": 6, "kind": "custom", "differ": 46, "only_a": 1}
    assert counts[(6, "compile")] == {"phase": 6, "kind": "compile", "differ": 57}
    assert counts[(6, "link")] == {"phase": 6, "kind": "link", "differ": 46}
    assert counts[(6, "archive")] == {"phase": 6, "kind": "archive", "differ": 46}
    assert counts[(6, "pch")] == {"phase": 6, "kind": "pch", "only_a": 1}
    assert counts[(6, "phony")] == {"phase": 6, "kind": "phony", "only_a": 335}
    assert {
        "$BUILD/test/CMakeFiles/_test_internal.dir/cmake_pch.hxx.pch",
        "$BUILD/test/generator/example.stub.h",
    } <= set(doc["only_a"])
    assert doc["only_b"] == []


def test_standard_flags_follow_cmake_per_directory(build):
    ninja = (build / "souffle" / "build.ninja").read_text()
    std = re.compile(r"(?<= )(-O3|-DNDEBUG)(?= )")
    # src/ and tools keep the configuration's -O3 -DNDEBUG, on links too (CMake passes
    # CMAKE_CXX_FLAGS_RELEASE to the compiler driver); test/ wipes both (wipe_standard_flags).
    for out in (f"lib/{REL}/libHalide.22.0.0.dylib", f"bin/{REL}/regexp_replace"):
        assert len(set(std.findall(cmd_of(ninja, out)))) == 2, out
    for out in (
        f"bin/{REL}/_test_internal",
        f"obj/{REL}/_test_internal/test/pch_helper.cpp.o",
        f"obj/{REL}/Halide_terminate_handler/test/common/terminate_handler.cpp.o",
    ):
        assert not std.search(cmd_of(ninja, out)), out
    assert std.search(cmd_of(ninja, f"obj/{REL}/Halide/src/Util.cpp.o"))


def test_the_sampled_tests_are_typed_facts(build):
    """add_halide_test's properties, derived by halide-build over core's
    test relations: labels, HL_TARGET (host: a native build), the SKIP and
    Success! regexes, RUN_SERIAL for multithreaded, a later ENVIRONMENT that
    replaces the default, and the plugin ARG as a target reference."""
    doc = json.loads((build / "souffle" / "tests.json").read_text())
    by = {t["name"]: t for t in doc["tests"]}
    assert {t["config"] for t in by.values()} == {REL}
    auto = by["correctness_auto_schedule_no_parallel"]
    assert auto["argv"] == [
        f"../../../bin/{REL}/correctness_auto_schedule_no_parallel",
        f"../../../lib/{REL}/libautoschedule_mullapudi2016.so",
    ]
    assert f"lib/{REL}/libautoschedule_mullapudi2016.so" in auto["needs"]
    assert auto["env"] == {"HL_JIT_TARGET": "host", "HL_TARGET": "host"}
    assert auto["pass"] == ["Success!"]
    assert auto["skip"] == [r"\[SKIP(-WITH-ISSUE(-[0-9]+)?)?\]"]
    assert auto["labels"] == ["correctness"] and not auto["serial"]
    assert by["generator_aot_multitarget_1"]["env"] == {
        "HL_MULTITARGET_TEST_USE_NOBOUNDSQUERY_FEATURE": "1"
    }
    demo = by["adams2019_demo_apps_autoscheduler"]
    assert demo["argv"][1:] == [
        "--benchmarks=all", "--benchmark_min_time=1", "--estimate_all", "--success",
    ]  # fmt: skip
    assert demo["labels"] == ["adams2019", "autoschedulers_cpu", "multithreaded"]
    assert {
        "adams2019_demo_apps_autoscheduler",
        "correctness_extern_stage",
        "correctness_simd_op_check_arm",
        "generator_aot_alias",
        "generator_aot_autograd",
        "generator_aot_user_context",
        "generator_aotcpp_example",
    } <= {n for n, t in by.items() if t["serial"]}


@pytest.mark.skipif(
    not os.environ.get("BUILDLOG_HALIDE_BUILD"), reason="set BUILDLOG_HALIDE_BUILD=1 to build"
)
def test_build_and_run_the_sampled_tests(build, tmp_path):
    out = tmp_path / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(build), "-j", "4",
         "--json", str(out)],
        check=False, capture_output=True,
    )  # fmt: skip
    counts = json.loads(out.read_text())["counts"]
    # As under ctest: gpu_allocation_cache and multi_gpu skip without a GPU.
    assert counts == {"passed": 30, "skipped": 2, "failed": 0, "not run": 0}


def link_items(build, target):
    """The archives and objects of a program's link line, in command order."""
    import shlex

    ninja = (build / "souffle" / "build.ninja").read_text()
    words = shlex.split(cmd_of(ninja, f"bin/{REL}/{target}"))
    return [os.path.basename(w) for w in words if w.endswith((".a", ".o"))]


@pytest.mark.parametrize("suffix", ["", "_cpp"])
def test_aot_archives_precede_the_archives_they_call(build, suffix):
    # GNU ld reads an archive once: the caller's archive precedes its callees',
    # whatever the names' lexical order (tiled_blur calls blur2x2; root calls
    # inner and combine; inner calls leaf and combine).
    twin = "aotcpp" if suffix else "aot"
    tiled = link_items(build, f"generator_{twin}_tiled_blur")
    assert tiled.index(f"libtiled_blur{suffix}.a") < tiled.index(f"libblur2x2{suffix}.a")
    nested = link_items(build, f"generator_{twin}_nested_externs")
    pos = {
        n: nested.index(f"libnested_externs_{n}{suffix}.a")
        for n in ("root", "inner", "combine", "leaf")
    }
    assert pos["root"] < pos["inner"] < pos["combine"]
    assert pos["inner"] < pos["leaf"]


def test_a_generated_archive_follows_the_objects_that_use_it(build):
    items = link_items(build, "lesson_10_aot_compilation_run")
    assert items[-1] == "lesson_10_halide.a"
    assert all(i.endswith(".o") for i in items[:-1])
