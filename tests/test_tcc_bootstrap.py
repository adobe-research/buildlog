# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""TinyCC bootstrap case study: a toolchain built and used in one graph.

case-studies/tcc/project builds tcc_stage1 with the user's C toolchain
(apple-clang, the preset), then tcc_stage2 with tcc_stage1 as its compiler
(the toolchain tcc-stage1), then tcc_stage3 with tcc_stage2 (tcc-stage2),
and an ordinary library and program with tcc-stage2. The toolchains are
stated by the case study's tcc rule package (packages/tcc), never probed:
core's toolchain_tool_target makes a role's program a target's run artifact
in the exec configuration (L2_toolchains.dl), and target_toolchain lets a
target name the built toolchain that builds it.

The study is configured for arm64-macos (TCC's Mach-O arm64 code generator
and lib-arm64 runtime), so it runs only there.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "tcc"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang-arm64.toml"
REL = "arm64-macos.release.none"
DBG = "arm64-macos.debug.none"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (sys.platform == "darwin" and platform.machine() == "arm64"),
        reason="macOS arm64 only: the study builds TCC's Mach-O arm64 compiler and runtime",
    ),
    pytest.mark.skipif(
        not (CASE / "upstream" / "tcc.c").exists(), reason="the pinned tinycc checkout is absent"
    ),
]


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def rows(build: Path, rel: str) -> set[tuple[str, ...]]:
    path = build / "souffle" / "facts" / f"{rel}.csv"
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def cfg(c: str) -> str:
    """A configuration's printed name (arm64-macos.release.none) from its
    value in a facts file."""
    plat, rest = c[1:].split(", ", 1)
    return f"{plat}.{rest.split(', ')[1].rstrip(']')}.none"


