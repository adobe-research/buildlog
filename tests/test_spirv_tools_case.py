# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SPIRV-Tools case study: grammar-driven code generation over an imported
package's data files (case-studies/spirv-tools).

SPIRV-Headers is imported from an install prefix (its CMake export, found
through the user's package_prefix); its grammar JSON files are data files
of that package, located from the imported include dir. The generator
actions run SPIRV-Tools' Python scripts over them with the user's Python
(a CPS the user ships). The generated tables are compared byte for byte
with the CMake baseline (case-studies/spirv-tools/baseline), `why` traces
a grammar argument back to the user's prefix, and another SPIRV-Headers
release in another prefix changes the tables.

Environment: BUILDLOG_SPIRV_TOOLS_SRC (the pinned checkout, default the
submodule case-studies/spirv-tools/upstream, with external/ filled by
case-studies/spirv-tools/fetch-deps.sh), BUILDLOG_SPIRV_HEADERS_PREFIX (the
paired SPIRV-Headers install, no default; the tests skip without it) and
BUILDLOG_SPIRV_HEADERS_ALT_PREFIX (another release, no default; its test
skips without it). README.md says how to build the installs.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "spirv-tools"
PROJECT = CASE / "project"
PRESETS = PROJECT / "presets"
BASELINE = host.baseline_dir(CASE)
SMOKE = CASE / "smoke"
REL = "arm64-macos.release.none"
GEN = f"gen/{REL}/spirv-tools-tables"
GENERATED = (
    "core_tables_body.inc",
    "core_tables_header.inc",
    "generators.inc",
    "DebugInfo.h",
    "OpenCLDebugInfo100.h",
    "build-version.inc",
)


def _env_path(name: str, default: Path | None = None) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(os.path.expanduser(value))
    return default if default is not None else Path(f"<{name} unset>")


SRC = _env_path("BUILDLOG_SPIRV_TOOLS_SRC", CASE / "upstream")
HEADERS = _env_path("BUILDLOG_SPIRV_HEADERS_PREFIX")
ALT_HEADERS = _env_path("BUILDLOG_SPIRV_HEADERS_ALT_PREFIX")
if host.MACOS:
    PYTHON = Path("/opt/homebrew/bin/python3")  # presets/Python3.cps
    GTEST = Path("/opt/homebrew/opt/googletest/lib/cmake/GTest")  # presets/homebrew-googletest.toml
    PYTHON_PRESET, GTEST_PRESET, GTEST_VARS = "homebrew-python", "homebrew-googletest", {}
else:
    PYTHON = Path("/usr/bin/python3")  # presets/linux/Python3.cps
    GTEST = Path("/opt/googletest/lib/cmake/GTest")  # presets/prefix-googletest.toml
    PYTHON_PRESET, GTEST_PRESET = "linux-python", "prefix-googletest"
    GTEST_VARS = {"GOOGLETEST_PREFIX": "/opt/googletest"}
HEADERS_CONFIG = "share/cmake/SPIRV-Headers/SPIRV-HeadersConfig.cmake"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
    pytest.mark.skipif(
        not (SRC / "external" / "googletest").is_dir() or not (SRC / "utils" / "ggt.py").exists(),
        reason="the pinned SPIRV-Tools checkout is absent (git submodule update --init; "
        "case-studies/spirv-tools/fetch-deps.sh)",
    ),
    pytest.mark.skipif(
        not (HEADERS / HEADERS_CONFIG).exists(),
        reason="BUILDLOG_SPIRV_HEADERS_PREFIX unset or not a SPIRV-Headers install",
    ),
    pytest.mark.skipif(
        not PYTHON.exists(), reason="the preset python3 (presets/Python3.cps) absent"
    ),
]

BASE_PRESETS = ("apple-clang-arm64", PYTHON_PRESET, "spirv-headers-prefix")


def configure(build: Path, *presets: str, prefix: Path = HEADERS, project: Path = PROJECT):
    extra = (GTEST_PRESET,) if GTEST.exists() else ("skip-tests",)
    users = [PRESETS / f"{p}.toml" for p in (*BASE_PRESETS, *extra, *presets)]
    _, metrics = configure_project(
        project,
        build,
        user_files=users,
        var_overrides={
            "SPIRV_TOOLS_SRC": str(SRC),
            "SPIRV_HEADERS_PREFIX": str(prefix),
            **(GTEST_VARS if GTEST.exists() else {}),
        },
    )
    return metrics


