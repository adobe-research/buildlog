# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib-ng case study: per-TU ISA flags, ISA compatibility, universal2.

case-studies/zlib-ng/project builds zlib-ng 2.3.3 (the pinned checkout,
buildlog.toml's ZLIBNG_SRC) for arm64, x86_64 and universal2 (both slices,
merged). The ground truth is case-studies/zlib-ng/baseline, zlib-ng's own
CMake build of arm64 and x86_64 (baseline/run-cmake.sh); its universal2
build compiles no ISA-specific source in either slice (zlib-ng #1016,
#1085). Here each slice gets its architecture's sets with their flags, a
set needed where its cpu lacks the extensions is an error, and a set
requiring extensions no architecture has together is an error.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "zlib-ng"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang.toml"
BASELINE = host.baseline_dir(CASE)
SRC = Path(os.path.expanduser(os.environ.get("BUILDLOG_ZLIBNG_SRC", str(CASE / "upstream"))))
SRC_VAR = {"ZLIBNG_SRC": str(SRC)}
ARM = "arm64-macos.release.none"
X86 = "x86_64-macos.release.none"
U2 = "universal2-macos.release.none"
ARM_CFG = '["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]]'

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "zlib-ng.h.in").exists(), reason="the pinned zlib-ng checkout is absent"
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]

# The case study's comparison script (run_path: no bytecode in the repo).
compare = SimpleNamespace(**runpy.run_path(str(CASE / "compare.py")))

ISA_SYMBOL = re.compile(
    r"_(neon|armv8|armv6|sse2|ssse3|sse41|sse42|pclmulqdq|avx2|avx512|avx512_vnni|vpclmulqdq)$"
    r"|^_?(cpu_check_features|arm_check_features|x86_check_features)$"
)


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib-ng") / "build"
    _, metrics = configure_project(
        PROJECT,
        build,
        user_files=[PRESET],
        var_overrides=SRC_VAR,
        configs=None if host.MACOS else ["release"],
    )
    assert metrics["errors"] == []
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    return build


def _out(build: Path) -> Path:
    return build / "souffle"


def _isa_functions(archive: Path, types: tuple[str, ...] = ("T",)) -> dict[str, list[str]]:
    """Arch -> the ISA-specific functions a file defines (baseline/run-cmake.sh's
    selection; a dylib's are local symbols, its hidden visibility)."""
    out = {}
    if not host.MACOS:
        nm = subprocess.check_output(["nm", str(archive)], text=True, stderr=subprocess.DEVNULL)
        syms = {
            w[2]
            for w in (line.split() for line in nm.splitlines())
            if len(w) == 3 and w[1] in types
        }
        return {"arm64": sorted(s for s in syms if ISA_SYMBOL.search(s))}
    for arch in subprocess.check_output(["lipo", "-archs", str(archive)], text=True).split():
        nm = subprocess.check_output(
            ["nm", "-arch", arch, str(archive)], text=True, stderr=subprocess.DEVNULL
        )
        syms = {
            w[2]
            for w in (line.split() for line in nm.splitlines())
            if len(w) == 3 and w[1] in types
        }
        out[arch] = sorted(s for s in syms if ISA_SYMBOL.search(s))
    return out


def _baseline_isa(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (BASELINE / name / "isa-functions.txt").read_text().splitlines():
        if line.startswith("===== "):
            cur = out.setdefault(line[6:].replace("aarch64", "arm64"), [])
        else:
            cur.append(line)
    return out


def _why(build: Path, *args: str) -> str:
    return subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), *args, str(PROJECT)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout


@pytest.mark.parametrize(
    ("baseline", "platform"),
    [("native", "arm64-macos"), pytest.param("x86_64", "x86_64-macos", marks=host.cross)],
)
def test_every_tu_and_its_flags_match_the_baseline(build, baseline, platform):
    ours = compare.buildlog(_out(build), platform)
    theirs = compare.baseline(BASELINE / baseline / "commands.txt")
    assert ours.keys() == theirs.keys()
    assert {k: v for k, v in ours.items() if theirs[k] != v} == {}


