# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""flatbuffers case study (case-studies/flatbuffers): flatc, a code generator
the build runs, is built for the machine running the build while the
runtime, generated code and tests are built for the configuration's target.

Nothing in the project says where flatc runs: rules.dl's flatc_schema emits
action_run(..., "flatc", "run") and core infers the exec dependency. The
release configuration is compared with the CMake baseline
(case-studies/flatbuffers/baseline/release): the same objects and link
inputs, and flatc's generated headers byte-identical to CMake's.
"""

from __future__ import annotations

import hashlib
import json
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
CASE = ROOT / "case-studies" / "flatbuffers"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang.toml"
BASELINE = host.baseline_dir(CASE) / "release"
SRC = Path(os.environ.get("BUILDLOG_FLATBUFFERS_SRC", str(CASE / "upstream"))).expanduser()
ARM = "arm64-macos.release.none"
X86 = "x86_64-macos.release.none"
FLATC = f"bin/{ARM}/flatc"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "include" / "flatbuffers" / "flatbuffers.h").exists(),
        reason="the pinned flatbuffers checkout is absent",
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


def configure(build: Path, config: str) -> None:
    _, metrics = configure_project(
        PROJECT,
        build,
        user_files=[PRESET],
        configs=[config],
        var_overrides={"FLATBUFFERS_SRC": str(SRC)},
    )
    assert metrics["errors"] == []


def ninja(build: Path, *args: str) -> str:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    ).stdout


def commands(build: Path) -> list[list[str]]:
    return [shlex.split(line) for line in ninja(build, "-t", "commands").splitlines()]


def flatc_runs(build: Path) -> list[list[str]]:
    return [w for w in commands(build) if Path(w[0]).name == "flatc"]


def target_of(words: list[str]) -> str | None:
    return next((w.split("=", 1)[1] for w in words if w.startswith("--target=")), None)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    build = tmp_path_factory.mktemp("flatbuffers") / "release"
    configure(build, "release")
    return build


@pytest.fixture(scope="module")
def x86_64(tmp_path_factory):
    host.require_macos()
    build = tmp_path_factory.mktemp("flatbuffers") / "x86_64"
    configure(build, "x86_64")
    return build


def test_release_generates_what_cmake_generates(release):
    runs = flatc_runs(release)
    # CMake's ten compile_schema_for_test calls plus the sample's schema.
    assert len(runs) == 11 and {w[0] for w in runs} == {FLATC}
    gen = f"gen/{ARM}/flattests"
    want = {}
    for line in (BASELINE / "generated-sha256.txt").read_text().splitlines():
        digest, path = line.split()
        want[path.removeprefix("./")] = digest
    ninja(release, *(f"{gen}/{p}" for p in want))
    for path, digest in want.items():
        data = (release / "souffle" / gen / path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, path


def test_release_objects_and_links_match_the_baseline(release):
    def key(o: str) -> tuple[str, str]:
        if o.startswith("CMakeFiles/"):
            t, rest = o.removeprefix("CMakeFiles/").split(".dir/", 1)
        else:
            t, rest = o.removeprefix(f"obj/{ARM}/").split("/", 1)
        return t, rest

    cmake = [shlex.split(ln) for ln in (BASELINE / "commands.txt").read_text().splitlines()]
    theirs = {key(w[w.index("-o") + 1]) for w in cmake if "-c" in w and "-o" in w}
    ours = {key(w[w.index("-o") + 1]) for w in commands(release) if "-c" in w}
    # 85 objects: the library's six sources, recompiled into flatc, flattests
    # and two samples as upstream does, rather than linked from the archive.
    assert ours == theirs and len(ours) == 85


def test_release_builds_and_flattests_passes(release):
    ninja(release, f"bin/{ARM}/flattests", f"bin/{ARM}/flatsamplebinary")
    out = subprocess.run(
        [str(release / "souffle" / f"bin/{ARM}/flattests")],
        cwd=SRC / "tests",
        check=False,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "ALL TESTS PASSED" in out.stdout
    sample = subprocess.run(
        [str(release / "souffle" / f"bin/{ARM}/flatsamplebinary")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert sample.returncode == 0 and "The FlatBuffer was successfully created" in sample.stdout


def test_x86_64_runs_an_arm64_flatc_with_no_annotation(x86_64):
    cmds = commands(x86_64)
    runs = flatc_runs(x86_64)
    assert len(runs) == 11 and {w[0] for w in runs} == {FLATC}
    link = {w[w.index("-o") + 1]: target_of(w) for w in cmds if Path(w[0]).name == "clang++"}
    # The tool the build runs is linked for the build machine; the requested
    # products, flatc included, for the target.
    assert link[FLATC] == "arm64-apple-macos13"
    assert link[f"bin/{X86}/flatc"] == "x86_64-apple-macos13"
    assert link[f"bin/{X86}/flattests"] == "x86_64-apple-macos13"
    # The generated headers flattests includes are the x86_64 configuration's.
    assert all(f"gen/{X86}/" in w[w.index("-o") + 1] for w in runs)
    # The project states no exec platform, override or tool dependency.
    files = [*PROJECT.glob("*.toml"), *PROJECT.glob("presets/*.toml"), PROJECT / "rules.dl"]
    text = "".join(p.read_text() for p in files)
    for word in ("exec_config", "exec_value", "exec_overridden", "host_platform", "tool_dep"):
        assert word not in text, word


def test_x86_64_builds_the_exec_flatc_for_arm64(x86_64):
    ninja(x86_64, FLATC, f"bin/{X86}/flattests")
    arch = subprocess.run(
        ["lipo", "-archs", str(x86_64 / "souffle" / FLATC)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert arch.stdout.split() == ["arm64"]
    arch = subprocess.run(
        ["lipo", "-archs", str(x86_64 / "souffle" / f"bin/{X86}/flattests")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert arch.stdout.split() == ["x86_64"]


def _rosetta() -> bool:
    return (
        subprocess.run(
            ["arch", "-x86_64", "/usr/bin/true"], capture_output=True, check=False
        ).returncode
        == 0
    )


@pytest.mark.skipif(not _rosetta(), reason="Rosetta is not installed")
def test_x86_64_tests_run_where_the_user_says_the_machine_runs_them(x86_64):
    """The user's platform_runs_on
    (presets/rosetta.toml) makes `buildlog test` run the x86_64 flattests
    under Rosetta; without it they are "not run". Nothing is rebuilt."""
    tests = json.loads((x86_64 / "souffle" / "tests.json").read_text())["tests"]
    assert [t["not_run"] for t in tests] == [
        ["built for x86_64-macos; the build machine is arm64-macos"]
    ]
    _, metrics = configure_project(
        PROJECT,
        x86_64,
        user_files=[PRESET, PROJECT / "presets" / "rosetta.toml"],
        configs=["x86_64"],
        var_overrides={"FLATBUFFERS_SRC": str(SRC)},
    )
    assert metrics["errors"] == []
    (case,) = json.loads((x86_64 / "souffle" / "tests.json").read_text())["tests"]
    assert case["not_run"] == [] and case["launcher"] == []
    ninja(x86_64, "-j4", *case["needs"])
    r = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "test", "-C", str(x86_64), "--no-build"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed, 0 skipped, 0 failed, 0 not run" in r.stdout
