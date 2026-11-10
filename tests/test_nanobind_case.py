# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""nanobind case study (case-studies/nanobind/README.md): nanobind_example's
extension module, and libnanobind from source, built by buildlog for the
interpreters a user file names, compared with scikit-build-core's build
(case-studies/nanobind/baseline), imported and tested with upstream's
tests, and packed into wheels.

The unit tests (the probes, the wheel metadata, the backend's user file)
need no checkout. The integration tests need the pinned nanobind_example
checkout (BUILDLOG_NANOBIND_EXAMPLE_SRC, default the submodule
case-studies/nanobind/upstream) and the case study's venvs
(baseline/run-baseline.sh: venv313 and venv311 under BUILDLOG_NANOBIND_VENVS,
default build/case-nanobind in the repository, each with nanobind 3.1).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from buildlog_python import backend, probes, wheel

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

sys.path.insert(0, str(Path(__file__).parent))
from host import MACOS, baseline_dir

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "nanobind"
PROJECT = CASE / "project"
PYPROJECT_DEMO = CASE / "pyproject"
TOOLCHAIN = PROJECT / "presets" / "apple-clang-arm64.toml"
INTERPRETERS = PROJECT / "presets" / "interpreters.toml"
BASELINE = baseline_dir(CASE)
SRC = Path(
    os.path.expanduser(os.environ.get("BUILDLOG_NANOBIND_EXAMPLE_SRC", str(CASE / "upstream")))
)
VENV_DIR = Path(
    os.path.expanduser(
        os.environ.get("BUILDLOG_NANOBIND_VENVS", str(ROOT / "build" / "case-nanobind"))
    )
)
VENVS = {t: VENV_DIR / f"venv{t}" for t in ("313", "311")}
CFG = {t: f"arm64-macos.release.none.cp{t}" for t in VENVS}


def _ext_suffix(tag: str) -> str:
    """The venv's interpreter's own EXT_SUFFIX (CPython's rules for the host)."""
    return subprocess.run(
        [
            str(VENVS[tag] / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


PLATFORM_TAG = "macosx_13_0_arm64" if MACOS else "linux_aarch64"

needs_case = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "src" / "nanobind_example_ext.cpp").exists(),
        reason="the pinned nanobind_example checkout is absent",
    ),
    pytest.mark.skipif(
        not all((v / "bin" / "python").exists() for v in VENVS.values()),
        reason=f"the case study's venvs (venv313, venv311) are absent from {VENV_DIR} (BUILDLOG_NANOBIND_VENVS)",
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


# ---- unit: probes, metadata, backend ----


def test_interp_probe_reports_this_interpreter():
    rows = probes.python_interp(None, "r", (sys.executable,), {})["res_python_interp"]
    info = {k: v for _, k, v in rows}
    assert info["found"] == "yes"
    assert info["version_minor"] == str(sys.version_info.minor)
    assert info["implementation"] == sys.implementation.name
    import sysconfig

    assert info["ext_suffix"] == sysconfig.get_config_var("EXT_SUFFIX")
    assert info["abi3_suffix"] in (".abi3.so", "none")
    assert Path(info["include_dir"], "Python.h").is_file()


def test_interp_probe_on_a_missing_interpreter_is_not_found(tmp_path):
    rows = probes.python_interp(None, "r", (str(tmp_path / "python"),), {})
    assert rows == {"res_python_interp": [("r", "found", "no")]}


def test_package_probe_checks_the_specifier():
    ok = probes.python_package(None, "r", (sys.executable, "packaging", "packaging", ">=20"), {})
    info = {k: v for _, k, v in ok["res_python_package"]}
    assert info["found"] == "yes" and info["satisfies"] == "yes"
    assert Path(info["dir"], "version.py").is_file()
    too_new = probes.python_package(
        None, "r", (sys.executable, "packaging", "packaging", ">=999"), {}
    )
    assert ("r", "satisfies", "no") in too_new["res_python_package"]
    absent = probes.python_package(None, "r", (sys.executable, "no-such-dist", "no_such", ""), {})
    assert ("r", "found", "no") in absent["res_python_package"]


def test_core_metadata_from_pyproject(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "LICENSE").write_text("MIT\n")
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "Demo.Pkg"
version = "1.2"
description = "d"
readme = "README.md"
requires-python = ">=3.8"
authors = [{ name = "A", email = "a@x" }, { name = "B" }]
dependencies = ["numpy"]
"""
    )
    name, version, text, licenses = wheel.core_metadata(tmp_path / "pyproject.toml")
    assert (name, version) == ("Demo.Pkg", "1.2")
    assert text.splitlines()[:4] == [
        "Metadata-Version: 2.1",
        "Name: Demo.Pkg",
        "Version: 1.2",
        "Summary: d",
    ]
    assert "Author-Email: A <a@x>" in text and "Author: B" in text
    assert "Requires-Dist: numpy" in text
    assert text.endswith("Description-Content-Type: text/markdown\n\n# demo\n")
    assert [p.name for p in licenses] == ["LICENSE"]
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndynamic = ["version"]\n')
    with pytest.raises(wheel.WheelError, match="dynamic"):
        wheel.core_metadata(tmp_path / "pyproject.toml")


def test_backend_user_file_names_the_frontends_interpreter():
    data = tomllib.loads(backend.user_file('/p/"py"/bin/python', "arm64-macos"))
    assert data["python_interpreter"] == [["pep517", '/p/"py"/bin/python']]
    assert data["config_decl"] == [["pep517", "arm64-macos"]]
    assert ["pep517", "python.interpreter", "pep517"] in data["config_decl_setting"]


# ---- integration ----


def _configure(project: Path, build: Path, *users: Path) -> dict:
    _, metrics = configure_project(
        project, build, package_paths=[CASE], user_files=[TOOLCHAIN, *users]
    )
    assert metrics["errors"] == []
    return metrics


def _ninja(build: Path, *args: str) -> str:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _facts(build: Path, rel: str) -> list[list[str]]:
    text = (build / "souffle" / "facts" / f"{rel}.csv").read_text()
    return [line.split("\t") for line in text.splitlines()]


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    b = tmp_path_factory.mktemp("nanobind") / "build"
    _configure(PROJECT, b, INTERPRETERS)
    _ninja(b, "-j4")
    return b


def _compiles(lines: list[str]) -> dict[str, dict]:
    """source basename -> defines and flags of each compile, for the module's
    own source and libnanobind's; the cp313 (abi3) configuration's for ours."""
    out = {}
    for line in lines:
        words = shlex.split(line)
        if "-c" not in words or ("cp311" in line):
            continue
        src = Path(words[words.index("-c") + 1]).name
        out[src] = {
            "defines": {w[2:] for w in words if w.startswith("-D")},
            "flags": {w for w in words if w.startswith(("-f", "-O"))},
        }
    return out


def _apply(fn):
    """needs_case's marks, on an integration test."""
    for m in needs_case:
        fn = m(fn)
    return fn


@_apply
def test_compiles_match_the_abi3_baseline(build):
    ours = _compiles(_ninja(build, "-t", "commands").splitlines())
    theirs = _compiles((BASELINE / "313" / "commands.txt").read_text().splitlines())
    # The module's source and libnanobind's 11.
    assert set(ours) == set(theirs) and len(ours) == 12
    for src, cmake in theirs.items():
        mine = ours[src]
        # CMake's per-target export define is the one difference.
        assert mine["defines"] == cmake["defines"] - {"nanobind_example_ext_EXPORTS"}, src
        assert "Py_LIMITED_API=0x030C0000" in mine["defines"], src
        # The same optimization and code-generation flags.
        assert mine["flags"] == cmake["flags"], src


@_apply
def test_link_leaves_python_undefined_by_nanobinds_list(build):
    module_flag = "-bundle" if MACOS else "-shared"
    lines = [
        x
        for x in _ninja(build, "-t", "commands").splitlines()
        if f" {module_flag} " in x and "nanobind_example_ext" in x
    ]
    assert len(lines) == 2
    for line in lines:
        words = shlex.split(line)
        has_list = any(w.startswith("-Wl,@") and w.endswith("darwin-ld-cpython.sym") for w in words)
        if MACOS:
            assert has_list
            assert {"-Wl,-dead_strip", "-Wl,-x", "-Wl,-S"} <= set(words)
        else:
            # nanobind_link_options and the Mach-O strip flags are macOS's.
            assert not has_list and "-Wl,-dead_strip" not in words
            assert {"-Wl,-s", "-Wl,--gc-sections"} <= set(words)
        assert not any("libpython" in w or w.startswith("-lpython") for w in words)
        assert "-Wl,-undefined,dynamic_lookup" not in words
    # The symbol list is an input of the link, so a changed nanobind relinks.
    query = _ninja(build, "-t", "query", f"lib/{CFG['313']}/nanobind_example_ext.abi3.so")
    assert ("darwin-ld-cpython.sym" in query) == MACOS


@_apply
def test_extension_names_abi_and_wheel_tags(build):
    staged = {r[1] for r in _facts(build, "python_staged")}
    assert staged == {
        "nanobind_example/__init__.py",
        "nanobind_example/nanobind_example_ext.abi3.so",
        f"nanobind_example/nanobind_example_ext{_ext_suffix('311')}",
    }
    assert MACOS == (_ext_suffix("311") == ".cpython-311-darwin.so")
    tags = {r[0]: tuple(r[2:]) for r in _facts(build, "python_wheel")}
    assert tags == {
        "release-cp313": ("cp312", "abi3", PLATFORM_TAG),
        "release-cp311": ("cp311", "cp311", PLATFORM_TAG),
    }
    so = build / "souffle" / "python" / CFG["313"] / "nanobind_example"
    nm = ["nm", "-gU"] if MACOS else ["nm", "-gD", "--defined-only"]
    exports = subprocess.run(
        [*nm, str(so / "nanobind_example_ext.abi3.so")],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    names = {line.split()[-1] for line in exports.splitlines()}
    assert ("_" if MACOS else "") + "PyInit_nanobind_example_ext" in names
    # What the stable-ABI baseline exports: nothing else.
    assert names == _baseline_exports("313")


@_apply
@pytest.mark.parametrize("tag", ["313", "311"])
def test_upstream_tests_pass_on_the_staged_tree(build, tag):
    env = dict(os.environ, PYTHONPATH=str(build / "souffle" / "python" / CFG[tag]))
    out = subprocess.run(
        [
            str(VENVS[tag] / "bin" / "python"),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "tests",
        ],
        cwd=SRC,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 passed" in out.stdout


@_apply
def test_abi3_module_loads_in_another_interpreter(build):
    # The cp313 configuration's abi3 module, in this (buildlog's) interpreter.
    tree = build / "souffle" / "python" / CFG["313"]
    code = f"import sys; sys.path.insert(0, {str(tree)!r}); import nanobind_example as m; print(m.add(2, 3))"
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert out.stdout.strip() == "5", out.stderr


@_apply
def test_wheels_match_the_baseline_but_the_platform_tag(build, tmp_path):
    for decl, tag in (("release-cp313", "313"), ("release-cp311", "311")):
        whl = wheel.build_wheel(build, decl, SRC / "pyproject.toml", tmp_path / "dist")
        base = (BASELINE / tag / "wheel-name.txt").read_text().strip()
        assert whl.name == base.replace("macosx_26_0", "macosx_13_0")
        with zipfile.ZipFile(whl) as zf:
            names = sorted(zf.namelist())
            info = "nanobind_example-0.0.1.dist-info"
            assert zf.read(f"{info}/METADATA").decode() == (BASELINE / tag / "METADATA").read_text()
            record = zf.read(f"{info}/RECORD").decode()
            zf.extractall(tmp_path / tag)
        want = sorted(
            line.removeprefix("./")
            for line in (BASELINE / tag / "wheel-files.txt").read_text().split()
        )
        assert names == want
        assert all(line.count(",") == 2 for line in record.splitlines())
        env = dict(os.environ, PYTHONPATH=str(tmp_path / tag))
        out = subprocess.run(
            [
                str(VENVS[tag] / "bin" / "python"),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "tests",
            ],
            cwd=SRC,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, out.stdout + out.stderr


@_apply
def test_pyproject_entry_point_builds_the_same(build, tmp_path):
    other = tmp_path / "build"
    _configure(PYPROJECT_DEMO, other, INTERPRETERS)
    assert _ninja(other, "-t", "commands") == _ninja(build, "-t", "commands")


@_apply
def test_why_explains_the_limited_api_define(build):
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "buildlog",
            "why",
            "-C",
            str(build),
            "--flag=-DPy_LIMITED_API=0x030C0000",
            "--output",
            f"obj/{CFG['313']}/nanobind_example_ext/src/nanobind_example_ext.cpp.o",
            str(PROJECT),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'python_py_api("cp312")' in out and "spec.toml" in out
    assert 'python_info(‹arm64-macos.release.none.cp313›, "implementation", "cpython")' in out
    assert "res_python_interp(" in out


@_apply
def test_a_missing_interpreter_is_an_error(tmp_path, capsys):
    user = tmp_path / "user.toml"
    user.write_text(
        f'python_interpreter = [["nope", "{tmp_path}/python"]]\n'
        'config_decl = [["r", "arm64-macos"]]\n'
        'config_decl_setting = [["r", "native.opt", "release"], ["r", "python.interpreter", "nope"]]\n'
    )
    with pytest.raises(SystemExit):
        configure_project(
            PROJECT, tmp_path / "build", package_paths=[CASE], user_files=[TOOLCHAIN, user]
        )
    errors = [x for x in capsys.readouterr().err.splitlines() if x.startswith("error:")]
    # Only the cause: no follow-on errors about what it would have provided.
    assert len(errors) == 1 and "python interpreter does not run: nope" in errors[0]


def _baseline_exports(tag: str) -> set[str]:
    section = (BASELINE / tag / "extension.txt").read_text().split("=====")[1]
    return {line.split()[-1] for line in section.splitlines()[1:] if line.strip()}


def _baseline_undefined(tag: str) -> set[str]:
    out, section = set(), ""
    for line in (BASELINE / tag / "extension.txt").read_text().splitlines():
        if line.startswith("====="):
            section = line
        elif "undefined Python" in section:
            out.add(line.strip())
    return out


@_apply
@pytest.mark.parametrize("tag", ["313", "311"])
def test_python_api_left_undefined_as_in_the_baseline(build, tag):
    (so,) = (build / "souffle" / "python" / CFG[tag] / "nanobind_example").glob("*.so")
    argv = ["nm", "-u"] if MACOS else ["nm", "-D", "-u"]
    nm = subprocess.run([*argv, str(so)], capture_output=True, text=True, check=True).stdout
    ours = {s for s in nm.split() if "Py" in s and s[0] in "_P"}
    assert ours == _baseline_undefined(tag)
    if MACOS:
        assert len(ours) == {"313": 100, "311": 91}[tag]