def test_generated_files_match_the_baseline(build):
    for cfg, name in ((ARM, "native"), (X86, "x86_64"))[: 2 if host.MACOS else 1]:
        gen = _out(build) / "gen" / cfg
        for f in ("zconf-ng.h", "zlib-ng.h", "gzread_mangle.h", "zlib_name_mangling-ng.h"):
            assert (gen / "zlib-ng" / f).read_bytes() == (BASELINE / name / f).read_bytes(), (
                cfg,
                f,
            )
        assert (gen / "zlib-ng_pc" / "zlib-ng.pc").read_bytes() == (
            BASELINE / name / "zlib-ng.pc"
        ).read_bytes()


def test_the_libraries_define_the_baselines_isa_functions(build):
    lib = _out(build) / "lib"
    assert _isa_functions(lib / ARM / "libz-ng.a") == _baseline_isa("native")
    if host.MACOS:
        assert _isa_functions(lib / X86 / "libz-ng.a") == _baseline_isa("x86_64")


@host.cross
def test_universal2_has_each_slices_own_isa_sources(build):
    lib = _out(build) / "lib" / U2
    # CMake's universal2 build: dispatch code only, no SIMD in either slice.
    assert _baseline_isa("universal2") == {
        "x86_64": ["_arm_check_features", "_cpu_check_features"],
        "arm64": ["_arm_check_features", "_cpu_check_features"],
    }
    both = {**_baseline_isa("native"), **_baseline_isa("x86_64")}
    assert _isa_functions(lib / "libz-ng.a") == both
    local = ("T", "t")
    assert _isa_functions(lib / "libz-ng.2.3.3.dylib", local) == _isa_functions(
        lib / "libz-ng.a", local
    )
    for f in [lib / "libz-ng.a", lib / "libz-ng.2.3.3.dylib", _out(build) / "bin" / U2 / "example"]:
        assert subprocess.check_output(["lipo", "-archs", str(f)], text=True).split() == [
            "x86_64",
            "arm64",
        ]
    # The merged shared library keeps its install name and gets its links.
    assert os.readlink(lib / "libz-ng.2.dylib") == "libz-ng.2.3.3.dylib"
    assert os.readlink(lib / "libz-ng.dylib") == "libz-ng.2.dylib"
    otool = subprocess.check_output(["otool", "-D", str(lib / "libz-ng.2.3.3.dylib")], text=True)
    assert otool.count("@rpath/libz-ng.2.dylib") == 2


@host.cross
def test_the_shared_library_matches_the_baselines_metadata(build):
    lib = _out(build) / "lib" / ARM
    otool = subprocess.check_output(["otool", "-L", str(lib / "libz-ng.2.3.3.dylib")], text=True)
    assert "@rpath/libz-ng.2.dylib (compatibility version 2.0.0, current version 2.3.3)" in otool
    names = {
        w[-1]
        for w in (
            line.split()
            for line in subprocess.check_output(
                ["nm", "-gU", str(lib / "libz-ng.2.3.3.dylib")], text=True
            ).splitlines()
        )
        if w
    }
    text = (BASELINE / "native" / "exported-symbols.txt").read_text()
    block = text.split("===== libz-ng.2.3.3.dylib\n")[1].split("=====")[0]
    assert names == {line.split()[-1] for line in block.splitlines() if line.strip()}


@host.linux_only
def test_the_shared_library_exports_the_baselines_functions_unversioned(build):
    """Upstream links with a version script (ZLIB_NG_2.0.0 ...), which buildlog
    cannot state (README, Linux): the defined function names are the same."""
    lib = _out(build) / "lib" / ARM / "libz-ng.so.2.3.3"
    got = {
        w[-1]
        for w in (
            line.split()
            for line in subprocess.check_output(
                ["nm", "-D", "--defined-only", str(lib)], text=True
            ).splitlines()
        )
        if len(w) == 3 and w[1] == "T"
    }
    text = (BASELINE / "native" / "exported-symbols.txt").read_text()
    block = text.split("===== libz-ng.so.2.3.3\n")[1].split("=====")[0]
    want = {
        line.split()[-1].split("@@")[0]
        for line in block.splitlines()
        if line.split() and line.split()[0] == "T"
    }
    assert got == want