def ninja(build: Path, *args: str) -> str:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    ).stdout


def sha256s(d: Path) -> dict[str, str]:
    return {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in GENERATED}


def baseline_sha256s() -> dict[str, str]:
    rows = (line.split() for line in (BASELINE / "generated-sha256.txt").read_text().splitlines())
    return {name: digest for digest, name in rows if name in GENERATED}


def generator_argv(line: str, build: Path, prefix: Path) -> list[str]:
    """A generator command with the checkout, the SPIRV-Headers dir and the
    output dir spelled $SRC, $HEADERS and $BUILD (the baseline's spelling)."""
    words = shlex.split(line.split("&&")[-1])
    out = []
    for w in words:
        if w.startswith("../"):  # relative to the Ninja dir
            w = os.path.normpath(build / "souffle" / w)
        w = w.replace(str(prefix), "$HEADERS").replace(str(SRC.resolve()), "$SRC")
        out.append(w.replace(str(SRC), "$SRC").replace(GEN, "$BUILD"))
    return out


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("spirv-tools") / "build"
    metrics = configure(build)
    assert metrics["errors"] == []
    ninja(build, *(f"{GEN}/{f}" for f in GENERATED))
    return build


def test_generated_tables_match_the_baseline(build):
    assert sha256s(build / "souffle" / GEN) == baseline_sha256s()


def test_generator_commands_are_upstreams(build):
    ours = [
        generator_argv(line, build, HEADERS)
        for line in ninja(build, "-t", "commands", *(f"{GEN}/{f}" for f in GENERATED)).splitlines()
        if str(PYTHON) in line
    ]
    theirs = [
        generator_argv(line, build, HEADERS)
        for line in (BASELINE / "generators.txt").read_text().splitlines()
    ]
    assert sorted(ours) == sorted(theirs) and len(ours) == 5


