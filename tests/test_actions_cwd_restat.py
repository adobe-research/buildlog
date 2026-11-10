# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Working directory, restat and `buildlog -E` builtins (L0).

A small project-local rule package states the actions directly: a command
run in another directory (its rooted paths rebased onto it), a generated
file copied into place with `buildlog -E copy_if_different` and restat, so
an unchanged result doesn't rebuild its consumers, and a program copied with
`buildlog -E copy` and run from its own directory.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog import builtins
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")

MANIFEST = """\
name = "steps"
version = "0.1.0"
rank = 1
layers = ["steps.dl"]
extends = ["action", "action_arg", "action_in", "action_stdout", "action_cwd", "action_restat",
           "action_builtin", "default_output"]
"""
STEPS = """\
// cat in.txt > gen/value.tmp, run from out:work (not an output directory).
action("out:gen/value.tmp", "CAT").
action_arg("out:gen/value.tmp", 0, "", "/bin/cat").
action_arg("out:gen/value.tmp", 1, "", "src:in.txt").
action_in("out:gen/value.tmp", "src:in.txt", "explicit").
action_stdout("out:gen/value.tmp", "out:gen/value.tmp").
action_cwd("out:gen/value.tmp", "out:work").
// Copy into place only when different; restat prunes the consumers.
action("out:gen/value", "COPY").
action_builtin("out:gen/value", "copy_if_different").
action_arg("out:gen/value", 1, "", "out:gen/value.tmp").
action_arg("out:gen/value", 2, "", "out:gen/value").
action_in("out:gen/value", "out:gen/value.tmp", "explicit").
action_restat("out:gen/value").
action("out:final", "CP").
action_arg("out:final", 0, "", "/bin/cp").
action_arg("out:final", 1, "", "out:gen/value").
action_arg("out:final", 2, "", "out:final").
action_in("out:final", "out:gen/value", "explicit").
// A script copied into the build tree, run from its own directory.
action("out:tools/hello.sh", "COPY").
action_builtin("out:tools/hello.sh", "copy").
action_arg("out:tools/hello.sh", 1, "", "src:hello.sh").
action_arg("out:tools/hello.sh", 2, "", "out:tools/hello.sh").
action_in("out:tools/hello.sh", "src:hello.sh", "explicit").
action("out:hello.txt", "HELLO").
action_arg("out:hello.txt", 0, "", "out:tools/hello.sh").
action_in("out:hello.txt", "out:tools/hello.sh", "implicit").
action_stdout("out:hello.txt", "out:hello.txt").
action_cwd("out:hello.txt", "out:tools").
// Run in the source tree: rebasing leaves the ninja directory.
action("out:from-src.txt", "CAT").
action_arg("out:from-src.txt", 0, "", "/bin/cat").
action_arg("out:from-src.txt", 1, "", "src:in.txt").
action_in("out:from-src.txt", "src:in.txt", "explicit").
action_stdout("out:from-src.txt", "out:from-src.txt").
action_cwd("out:from-src.txt", "src:").
default_output("out:from-src.txt").
default_output("out:final").
default_output("out:hello.txt").
"""


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "packages" / "steps").mkdir(parents=True)
    (proj / "packages" / "steps" / "manifest.toml").write_text(MANIFEST)
    (proj / "packages" / "steps" / "steps.dl").write_text(STEPS)
    (proj / "buildlog.toml").write_text('uses = ["steps"]\nspec = ["spec.toml"]\n')
    (proj / "spec.toml").write_text("")
    (proj / "in.txt").write_text("one\n")
    (proj / "hello.sh").write_text('#!/bin/sh\necho "hello from $(basename "$PWD")"\n')
    os.chmod(proj / "hello.sh", 0o755)
    return proj


def _ninja(d):
    return subprocess.run(
        ["ninja", "-C", str(d)], check=True, capture_output=True, text=True
    ).stdout