def _copy(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    toml = project / "buildlog.toml"
    toml.write_text(toml.read_text().replace("${REPO_ROOT}/case-studies/tcc", str(CASE)))
    return project


def _configure(project: Path, build: Path) -> dict:
    try:
        configure_project(project, build, user_files=[PRESET], update_lock=True)
    except SystemExit:
        pass
    return json.loads((build / "metrics.json").read_text())


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    build = tmp_path_factory.mktemp("tcc") / "build"
    _, metrics = configure_project(PROJECT, build, user_files=[PRESET])
    assert metrics["errors"] == []
    assert metrics["warnings"] == []
    return build


def _obj(target: str, c: str = REL) -> str:
    return f"out:obj/{c}/{target}/upstream/tcc.c.o"


def _tcc(stage: int, c: str = REL) -> str:
    return f"out:bin/{c}/stage{stage}/tcc"


def test_each_stage_is_built_by_the_previous_one(configured):
    tc_of = {(t, lang, cfg(c)): tc for t, lang, c, tc in rows(configured, "tc_of")}
    # stage 1 by the user's selection, the rest by the toolchain they name
    assert tc_of[("tcc_stage1", "c", REL)] == "apple-clang"
    assert tc_of[("tcc_stage2", "c", REL)] == "tcc-stage1"
    assert tc_of[("tcc_stage3", "c", REL)] == "tcc-stage2"
    assert tc_of[("libtcc1_stage1", "c", REL)] == "tcc-stage1"
    assert tc_of[("libtcc1_stage2", "c", REL)] == "tcc-stage2"
    for t in ("greet", "hello", "same_file"):
        assert tc_of[(t, "c", REL)] == "tcc-stage2"
        assert tc_of[(t, "native-link", REL)] == "tcc-stage2"
    # one toolchain per (target, language, configuration): no ambiguity
    keys = [(t, lang, cfg(c)) for t, lang, c, _ in rows(configured, "tc_of")]
    assert len(keys) == len(set(keys))


def test_stage2_compile_runs_stage1s_artifact(configured):
    args = rows(configured, "action_arg")
    ins = rows(configured, "action_in")
    for stage in (2, 3):
        obj = _obj(f"tcc_stage{stage}")
        tool = _tcc(stage - 1)
        assert (obj, "0", "", tool) in args
        # Ninja orders it: the compiler is an input of every action it runs
        assert (obj, tool, "implicit") in ins
        # aimed by its package's facts: -B names the tccdir (its include/)
        assert (obj, "1", "-B ", "src:upstream") in args
        link = _tcc(stage)
        assert (link, "0", "", tool) in args
        assert (link, tool, "implicit") in ins
        # ... and links the stage's runtime, built by the same compiler
        assert (link, f"out:lib/{REL}/liblibtcc1_stage{stage - 1}.a", "implicit") in ins
    # tcc's archiver is `tcc -ar`
    ar = f"out:lib/{REL}/libgreet.a"
    assert (ar, "0", "", _tcc(2)) in args
    assert (ar, "1", "", "-ar") in args


def test_a_built_toolchain_is_a_tool_dependency(configured):
    deps = {(t, cfg(c), g) for t, c, g in rows(configured, "tool_dep")}
    assert ("tcc_stage2", REL, "tcc_stage1") in deps
    assert ("tcc_stage3", REL, "tcc_stage2") in deps
    assert ("hello", REL, "tcc_stage2") in deps
    assert not any(t == "tcc_stage1" for t, _, _ in deps)


def test_no_probe_runs_a_built_toolchain(configured):
    # The TCCs' capabilities are their package's facts: nothing asks a
    # compiler that does not exist at configure time.
    for rel in ("req_cc_capability", "req_cc_warning", "req_cc_implicit_include", "req_cxx_std"):
        for row in rows(configured, rel):
            assert not any("tcc-stage" in v for v in row), (rel, row)


def test_tools_run_in_the_exec_configuration(tmp_path):
    # A debug configuration's stages are compiled by the compilers built for
    # the build machine (the release configuration, debug's exec), as a
    # generator's tool is; tcc_stage1 is never built in debug.
    project = _copy(tmp_path)
    toml = (project / "buildlog.toml").read_text()
    toml = toml.replace(
        'config_decl = { release = "arm64-macos" }',
        'config_decl = { release = "arm64-macos", debug = "arm64-macos" }',
    ).replace(
        'config_decl_setting = [["release", "native.opt", "release"]]',
        'config_decl_setting = [["release", "native.opt", "release"], ["debug", "native.opt", "debug"]]',
    )
    (project / "buildlog.toml").write_text(toml)
    build = tmp_path / "build"
    metrics = _configure(project, build)
    assert metrics["errors"] == []
    exec_of = {cfg(c): cfg(x) for c, x in rows(build, "exec_config")}
    assert exec_of[DBG] == REL
    args = rows(build, "action_arg")
    ins = rows(build, "action_in")
    assert (_obj("tcc_stage2", DBG), "0", "", _tcc(1, REL)) in args
    assert (_obj("tcc_stage2", DBG), _tcc(1, REL), "implicit") in ins
    assert (_obj("tcc_stage3", DBG), "0", "", _tcc(2, REL)) in args
    needed = {(t, cfg(c)) for t, c in rows(build, "needed")}
    assert ("tcc_stage1", REL) in needed
    assert ("tcc_stage1", DBG) not in needed
    # the runtime is the linked target's own configuration's
    assert (_tcc(2, DBG), f"out:lib/{DBG}/liblibtcc1_stage1.a", "implicit") in ins


def test_why_explains_which_compiler_builds_stage2(configured, capsys):
    capsys.readouterr()
    assert (
        main(
            [
                "why",
                "-C",
                str(configured),
                "--flag",
                f"bin/{REL}/stage1/tcc",
                "--output",
                f"obj/{REL}/tcc_stage2/upstream/tcc.c.o",
                str(PROJECT),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert 'action_tc_tool("out:obj/' in out
    assert 'target_toolchain("tcc_stage2", "tcc-stage1")' in out
    assert 'tcc_toolchain("tcc-stage1", "tcc_stage1", "libtcc1_stage1"' in out
    assert 'executable("tcc_stage1"' in out


EXTRA = '[target.extra]\nkind = "executable"\nsources = ["project/demo/hello.c"]\n'


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        # an installed toolchain is the user's choice (toolchain_pin)
        (EXTRA + 'toolchain = "apple-clang"\n', "target toolchain is not a built toolchain"),
        (EXTRA + 'toolchain = "tcc-stage9"\n', "target toolchain is not a built toolchain"),
        (
            'tcc_toolchain = [["tcc-stage1", "tcc_stage1", "libtcc1_stage1", "src:upstream", "0.9.27"]]\n',
            "tcc toolchain named twice",
        ),
        (
            'tcc_toolchain = [["tcc-stage3", "tcc_stage3", "no_such_runtime", "src:upstream", "x"]]\n',
            "unknown target",
        ),
    ],
)
def test_misuse_is_an_error(tmp_path, extra, message):
    project = _copy(tmp_path)
    (project / "extra.toml").write_text(extra)
    toml = project / "buildlog.toml"
    text = toml.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "extra.toml"]')
    if extra.startswith("[target.extra]"):
        text = text.replace(
            'request_all = ["tcc_stage3", "hello"]',
            'request_all = ["tcc_stage3", "hello", "extra"]',
        )
    toml.write_text(text)
    metrics = _configure(project, tmp_path / "build")
    assert metrics["errors"], "expected an error"
    assert any(message in e[0] for e in metrics["errors"]), metrics["errors"]


@pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed")
def test_bootstrap_builds_reaches_its_fixpoint_and_runs(configured, capsys):
    subprocess.run(["ninja", "-C", str(configured / "souffle")], check=True, capture_output=True)
    bins = configured / "souffle" / "bin" / REL
    s1, s2, s3 = (bins / f"stage{n}" / "tcc" for n in (1, 2, 3))
    version = subprocess.run([s3, "-v"], check=True, capture_output=True, text=True).stdout
    assert version.startswith("tcc version 0.9.28rc")
    # stage 1 is clang's code, stage 2 and 3 tcc's: the fixpoint
    assert s1.read_bytes() != s2.read_bytes()
    assert s2.read_bytes() == s3.read_bytes()
    out = subprocess.run([bins / "hello"], check=True, capture_output=True, text=True).stdout
    assert out == "hello from tcc, 6 * 7 = 42\n"
    capsys.readouterr()
    assert main(["test", "-C", str(configured), str(PROJECT)]) == 0
    out = capsys.readouterr().out
    assert "passed" in out and "bootstrap_fixpoint" in out
    assert "2 passed, 0 skipped, 0 failed, 0 not run" in out