def test_no_link_uses_the_cxx_driver(build):
    lines = subprocess.run(
        ["ninja", "-C", str(_out(build)), "-t", "commands"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert not [line for line in lines if line.split()[0].endswith(("clang++", "g++"))]


@host.cross
def test_why_avx2_flag_leads_to_the_isa_facts_the_cpu_and_the_probe(build):
    out = _why(
        build,
        "--with-demand",
        "--leaves",
        "--output",
        f"obj/{X86}/zlib-ng/arch/x86/adler32_avx2.c.o",
        "--flag=-mavx2",
    )
    for fact in (
        'family_isa_flag("clang", "avx2", "-mavx2")',
        'isa_requires("avx2", "avx2")',
        'isa_ext("x86_64", "avx2")',
        'source_set_source("avx2", "arch/x86/adler32_avx2.c")',
        'source_set_when("avx2", "WITH_AVX2 & cc.check.HAVE_AVX2_INTRIN & isa.sse42 == on")',
        'option_default("WITH_AVX2", "on")',
        'res_platform_constraint("inventory:default", "x86_64-macos", "cpu", "x86_64")',
        'toolchain_pin("c", "arm64-macos", "x86_64-macos", "apple-clang")',
    ):
        assert fact in out, fact
    probe = [
        line
        for line in out.splitlines()
        if line.strip().startswith("res_cc_check(") and "_mm256_subs_epu16" in line
    ]
    assert len(probe) == 1 and '"yes")' in probe[0]
    # Nothing about another configuration, variant or architecture: those
    # were engine-chosen witnesses of domain guards (cond_sub,
    # config_platform, settings_suffix) and of the C capability probe's
    # demand (tc_required: "some C target is needed"), not reasons.
    for stray in (
        "ARMV8",
        'config_decl("release"',
        "universal2",
        '"shared_library"',
        "[asked:",
    ):
        assert stray not in out, stray
    # 1 root + 52 leaves: the avx2 set, its variant, request, option, check
    # and probe (11); the x86_64 configuration, preset and inventory (9);
    # buildlog-cpp's knowledge (3); the sse42 -> sse41 -> ssse3 -> sse2
    # prerequisite chain, 7 facts per set, plus WITH_OPTIM (29). No slack: one leaked witness
    # adds a line or more, which is the regression this guards against.
    assert len(out.splitlines()) <= 53, out


def test_why_the_avx2_set_is_absent_from_the_arm64_slice(build):
    assert not (_out(build) / "obj" / ARM / "zlib-ng" / "arch" / "x86").exists()
    out = _why(build, f'config_attr({ARM_CFG}, "isa.avx2", "off")')
    assert 'config_attr(‹arm64-macos.release.none›, "isa.compatible.avx2", "no")' in out
    assert 'platform_constraint("arm64-macos", "cpu", "arm64")' in out
    assert '!isa_compatible("avx2", "arm64")' in out
    # Paper-printable: a shared premise is cited by id on its
    # consumers' lines, and the infix condition shares its row's line.
    # 15 plus the one $Named step the recursion check requires of isa.dl.
    assert len(out.splitlines()) <= 16, out
    # The infix condition no longer shares its row: isa.dl names conditions
    # ($Named) instead of copying their terms, as the recursion check requires.
    assert "↑ #" in out


def test_the_smoke_tests_pass(build):
    if not host.MACOS:
        cp = subprocess.run(
            ["uv", "run", "--frozen", "buildlog", "test", "-C", str(build), str(PROJECT)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        assert " 0 failed" in cp.stdout, cp.stdout
        return
    cp = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "test",
            "-C",
            str(build),
            "--no-build",
            "--json",
            str(build / "t.json"),
            str(PROJECT),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    assert "7 passed, 0 skipped, 0 failed, 7 not run" in cp.stdout, cp.stdout
    # The x86_64 build and both slices of universal2 run on this Mac too
    # (Rosetta); buildlog test does not run a foreign platform's binaries.
    for arch, exe in (
        ("x86_64", _out(build) / "bin" / X86 / "example"),
        ("x86_64", _out(build) / "bin" / U2 / "example"),
        ("arm64", _out(build) / "bin" / U2 / "example"),
    ):
        subprocess.run(["arch", f"-{arch}", str(exe)], check=True, capture_output=True, cwd=build)
    for arch in ("x86_64", "arm64"):
        gz = _out(build) / "bin" / U2 / "minigzip"
        packed = subprocess.run(
            ["arch", f"-{arch}", str(gz)], input=b"zlib-ng\n" * 100, capture_output=True, check=True
        ).stdout
        unpacked = subprocess.run(
            ["arch", f"-{arch}", str(gz), "-d"], input=packed, capture_output=True, check=True
        ).stdout
        assert unpacked == b"zlib-ng\n" * 100


def _configure_variant(tmp_path: Path, edit: dict[str, str]) -> list[list[str]]:
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    for name, text in edit.items():
        with (project / name).open("a") as f:
            f.write(text)
    build = tmp_path / "build"
    with pytest.raises(SystemExit):
        configure_project(
            project,
            build,
            user_files=[PRESET],
            configs=["x86_64"],
            check_only=True,
            var_overrides=SRC_VAR,
        )
    return json.loads((build / "metrics.json").read_text())["errors"]


@host.cross
def test_a_neon_set_needed_in_the_x86_64_slice_is_rejected(tmp_path):
    errors = _configure_variant(
        tmp_path,
        {
            # a TU of the neon set, compiled outside the set (bypassing isa.neon)
            "tests.toml": '\n[target.neon_user]\nnative_kind = "library"\n'
            'c_source = "arch/arm/adler32_neon.c"\nrequest = "x86_64"\n',
        },
    )
    assert [
        "ISA scope incompatible with the configuration's cpu",
        f"neon_user: arch/arm/adler32_neon.c (neon) needs neon, which x86_64 ({X86}) does not have",
    ] in [list(e) for e in errors]


def test_a_set_requiring_neon_and_avx2_is_rejected(tmp_path):
    errors = _configure_variant(
        tmp_path,
        {
            "isa.toml": '\n[source_set.mixed]\nlib = "zlib-ng"\nsource_set_source = ["arch/arm/adler32_neon.c"]\n'
            'isa_requires = ["neon", "avx2"]\n',
        },
    )
    assert [
        "ISA extensions cannot coexist in one TU (no architecture has both)",
        "mixed: avx2 and neon",
    ] in [list(e) for e in errors]


def test_an_unknown_isa_extension_is_rejected(tmp_path):
    """The extensions are buildlog-cpp's knowledge."""
    errors = _configure_variant(
        tmp_path,
        {
            "isa.toml": '\n[source_set.future]\nlib = "zlib-ng"\nsource_set_source = ["arch/x86/adler32_avx2.c"]\n'
            'isa_requires = ["avx10.2"]\n',
        },
    )
    assert ["unknown ISA extension", "future: avx10.2"] in [list(e) for e in errors]


def test_cyclic_source_set_conditions_are_an_error_not_a_hang(tmp_path):
    """Two sets whose whens name each other, one with a second when (a base
    case), used to nest condition terms without end before reporting."""
    errors = _configure_variant(
        tmp_path,
        {
            "isa.toml": '\n[source_set.cyc_a]\nlib = "zlib-ng"\nsource_set_source = ["arch/x86/adler32_avx2.c"]\n'
            'source_set_when = ["WITH_OPTIM", "isa:cyc_b"]\nisa_requires = "sse2"\n'
            '\n[source_set.cyc_b]\nlib = "zlib-ng"\nsource_set_source = ["arch/x86/adler32_sse42.c"]\n'
            'source_set_when = "isa:cyc_a"\nisa_requires = "sse2"\n',
        },
    )
    flat = [list(e) for e in errors]
    assert ["source set condition refers to itself", "cyc_a"] in flat
    assert ["source set condition refers to itself", "cyc_b"] in flat