def _why_leaves(build: Path, flag: str, output: str) -> str:
    return subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--flag", flag]
        + ["--output", output, "--leaves", str(PROJECT)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_why_a_grammar_input_leads_to_the_users_prefix(build):
    grammar = HEADERS / "include/spirv/unified1/spirv.core.grammar.json"
    out = _why_leaves(build, f"--spirv-core-grammar={grammar}", f"{GEN}/core_tables_body.inc")
    # The file is a grammar of the project, which the project rule places in the package...
    assert 'spv_grammar("core", "spirv.core.grammar.json")' in out
    assert "generators.toml" in out
    # ...located by the include dir its CMake export states...
    # (display_path may shorten it relative to the cwd.)
    assert f"{HEADERS.name}/{HEADERS_CONFIG}:" in out
    assert f'"cc.include_dir", "{HEADERS}/include"' in out
    # ...in the prefix the user named.
    assert f'package_prefix("{HEADERS}")' in out
    assert "presets/spirv-headers-prefix.toml" in out


def test_the_interpreter_is_the_users(build):
    out = _why_leaves(build, str(PYTHON), f"{GEN}/generators.inc")
    assert 'alias("python3", "Python3:Interpreter")' in out
    assert f"presets/{PYTHON_PRESET}.toml" in out
    assert "Python3.cps" in out


@pytest.mark.skipif(
    not (ALT_HEADERS / HEADERS_CONFIG).exists(),
    reason="BUILDLOG_SPIRV_HEADERS_ALT_PREFIX unset or not a SPIRV-Headers install",
)
def test_another_spirv_headers_changes_the_tables(build, tmp_path):
    alt = tmp_path / "alt"
    assert configure(alt, prefix=ALT_HEADERS)["errors"] == []
    ninja(alt, *(f"{GEN}/{f}" for f in GENERATED))
    before, after = sha256s(build / "souffle" / GEN), sha256s(alt / "souffle" / GEN)
    changed = {f for f in GENERATED if before[f] != after[f]}
    grammar = "include/spirv/unified1/spirv.core.grammar.json"
    if (HEADERS / grammar).read_bytes() != (ALT_HEADERS / grammar).read_bytes():
        assert {"core_tables_body.inc", "core_tables_header.inc"} <= changed
    # build-version.inc reads SPIRV-Tools' CHANGES only.
    assert "build-version.inc" not in changed
    cmd = ninja(alt, "-t", "commands", f"{GEN}/core_tables_body.inc").splitlines()[-1]
    assert f"--spirv-core-grammar={ALT_HEADERS}/include/" in cmd


# ---- options ----------------------------------------------------------------


def _bins(build: Path) -> set[str]:
    text = (build / "souffle" / "build.ninja").read_text()
    return {
        line.split(":")[0].rsplit("/", 1)[-1]
        for line in text.splitlines()
        if line.startswith(f"build bin/{REL}/")
    }


def test_default_builds_the_tools_and_tests(build):
    bins = _bins(build)
    assert {"spirv-as", "spirv-dis", "spirv-val", "spirv-opt"} <= bins
    if GTEST.exists():
        assert {"test_spirv_unit_tests", "test_c_interface", "test_cpp_interface"} <= bins


def test_skip_executables_builds_the_libraries_only(tmp_path):
    assert configure(tmp_path / "b", "skip-executables")["errors"] == []
    assert _bins(tmp_path / "b") == set()
    text = (tmp_path / "b" / "souffle" / "build.ninja").read_text()
    assert f"lib/{REL}/libSPIRV-Tools-opt.a" in text


def configure_errors(capsys, build: Path, *presets: str, **kw) -> str:
    with pytest.raises(SystemExit):
        configure(build, *presets, **kw)
    return capsys.readouterr().err


def test_the_fuzzer_is_rejected(tmp_path, capsys):
    err = configure_errors(capsys, tmp_path / "b", "fuzzer")
    assert "option not supported by this spec: SPIRV_BUILD_FUZZER=on" in err


def _compile(build: Path, obj: str) -> list[str]:
    return shlex.split(ninja(build, "-t", "commands", obj).splitlines()[-1])


def test_werror_is_the_users_opt_in(build, tmp_path):
    obj = f"obj/{REL}/SPIRV-Tools/source/table2.cpp.o"
    assert "-Werror" not in _compile(build, obj)
    assert configure(tmp_path / "b", "werror")["errors"] == []
    assert "-Werror" in _compile(tmp_path / "b", obj)


def test_an_author_werror_is_rejected(tmp_path, capsys):
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    spec = project / "spec.toml"
    text = spec.read_text()
    assert '"-Wall",' in text
    spec.write_text(text.replace('"-Wall",', '"-Wall", "-Werror",', 1))
    err = configure_errors(capsys, tmp_path / "b", project=project)
    assert "error: -Werror is a user decision: SPIRV-Tools: -Werror" in err


def test_static_library_flags_match_the_baseline(build):
    """The static library's compile line against CMake's, modulo the
    ledger's differences: -Werror (the user's), how the target and the
    dependency's include dir are spelled, and the flag order."""
    ours = _compile(build, f"obj/{REL}/SPIRV-Tools/source/table2.cpp.o")
    theirs = next(
        shlex.split(line)
        for line in (BASELINE / "commands.txt").read_text().splitlines()
        if "SPIRV-Tools-static.dir/" in line and " -c " in line
    )

    def flags(words):
        return {w for w in words if w.startswith(("-W", "-f", "-D", "-O", "-std"))}

    assert flags(ours) == flags(theirs) - {"-Werror"}


# ---- the programs -------------------------------------------------------------

TOOLS = ("spirv-as", "spirv-dis", "spirv-val")


@pytest.fixture(scope="module")
def tools(build):
    ninja(build, "-j4", *(f"bin/{REL}/{t}" for t in TOOLS))
    return build / "souffle" / "bin" / REL


def test_tools_round_trip_a_module(tools, tmp_path):
    spv = tmp_path / "sample.spv"
    subprocess.run([tools / "spirv-as", SMOKE / "sample.spvasm", "-o", spv], check=True)
    subprocess.run([tools / "spirv-val", spv], check=True)
    dis = tmp_path / "sample.dis"
    subprocess.run([tools / "spirv-dis", spv, "-o", dis], check=True)
    assert dis.read_text() == (SMOKE / "sample.dis").read_text()
    version = subprocess.run(
        [tools / "spirv-as", "--version"], check=True, capture_output=True, text=True
    ).stdout
    assert version.startswith("SPIRV-Tools v2026.3 ")


@pytest.mark.skipif(not GTEST.exists(), reason="googletest absent")
def test_c_interface_tests_pass(tools):
    out = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "test", "-C", str(tools.parents[2])]
        + ["-R", "c_interface$", "-j4", str(PROJECT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 passed, 0 skipped, 0 failed" in out.stdout
