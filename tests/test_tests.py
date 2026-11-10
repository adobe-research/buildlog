# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests as typed facts (core L1 `test`, L0 `test_case`) and `buildlog test`,
ctest's semantics: literal and target-reference arguments, environment,
labels, pass/fail/skip regexes, serial cases, cross configurations listed as
not run, and the errors."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests import host
from tests.test_cpp_mechanisms import configure, configure_fails, needs_souffle, project

REL = "arm64-macos.release.none"

MAIN = r"""#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <unistd.h>
int main(int argc, char** argv) {
  const char* mode = argc > 1 ? argv[1] : "";
  if (!strcmp(mode, "plugin")) {
    void* h = dlopen(argv[2], RTLD_NOW);
    if (!h) { std::puts(dlerror()); return 1; }
    auto f = (int (*)())dlsym(h, "value");
    std::printf("value %d\n", f());
    return f() == 7 ? 0 : 1;
  }
  if (!strcmp(mode, "env")) { std::printf("%s\n", getenv("GREETING")); return 0; }
  if (!strcmp(mode, "cwd")) { char b[4096]; std::printf("%s\n", getcwd(b, sizeof b)); return 0; }
  if (!strcmp(mode, "skip")) { std::puts("[SKIP] not today"); return 0; }
  if (!strcmp(mode, "noisy")) { std::puts("Success!"); return 3; }
  if (!strcmp(mode, "quiet")) { std::puts("done"); return 0; }
  if (!strcmp(mode, "error")) { std::puts("Success! ERROR"); return 0; }
  return 1;
}
"""

SPEC = """
target = [["prog"], ["plug"]]
native_kind = [["prog", "executable"], ["plug", "module_library"]]
cxx_source = [["prog", "main.cpp"], ["plug", "plug.cpp"]]
request = [["prog", "rel"]]
test = [["t_plugin", "prog"], ["t_env", "prog"], ["t_cwd", "prog"], ["t_skip", "prog"],
  ["t_noisy", "prog"], ["t_quiet", "prog"], ["t_error", "prog"], ["t_fails", "prog"]]
test_arg = [["t_plugin", 0, "plugin"], ["t_env", 0, "env"], ["t_cwd", 0, "cwd"],
  ["t_skip", 0, "skip"], ["t_noisy", 0, "noisy"], ["t_quiet", 0, "quiet"],
  ["t_error", 0, "error"], ["t_fails", 0, "nope"]]
test_arg_ref = [["t_plugin", 1, "plug"]]
test_env = [["t_env", "GREETING", "hello there"]]
test_label = [["t_plugin", "plugins"], ["t_env", "basic"], ["t_cwd", "basic"]]
test_regex = [["t_skip", "skip", "\\\\[SKIP\\\\]"], ["t_noisy", "pass", "Success!"],
  ["t_quiet", "pass", "Success!"], ["t_error", "pass", "Success!"], ["t_error", "fail", "ERROR"]]
test_serial = [["t_cwd"]]
"""

FILES = {"main.cpp": MAIN, "plug.cpp": 'extern "C" int value() { return 7; }\n'}


def buildlog(*args, check=False):
    return subprocess.run(
        [sys.executable, "-m", "buildlog", *args], capture_output=True, text=True, check=check
    )


def cases_json(build):
    return json.loads((build / "souffle" / "tests.json").read_text())