def test_cwd_restat_and_builtins_are_emitted_and_run(tmp_path):
    proj = _project(tmp_path)
    build = tmp_path / "build"
    configure_project(proj, build, update_lock=True)
    nd = build / "souffle"
    text = (nd / "build.ninja").read_text()
    e = f"{sys.executable} -m buildlog -E"
    assert "  cmd = cd work && /bin/cat ../../../proj/in.txt > ../gen/value.tmp\n" in text
    assert f"  cmd = {e} copy_if_different gen/value.tmp gen/value\n  restat = 1\n" in text
    assert f"  cmd = {e} copy ../../proj/hello.sh tools/hello.sh\n" in text
    assert "  cmd = cd tools && ./hello.sh > ../hello.txt\n" in text
    assert "  cmd = cd ../../proj && /bin/cat in.txt > ../build/souffle/from-src.txt\n" in text
    # The cwd that is no output's directory exists before ninja runs.
    assert (nd / "work").is_dir()

    _ninja(nd)
    assert (nd / "final").read_text() == "one\n"
    assert (nd / "hello.txt").read_text() == "hello from tools\n"
    assert (nd / "from-src.txt").read_text() == "one\n"
    assert os.access(nd / "tools" / "hello.sh", os.X_OK)

    # Same content, newer source: the copy leaves gen/value alone, and restat
    # keeps `final` from rebuilding.
    old = (proj / "in.txt").stat().st_mtime - 10
    for f in ("gen/value.tmp", "gen/value", "final"):
        os.utime(nd / f, (old, old))
    before = (nd / "final").stat().st_mtime_ns
    os.utime(proj / "in.txt")
    out = _ninja(nd)
    assert "CAT" in out and "COPY" in out and " CP " not in out
    assert (nd / "final").stat().st_mtime_ns == before
    assert "no work to do" in _ninja(nd)

    (proj / "in.txt").write_text("two\n")
    _ninja(nd)
    assert (nd / "final").read_text() == "two\n"


def test_builtin_names_match_l0():
    l0 = (Path(builtins.__file__).parent / "layers" / "core" / "L0_actions.dl").read_text()
    declared = {ln.split('"')[1] for ln in l0.splitlines() if ln.startswith("builtin_command(")}
    assert declared == set(builtins.COMMANDS)


def test_builtins(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("x")
    assert builtins.main(["copy_if_different", str(a), str(b)]) == 0
    assert b.read_text() == "x"
    old = b.stat().st_mtime_ns
    os.utime(a)
    assert builtins.main(["copy_if_different", str(a), str(b)]) == 0
    assert b.stat().st_mtime_ns == old
    assert builtins.main(["make_directory", str(tmp_path / "d/e")]) == 0
    assert builtins.main(["touch", str(tmp_path / "d/e/f")]) == 0
    assert builtins.main(["create_symlink", "a", str(tmp_path / "l")]) == 0
    assert builtins.main(["create_symlink", "b", str(tmp_path / "l")]) == 0
    assert os.readlink(tmp_path / "l") == "b"
    assert builtins.main(["remove", str(tmp_path / "l"), str(tmp_path / "missing")]) == 0
    assert not os.path.lexists(tmp_path / "l")
    assert builtins.main(["copy", str(tmp_path / "missing"), str(b)]) == 1
    assert builtins.main(["copy", str(a)]) == 2
    assert builtins.main(["frobnicate"]) == 2
    # The console entry point dispatches -E before parsing anything else.
    subprocess.run(
        [sys.executable, "-m", "buildlog", "-E", "touch", str(tmp_path / "g")], check=True
    )
    assert (tmp_path / "g").exists()


JOIN_MANIFEST = MANIFEST.replace(
    '"default_output"]', '"default_output", "action_arg_join", "action_arg_part"]'
)
JOIN_STEPS = """\
// echo -l<a>,<b> > list.txt, run from out:work: each part is a path the
// translator rebases, and the word is one quoted argv token.
action("out:list.txt", "ECHO").
action_arg("out:list.txt", 0, "", "/bin/echo").
action_arg_join("out:list.txt", 1, "-l", ",").
action_arg_part("out:list.txt", 1, 1, "src:in.txt").
action_arg_part("out:list.txt", 1, 0, "out:gen/a b").
action_stdout("out:list.txt", "out:list.txt").
action_cwd("out:list.txt", "out:work").
default_output("out:list.txt").
"""


def test_joined_word_of_paths(tmp_path):
    proj = _project(tmp_path)
    pkg = proj / "packages" / "steps"
    (pkg / "manifest.toml").write_text(JOIN_MANIFEST)
    (pkg / "steps.dl").write_text(JOIN_STEPS)
    build = tmp_path / "build"
    configure_project(proj, build, update_lock=True)
    text = (build / "souffle" / "build.ninja").read_text()
    assert (
        "  cmd = cd work && /bin/echo -l'../gen/a b,../../../proj/in.txt' > ../list.txt\n" in text
    )


def test_joined_word_errors(tmp_path, capsys):
    proj = _project(tmp_path)
    pkg = proj / "packages" / "steps"
    (pkg / "manifest.toml").write_text(JOIN_MANIFEST)
    bad = (
        JOIN_STEPS
        + 'action_arg("out:list.txt", 1, "", "x").\naction_arg_part("out:list.txt", 2, 0, "y").\n'
    )
    (pkg / "steps.dl").write_text(bad)
    with pytest.raises(SystemExit):
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    err = capsys.readouterr().err
    assert "action argv position used by a joined word and another argument" in err
    assert "argv part without its joined word" in err