@needs_souffle
def test_tests_are_typed_facts_and_buildlog_test_runs_them(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    doc = cases_json(build)
    assert doc["format"] == "buildlog-tests" and doc["version"] == 1
    by = {t["name"]: t for t in doc["tests"]}
    assert sorted(by) == sorted(
        ["t_plugin", "t_env", "t_cwd", "t_skip", "t_noisy", "t_quiet", "t_error", "t_fails"]
    )
    plugin = by["t_plugin"]
    # The program and the referenced module, in the test's configuration,
    # rebased onto the case's working directory; both are needed and built.
    assert plugin["config"] == REL
    assert plugin["cwd"] == f"test/{REL}/t_plugin"
    assert plugin["argv"] == [
        f"../../../bin/{REL}/prog",
        "plugin",
        f"../../../lib/{REL}/libplug.so",
    ]
    assert plugin["needs"] == [f"bin/{REL}/prog", f"lib/{REL}/libplug.so"]
    assert plugin["labels"] == ["plugins"] and plugin["not_run"] == []
    assert by["t_env"]["env"] == {"GREETING": "hello there"}
    assert by["t_cwd"]["serial"] and not by["t_env"]["serial"]
    assert by["t_error"]["pass"] == ["Success!"] and by["t_error"]["fail"] == ["ERROR"]
    # The plugin is built because the test names it, not because anything
    # requested it; nothing else in the graph changes.
    assert f"lib/{REL}/libplug.so" in (build / "souffle" / "build.ninja").read_text()

    out = tmp_path / "results.json"
    r = buildlog("test", "-C", str(build), "--json", str(out), "--output-on-failure")
    assert r.returncode == 1, r.stdout + r.stderr  # t_error and t_fails fail
    res = {x["name"]: x["status"] for x in json.loads(out.read_text())["results"]}
    assert res == {
        "t_plugin": "passed",
        "t_env": "passed",
        "t_cwd": "passed",
        "t_skip": "skipped",
        "t_noisy": "passed",  # a pass regex decides; the exit status is ignored
        "t_quiet": "failed",  # exit 0, but no pass regex matched
        "t_error": "failed",  # a fail regex wins over a pass regex
        "t_fails": "failed",
    }
    assert "4 passed, 1 skipped, 3 failed, 0 not run" in r.stdout
    cwd_dir = build / "souffle" / "test" / REL / "t_cwd"
    assert cwd_dir.is_dir()

    # Selection by name and label, and --no-build.
    r = buildlog("test", "-C", str(build), "-L", "^basic$", "-E", "cwd", "--no-build")
    assert r.returncode == 0 and "1 passed, 0 skipped, 0 failed" in r.stdout
    r = buildlog("test", "-C", str(build), "-R", "^t_(plugin|env)$", "--list")
    assert r.stdout.split("\n")[:2] == [f"t_env [{REL}]", f"t_plugin [{REL}]"]


@host.cross
@needs_souffle
def test_a_cross_configurations_tests_are_listed_not_run(tmp_path):
    spec = SPEC.replace('request = [["prog", "rel"]]', 'request = [["prog", "rel"], ["prog", "x"]]')
    spec += '\nconfig_decl = [["rel", "arm64-macos"], ["x", "x86_64-macos"]]\n'
    proj = project(tmp_path, "", FILES)
    text = (proj / "spec.toml").read_text().replace('config_decl = [["rel", "arm64-macos"]]', "")
    (proj / "spec.toml").write_text(text + spec)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cases = [t for t in cases_json(build)["tests"] if t["name"] == "t_plugin"]
    assert [t["config"] for t in cases] == [REL, "x86_64-macos.debug.none"]
    cross = cases[1]
    assert cross["not_run"] == ["built for x86_64-macos; the build machine is arm64-macos"]
    r = buildlog("test", "-C", str(build), "-R", "^t_plugin$")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed, 0 skipped, 0 failed, 1 not run" in r.stdout


@host.cross
@needs_souffle
@pytest.mark.parametrize("via", ["", "arch -x86_64"])
def test_a_platform_the_user_says_runs_has_its_tests_run(tmp_path, via):
    """platform_runs_on(p, host, via), a user fact,
    never inferred: the cross configuration's tests run, through via."""
    from buildlog.engine import configure_project

    spec = SPEC.replace('request = [["prog", "rel"]]', 'request = [["prog", "x"]]')
    proj = project(tmp_path, "", FILES)
    text = (proj / "spec.toml").read_text().replace('config_decl = [["rel", "arm64-macos"]]', "")
    (proj / "spec.toml").write_text(text + spec + '\nconfig_decl = [["x", "x86_64-macos"]]\n')
    user = tmp_path / "user.toml"
    user.write_text(f'platform_runs_on = [["x86_64-macos", "arm64-macos", "{via}"]]\n')
    build = tmp_path / "build"
    _, metrics = configure_project(proj, build, update_lock=True, user_files=[user])
    assert metrics["errors"] == []
    case = next(t for t in cases_json(build)["tests"] if t["name"] == "t_plugin")
    assert case["config"] == "x86_64-macos.debug.none"
    assert case["not_run"] == [] and case["launcher"] == via.split()
    r = buildlog("test", "-C", str(build), "-R", "^t_(plugin|env)$", "--list")
    assert ("(via arch -x86_64)" in r.stdout) == bool(via)
    r = buildlog("test", "-C", str(build), "-R", "^t_(plugin|env)$")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed, 0 skipped, 0 failed, 0 not run" in r.stdout


@needs_souffle
@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ('[["arm64-macos", "arm64-macos", ""]]', "platform_runs_on a platform itself"),
        ('[["pdp11-unix", "arm64-macos", ""]]', "platform_runs_on an unknown platform"),
        (
            '[["x86_64-macos", "arm64-macos", ""], ["x86_64-macos", "arm64-macos", "arch -x86_64"]]',
            "platform_runs_on twice",
        ),
    ],
)
def test_platform_runs_on_errors(tmp_path, capsys, rows, message):
    from buildlog.engine import configure_project

    proj = project(tmp_path, SPEC, FILES)
    user = tmp_path / "user.toml"
    user.write_text(f"platform_runs_on = {rows}\n")
    with pytest.raises(SystemExit):
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True, user_files=[user])
    assert message in capsys.readouterr().err


@needs_souffle
def test_platform_runs_on_is_the_users(tmp_path):
    from buildlog.engine import configure_project
    from buildlog.errors import SpecError

    proj = project(
        tmp_path, SPEC + 'platform_runs_on = [["x86_64-macos", "arm64-macos", ""]]\n', FILES
    )
    with pytest.raises(SpecError, match="platform_runs_on is a user relation"):
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)


@host.cross
@needs_souffle
def test_a_test_requests_its_program(tmp_path):
    """Bug B1: nothing requests prog, yet its tests are built and listed in every
    active configuration, and test_when narrows one to where its condition holds."""
    spec = (
        'target = [["prog"], ["plug"]]\n'
        'native_kind = [["prog", "executable"], ["plug", "module_library"]]\n'
        'cxx_source = [["prog", "main.cpp"], ["plug", "plug.cpp"]]\n'
        'test = [["t_quiet", "prog"], ["t_x86", "prog"]]\n'
        'test_arg = [["t_quiet", 0, "quiet"], ["t_x86", 0, "quiet"]]\n'
        'condition = [["on_x86", "$Eq(platform, x86_64-macos)"]]\n'
        'test_when = [["t_x86", "on_x86"]]\n'
        'config_decl = [["rel", "arm64-macos"], ["x", "x86_64-macos"]]\n'
    )
    proj = project(tmp_path, "", FILES)
    text = (proj / "spec.toml").read_text().replace('config_decl = [["rel", "arm64-macos"]]', "")
    (proj / "spec.toml").write_text(text + spec)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cases = sorted((t["name"], t["config"]) for t in cases_json(build)["tests"])
    assert cases == [("t_quiet", REL), ("t_quiet", "x86_64-macos.debug.none"),
                     ("t_x86", "x86_64-macos.debug.none")]  # fmt: skip
    default = [ln for ln in (build / "souffle" / "build.ninja").read_text().splitlines()
               if ln.startswith("default ")]  # fmt: skip
    assert f"bin/{REL}/prog" in default[0] and "bin/x86_64-macos.debug.none/prog" in default[0]
    r = buildlog("test", "-C", str(build))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed, 0 skipped, 0 failed, 2 not run" in r.stdout


@needs_souffle
def test_test_cwd_roots_a_tests_working_directory_in_the_source_tree(tmp_path):
    proj = project(
        tmp_path,
        'target = [["prog"]]\nnative_kind = [["prog", "executable"]]\n'
        'cxx_source = [["prog", "main.cpp"]]\nrequest = [["prog", "rel"]]\n'
        'test = [["t_src", "prog"], ["t_default", "prog"]]\n'
        'test_arg = [["t_src", 0, "cwd"], ["t_default", 0, "cwd"]]\n'
        'test_cwd = [["t_src", "src:data"]]\n',
        FILES,
    )
    (proj / "src" / "data").mkdir()
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    by = {t["name"]: t for t in cases_json(build)["tests"]}
    assert by["t_src"]["cwd"] == "src:data" or by["t_src"]["cwd"].endswith("/data")
    assert by["t_default"]["cwd"] == f"test/{REL}/t_default"
    r = buildlog("test", "-C", str(build), "-R", "t_src")
    assert r.returncode == 0, r.stdout + r.stderr


@needs_souffle
def test_no_tests_no_file(tmp_path):
    proj = project(
        tmp_path,
        'target = [["prog"]]\nnative_kind = [["prog", "executable"]]\n'
        'cxx_source = [["prog", "main.cpp"]]\nrequest = [["prog", "rel"]]\n',
        FILES,
    )
    build, _ = configure(proj, tmp_path)
    assert not (build / "souffle" / "tests.json").exists()
    r = buildlog("test", "-C", str(build))
    assert r.returncode == 0 and "no tests" in r.stdout


@needs_souffle
@pytest.mark.parametrize(
    "extra, message",
    [
        ('test = [["t", "nosuch"]]', "unknown target"),
        ('test = [["t", "prog"]]\ntest_arg_ref = [["t", 0, "nosuch"]]', "unknown target"),
        ('test = [["t", "prog"], ["t", "plug"]]', "test defined twice"),
        ('test_env = [["u", "A", "1"]]', "test setting for an unknown test"),
        ('test = [["t", "prog"]]\ntest_arg = [["t", 0, "a"], ["t", 0, "b"]]', "two test arguments"),
        ('test = [["t", "prog"]]\ntest_arg = [["t", -1, "a"]]', "negative test argument"),
        ('test = [["t", "prog"]]\ntest_regex = [["t", "maybe", "x"]]', "unknown test regex kind"),
        ('test = [["t", "prog"]]\ntest_arg_ref = [["t", 0, "lib"]]', "test has nothing to run"),
        ('test = [["t", "prog"]]\ntest_when = [["t", "nosuch"]]', "unknown condition"),
        (
            'test = [["t", "prog"]]\ntest_cwd = [["t", "src:a"], ["t", "out:b"]]',
            "test has two working directories",
        ),
        ('test = [["t", "prog"]]\ntest_cwd = [["t", "a"]]', "not a rooted path"),
        ('test_cwd = [["u", "src:a"]]', "unknown test"),
    ],
)
def test_test_errors(tmp_path, capsys, extra, message):
    spec = (
        'target = [["prog"], ["plug"], ["lib"]]\n'
        'native_kind = [["prog", "executable"], ["plug", "module_library"]]\n'
        'cxx_source = [["prog", "main.cpp"], ["plug", "plug.cpp"]]\n'
        'request = [["prog", "rel"], ["plug", "rel"]]\n' + extra + "\n"
    )
    proj = project(tmp_path, spec, FILES)
    assert message in configure_fails(proj, tmp_path, capsys)
