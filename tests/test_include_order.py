# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Include-directory order and warnings: a target's own dirs are -I, every dependency's dirs (direct and
transitive, in-project and imported) -isystem, then the system class; within
a class a lexical set, ordered further only by cc_include_before priority
facts. -Wshadow-header is on where the toolchain has it, as a warning; only
the user turns warnings into errors (cc_werror), and an author's -Werror is
an error on a tool the user chooses (a selected toolchain, an imported tool)
unless [author_decides] says why or, for an imported tool, an
import_same_package fact ties it to the library it must match."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")

REL = "arm64-macos.release.none"
LLVM_BIN = Path("/opt/homebrew/opt/llvm@22/bin")
needs_llvm = pytest.mark.skipif(
    not (LLVM_BIN / "clang++").exists(), reason="Homebrew llvm@22 not installed"
)

PINS = """toolchain_pin = [
  ["c", "arm64-macos", "arm64-macos", "{tc}"],
  ["cxx", "arm64-macos", "arm64-macos", "{tc}"],
  ["native-link", "arm64-macos", "arm64-macos", "{tc}"],
]
"""

# app (own dirs own_b, own_a) -> lib1 (interface l1) -> lib2 (interface l2,
# and a system dir aaa_sys that sorts first but belongs to the last class).
LAYERED = """
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
target = [["app"], ["lib1"], ["lib2"]]
native_kind = [["app", "executable"], ["lib1", "library"], ["lib2", "library"]]
cxx_source = [["app", "main.cpp"], ["lib1", "l1.cpp"], ["lib2", "l2.cpp"]]
dep = [["app", "lib1", "private"], ["lib1", "lib2", "public"]]
prop = [
  ["app", "cc.include_dir", "src:own_b", "private"],
  ["app", "cc.include_dir", "src:own_a", "private"],
  ["lib1", "cc.include_dir", "src:l1", "public"],
  ["lib2", "cc.include_dir", "src:l2", "public"],
  ["lib2", "cc.system_include_dir", "src:aaa_sys", "interface"],
]
request = [["app", "rel"]]
"""

HEADERS = {
    "own_a/a.h": "#define A 1\n",
    "own_b/b.h": "#define B 2\n",
    "l1/l1.h": "#define L1 3\n",
    "l2/l2.h": "#define L2 4\n",
    "aaa_sys/sys.h": "#define SYS 5\n",
    "main.cpp": '#include "a.h"\n#include "b.h"\n#include <l1.h>\n#include <l2.h>\n'
    "#include <sys.h>\nint main() { return A + B + L1 + L2 + SYS == 15 ? 0 : 1; }\n",
    "l1.cpp": "int l1() { return 1; }\n",
    "l2.cpp": "int l2() { return 2; }\n",
}


def project(tmp_path, spec, files=None, buildlog_extra="", tc="apple-clang", user_extra=""):
    proj = tmp_path / "proj"
    src = proj / "src"
    src.mkdir(parents=True)
    for name, text in (files or {}).items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(text)
    (proj / "buildlog.toml").write_text(
        f'uses = ["buildlog-cpp"]\nsource_root = {json.dumps(str(src))}\nspec = ["spec.toml"]\n'
        + buildlog_extra
    )
    (proj / "spec.toml").write_text(spec)
    user = tmp_path / "user.toml"
    user.write_text(PINS.format(tc=tc) + user_extra)
    return proj, user


def configure(proj, user, tmp_path, name="build"):
    build = tmp_path / name
    _, metrics = configure_project(proj, build, update_lock=True, user_files=[user])
    return build, metrics


def configure_errors(proj, user, tmp_path):
    build = tmp_path / "build"
    with pytest.raises(SystemExit):
        configure_project(proj, build, update_lock=True, user_files=[user])
    return json.loads((build / "metrics.json").read_text())["errors"]


def compile_cmd(build, obj):
    lines = (build / "souffle" / "build.ninja").read_text().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"build {obj}:"):
            return next(x for x in lines[i + 1 :] if x.startswith("  cmd = "))[8:]
    raise AssertionError(obj)


def includes(cmd):
    """The include dirs of a command, in order, as (flag, dir below src/)."""
    words, out = shlex.split(cmd), []
    for i, w in enumerate(words):
        if w.startswith("-I"):
            out.append(("-I", w[2:].rsplit("src/", 1)[-1]))
        elif w == "-isystem":
            out.append(("-isystem", words[i + 1].rsplit("src/", 1)[-1]))
    return out


def why(build, proj, flag, output, capsys):
    capsys.readouterr()
    assert main(["why", "-C", str(build), "--flag", flag, "--output", output, str(proj)]) == 0
    return capsys.readouterr().out


def test_own_dirs_then_dependencies_then_system_each_lexical(tmp_path):
    proj, user = project(tmp_path, LAYERED, HEADERS)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    app = includes(compile_cmd(build, f"obj/{REL}/app/main.cpp.o"))
    # own -I (lexical, not declaration order); the direct and the transitive
    # dependency's dirs as -isystem; the system class last, though it sorts first.
    assert app == [
        ("-I", "own_a"),
        ("-I", "own_b"),
        ("-isystem", "l1"),
        ("-isystem", "l2"),
        ("-isystem", "aaa_sys"),
    ]
    # lib1's own public dir is -I in its own compile; lib2's reaches it as a dependency's.
    lib1 = includes(compile_cmd(build, f"obj/{REL}/lib1/l1.cpp.o"))
    assert lib1 == [("-I", "l1"), ("-isystem", "l2"), ("-isystem", "aaa_sys")]
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)


def test_a_dir_is_emitted_once_in_its_earliest_class(tmp_path):
    spec = LAYERED.replace(
        '["lib2", "cc.system_include_dir", "src:aaa_sys", "interface"],',
        '["lib2", "cc.system_include_dir", "src:aaa_sys", "interface"],\n'
        '  ["lib2", "cc.system_include_dir", "src:own_a", "interface"],\n'
        '  ["lib2", "cc.system_include_dir", "src:l1", "interface"],',
    )
    proj, user = project(tmp_path, spec, HEADERS)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    cmd = compile_cmd(build, f"obj/{REL}/app/main.cpp.o")
    app = includes(cmd)
    # own_a is app's own dir (cc.include_dir) and a system dir through
    # lib2 (cc.system_include_dir): -I only. l1 is a dependency's dir and
    # a system dir: once, in the dependency class.
    assert app == [
        ("-I", "own_a"),
        ("-I", "own_b"),
        ("-isystem", "l1"),
        ("-isystem", "l2"),
        ("-isystem", "aaa_sys"),
    ]
    assert len(app) == len({d for _, d in app})


def test_priority_facts_order_dirs_within_a_class(tmp_path, capsys):
    spec = LAYERED.replace(
        '["lib1", "cc.include_dir", "src:l1", "public"],',
        '["lib1", "cc.include_dir", "src:l1", "public"],\n  ["lib1", "cc.include_dir", "src:l0", "public"],',
    ) + (
        # l2 before l1 (the rest of the class stays lexical: l0 first); a
        # priority across classes changes nothing (own dirs stay -I, first).
        'cc_include_before = [["src:l2", "src:l1"], ["src:l1", "src:own_a"]]\n'
    )
    proj, user = project(tmp_path, spec, {**HEADERS, "l0/l0.h": "\n"})
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    obj = f"obj/{REL}/app/main.cpp.o"
    assert includes(compile_cmd(build, obj)) == [
        ("-I", "own_a"),
        ("-I", "own_b"),
        ("-isystem", "l0"),
        ("-isystem", "l2"),
        ("-isystem", "l1"),
        ("-isystem", "aaa_sys"),
    ]
    # Provenance: the flag's explanation includes the priority fact and its line.
    text = why(build, proj, f"-isystem {proj}/src/l1", obj, capsys)
    assert 'action_seq_before("out:obj/' in text
    assert 'cc_include_before("src:l2", "src:l1")  ← ' in text


def test_ties_are_lexical_and_the_order_is_transitive(tmp_path):
    # l2 before aaa (absent from the class) before l1: l2 still precedes l1.
    spec = LAYERED + 'cc_include_before = [["src:l2", "src:aaa"], ["src:aaa", "src:l1"]]\n'
    proj, user = project(tmp_path, spec, HEADERS)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    deps = [
        d for f, d in includes(compile_cmd(build, f"obj/{REL}/app/main.cpp.o")) if f == "-isystem"
    ]
    assert deps == ["l2", "l1", "aaa_sys"]


def test_contradicting_priority_facts_are_an_error(tmp_path):
    spec = LAYERED + 'cc_include_before = [["src:l1", "src:l2"], ["src:l2", "src:l1"]]\n'
    proj, user = project(tmp_path, spec, HEADERS)
    errors = configure_errors(proj, user, tmp_path)
    assert sorted(errors) == [
        ["include-dir priority cycle", "src:l1 is ordered before itself (cc_include_before)"],
        ["include-dir priority cycle", "src:l2 is ordered before itself (cc_include_before)"],
    ]


# ---- -Wshadow-header ----------------------------------------------------------

SHADOW = """
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
target = [["app"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
prop = [["app", "cc.include_dir", "src:a", "private"], ["app", "cc.include_dir", "src:b", "private"]]
request = [["app", "rel"]]
"""
SHADOW_FILES = {
    "a/x.h": "#define X 1\n",
    "b/x.h": "#define X 2\n",
    "main.cpp": '#include "x.h"\nint main() { return X == 1 ? 0 : 1; }\n',
}


def test_apple_clang_gets_no_shadow_header_flag(tmp_path):
    # Apple clang 21 rejects -Wshadow-header; the probe says so.
    proj, user = project(tmp_path, SHADOW, SHADOW_FILES)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    assert "shadow-header" not in compile_cmd(build, f"obj/{REL}/app/main.cpp.o")
    cache = json.loads((build / "probe-cache.json").read_text())
    answers = [v["res_cc_warning"] for k, v in cache.items() if json.loads(k)[0] == "cc_warning"]
    toolchain = "apple-clang"
    assert [
        [
            f"cc-warning:{len(toolchain)}:{toolchain}:3:cxx:-Wshadow-header",
            toolchain,
            "cxx",
            "-Wshadow-header",
            "no",
        ]
    ] in answers


@needs_llvm
def test_shadow_header_is_a_warning_where_the_toolchain_has_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUILDLOG_LLVM_BIN", str(LLVM_BIN))
    proj, user = project(
        tmp_path, SHADOW, SHADOW_FILES, tc="llvm", user_extra='cc_werror = [["*", "*"]]\n'
    )
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    obj = f"obj/{REL}/app/main.cpp.o"
    words = compile_cmd(build, obj).split()
    assert "-Wshadow-header" in words and "-Wno-error=shadow-header" in words and "-Werror" in words
    # It warns about the collision and, even under the user's -Werror, does not fail.
    r = subprocess.run(
        ["ninja", "-C", str(build / "souffle")], capture_output=True, text=True, check=True
    )
    assert "multiple candidates for header 'x.h' found" in r.stdout
    # Provenance: the probe that found the toolchain supports it.
    text = why(build, proj, "-Wshadow-header", obj, capsys)
    assert "res_cc_warning(" in text and "probe cc_warning" in text


# ---- -Werror: user only ---------------------------------------------------------


def test_cc_werror_is_the_users_opt_in(tmp_path):
    spec = LAYERED
    proj, user = project(tmp_path, spec, HEADERS, user_extra='cc_werror = [["rel", "lib1"]]\n')
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    assert "-Werror" in compile_cmd(build, f"obj/{REL}/lib1/l1.cpp.o").split()
    assert "-Werror" not in compile_cmd(build, f"obj/{REL}/app/main.cpp.o").split()
    user.write_text(user.read_text().replace('["rel", "lib1"]', '["*", "*"]'))
    build, metrics = configure(proj, user, tmp_path, "build2")
    for obj in ("app/main.cpp.o", "lib1/l1.cpp.o", "lib2/l2.cpp.o"):
        assert "-Werror" in compile_cmd(build, f"obj/{REL}/{obj}").split()
    user.write_text(user.read_text().replace('["*", "*"]', '["nope", "ghost"]'))
    errors = configure_errors(proj, user, tmp_path)
    assert ["unknown config", "nope (cc_werror)"] in errors
    assert ["unknown target", "ghost (cc_werror)"] in errors


def test_cc_werror_in_an_author_spec_is_a_user_relation(tmp_path):
    proj, user = project(tmp_path, LAYERED + 'cc_werror = [["*", "*"]]\n', HEADERS)
    with pytest.raises(ValueError, match="cc_werror is a user relation"):
        configure_project(proj, tmp_path / "build", update_lock=True, user_files=[user])


KIND = "-Werror is a user decision"


def user_choice_detail(flag, target="lib1"):
    return (
        f"{target}: {flag} (cxx compiler apple-clang is the user's choice: the user states "
        'cc_werror; or add [author_decides] cc_werror = "reason" to buildlog.toml)'
    )


def with_rules(proj, rules):
    (proj / "rules.dl").write_text(rules)
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "rules.dl"]')
    )


@pytest.mark.parametrize(
    "key, flag", [("cc.compile_flag", "-Werror"), ("cxx.compile_flag", "-Werror=format")]
)
def test_an_author_werror_flag_is_an_error_unless_the_author_decides(tmp_path, key, flag):
    spec = LAYERED.replace(
        '["lib2", "cc.include_dir", "src:l2", "public"],',
        f'["lib2", "cc.include_dir", "src:l2", "public"],\n  ["lib1", "{key}", "{flag}", "private"],',
    )
    proj, user = project(tmp_path, spec, HEADERS)
    errors = configure_errors(proj, user, tmp_path)
    assert errors == [[KIND, user_choice_detail(flag)]]
    (proj / "buildlog.toml").write_text(
        (proj / "buildlog.toml").read_text()
        + '[author_decides]\ncc_werror = "CI image: one pinned compiler"\n'
    )
    build, metrics = configure(proj, user, tmp_path, "build2")
    assert metrics["errors"] == []
    assert flag in compile_cmd(build, f"obj/{REL}/lib1/l1.cpp.o").split()


def test_a_rules_werror_on_a_user_chosen_compiler_is_an_error(tmp_path):
    # Not only properties: any argv word, here one a project rule writes.
    proj, user = project(tmp_path, LAYERED, HEADERS)
    with_rules(
        proj, 'action_arg(o, 60, "", "-Werror") :- target_action("lib1", _, o), action(o, "CXX").\n'
    )
    assert configure_errors(proj, user, tmp_path) == [[KIND, user_choice_detail("-Werror")]]


def test_a_werror_is_integrity_where_the_author_pins_the_toolchain(tmp_path, capsys):
    # The author pins the toolchain by identity ([author_decides]
    # toolchain_pin): the compiler is the author's, so -Werror is allowed,
    # and why shows the pin.
    spec = PINS.format(tc="apple-clang") + LAYERED.replace(
        '["lib2", "cc.include_dir", "src:l2", "public"],',
        '["lib2", "cc.include_dir", "src:l2", "public"],\n  ["lib1", "cc.compile_flag", "-Werror", "private"],',
    )
    reason = "firmware: one compiler, by identity"
    proj, user = project(
        tmp_path, spec, HEADERS, buildlog_extra=f'[author_decides]\ntoolchain_pin = "{reason}"\n'
    )
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    obj = f"obj/{REL}/lib1/l1.cpp.o"
    assert "-Werror" in compile_cmd(build, obj).split()
    text = why(build, proj, "-Werror", obj, capsys)
    assert f"toolchain apple-clang for cxx pinned by the author: {reason}" in text
    assert 'toolchain_pin("cxx", "arm64-macos", "arm64-macos", "apple-clang")' in text
    assert (
        f'author_decision("toolchain_pin", "cxx running on arm64-macos for arm64-macos", "{reason}")'
        in text
    )


TOOL_RULES = """\
.decl gen_out(c:Cfg, o:symbol)
gen_out(c, cat("out:gen/", n, "/w.txt")) :- needed("app", c), cfg_name(c, n).
action(o, "GEN"), target_action("app", c, o), action_stdout(o, o), default_output(o) :- gen_out(c, o).
action_run(o, 0, "", "tool", "run") :- gen_out(_, o).
action_arg(o, 1, "", "-Werror") :- gen_out(_, o).
"""


def test_a_werror_is_integrity_on_a_tool_named_by_reference(tmp_path, capsys):
    # The program is a target of the author's graph (action_run), as Halide's
    # runtime compiler is the Clang of the LLVM libHalide links: allowed, and
    # why shows the reference.
    spec = (
        LAYERED.replace('["lib2"]]', '["lib2"], ["tool"]]', 1)
        .replace('["lib2", "library"]]', '["lib2", "library"], ["tool", "executable"]]')
        .replace('["lib2", "l2.cpp"]]', '["lib2", "l2.cpp"], ["tool", "tool.cpp"]]')
    )
    proj, user = project(tmp_path, spec, {**HEADERS, "tool.cpp": "int main() { return 0; }\n"})
    with_rules(proj, TOOL_RULES)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    out = f"gen/{REL}/w.txt"
    assert compile_cmd(build, out).split()[1] == "-Werror"
    text = why(build, proj, "-Werror", out, capsys)
    assert "action_arg_basis(" in text and "runs tool, named by reference" in text
    assert 'tool_resolves("app", "tool", "tool")' in text and "(via action_run)" in text


LITERAL_RULES = """\
.decl lit_out(c:Cfg, o:symbol)
lit_out(c, cat("out:gen/", n, "/lit.txt")) :- needed("app", c), cfg_name(c, n).
action(o, "GEN"), target_action("app", c, o), action_stdout(o, o), default_output(o) :- lit_out(c, o).
action_arg(o, 0, "", "/bin/echo") :- lit_out(_, o).
action_arg(o, 1, "", "-Werror") :- lit_out(_, o).
"""


def test_a_werror_is_integrity_on_a_literal_program_path(tmp_path, capsys):
    # An author's literal path is the author's program: allowed, and why
    # cites the row that writes it.
    proj, user = project(tmp_path, LAYERED, HEADERS)
    with_rules(proj, LITERAL_RULES)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    out = f"gen/{REL}/lit.txt"
    assert compile_cmd(build, out).split()[:2] == ["/bin/echo", "-Werror"]
    text = why(build, proj, "-Werror", out, capsys)
    assert "action_arg_basis(" in text
    assert "program /bin/echo written by the author (a literal path)" in text
    # The subproof reaches the author's rule that writes argv[0].
    assert "lit_out(" in text and "(via action_arg)" in text


@host.cross
def test_a_werror_on_the_lipo_merge_is_the_users_platform_choice(tmp_path):
    cross = Path(__file__).resolve().parents[1] / "examples" / "cross"
    proj = tmp_path / "cross"
    shutil.copytree(cross, proj)
    (proj / "rules.dl").write_text('action_arg(f, 60, "", "-Werror") :- action(f, "LIPO").\n')
    toml = proj / "buildlog.toml"
    toml.write_text(toml.read_text().replace('"spec.toml"]', '"spec.toml", "rules.dl"]'))
    user = tmp_path / "user.toml"
    user.write_text((cross / "presets" / "apple-clang.toml").read_text())
    (errors,) = [
        e
        for e in configure_errors(proj, user, tmp_path)
        if e[0] == KIND and "platform tool lipo" in e[1]
    ][:1]
    assert "-Werror" in errors[1] and "is the user's choice" in errors[1]


def test_the_author_decides_bypass_shows_in_why(tmp_path, capsys):
    proj, user = project(
        tmp_path,
        LAYERED,
        HEADERS,
        buildlog_extra='[author_decides]\ncc_werror = "CI image: one pinned compiler"\n',
    )
    with_rules(
        proj, 'action_arg(o, 60, "", "-Werror") :- target_action("lib1", _, o), action(o, "CXX").\n'
    )
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    text = why(build, proj, "-Werror", f"obj/{REL}/lib1/l1.cpp.o", capsys)
    assert "[author_decides] cc_werror: CI image: one pinned compiler" in text


def test_imported_dirs_move_to_the_system_class_with_package_system_includes(tmp_path):
    """Imported include dirs are -isystem either way; package_system_includes
    only moves them after the in-project dependencies' dirs."""
    greet = (
        Path(__file__).resolve().parent
        / "fixtures/importers/prefix/lib/cmake/Greet/Greet-targets.cmake"
    )
    inc = os.path.realpath(greet.parents[3] / "include")
    spec = LAYERED.replace(
        'dep = [["app", "lib1", "private"], ["lib1", "lib2", "public"]]',
        'dep = [["app", "lib1", "private"], ["lib1", "lib2", "public"], ["app", "Greet::shout", "private"]]',
    )
    package = f'package_file = [["cmake", {json.dumps(str(greet))}]]\n'
    proj, user = project(tmp_path, spec, HEADERS, user_extra=package)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    cmd = compile_cmd(build, f"obj/{REL}/app/main.cpp.o")
    dirs = [d for f, d in includes(cmd) if f == "-isystem"]
    assert f"-I{inc}" not in cmd
    # The dependency class (lexical: the absolute path sorts before src: dirs).
    assert dirs == [inc, "l1", "l2", "aaa_sys"]
    user.write_text(user.read_text() + 'package_system_includes = [["cmake"]]\n')
    build, metrics = configure(proj, user, tmp_path, "build2")
    assert metrics["errors"] == []
    cmd = compile_cmd(build, f"obj/{REL}/app/main.cpp.o")
    dirs = [d for f, d in includes(cmd) if f == "-isystem"]
    assert dirs == ["l1", "l2", inc, "aaa_sys"]


# An imported tool: Greet::greet-tool, bound by the user's package
# selection, against the library Greet::whisper the app links. Two copies of
# one install prefix stand for two installations of the package.
FIXTURE_PREFIX = Path(__file__).resolve().parent / "fixtures/importers/prefix"
IMPORTED_TOOL_RULES = TOOL_RULES.replace('"tool"', '"Greet::greet-tool"')
SAME = 'import_same_package("Greet::greet-tool", "Greet::whisper").\n'


def prefixes(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        for sub in ("bin", "lib/cmake"):
            shutil.copytree(FIXTURE_PREFIX / sub, p / sub, symlinks=True)
        for lib in (FIXTURE_PREFIX / "lib").glob("*.a"):
            shutil.copy(lib, p / "lib" / lib.name)
        out.append(os.path.realpath(p))
    return out


def imported_tool_project(tmp_path, user_extra, rules):
    spec = LAYERED.replace(
        'dep = [["app", "lib1", "private"], ["lib1", "lib2", "public"]]',
        'dep = [["app", "lib1", "private"], ["lib1", "lib2", "public"], ["app", "Greet::whisper", "private"]]',
    )
    proj, user = project(tmp_path, spec, HEADERS, user_extra=user_extra)
    with_rules(proj, IMPORTED_TOOL_RULES + rules)
    return proj, user


def test_an_imported_tool_is_the_users_choice(tmp_path):
    (pa,) = prefixes(tmp_path, "pa")
    proj, user = imported_tool_project(tmp_path, f"package_prefix = [[{json.dumps(pa)}]]\n", "")
    assert configure_errors(proj, user, tmp_path) == [
        [
            KIND,
            (
                f"app: -Werror (imported tool Greet::greet-tool (from cmake prefix {pa}) is the user's "
                "choice: the user states cc_werror; or the author states import_same_package("
                '"Greet::greet-tool", <the library it must match>); or add [author_decides] '
                'cc_werror = "reason" to buildlog.toml)'
            ),
        ]
    ]


def test_an_imported_tool_tied_to_the_linked_library_is_the_authors(tmp_path, capsys):
    (pa,) = prefixes(tmp_path, "pa")
    proj, user = imported_tool_project(tmp_path, f"package_prefix = [[{json.dumps(pa)}]]\n", SAME)
    build, metrics = configure(proj, user, tmp_path)
    assert metrics["errors"] == []
    out = f"gen/{REL}/w.txt"
    assert compile_cmd(build, out).split()[1] == "-Werror"
    text = why(build, proj, "-Werror", out, capsys)
    basis = (
        "runs Greet::greet-tool, from the same package as Greet::whisper "
        f"(import_same_package: cmake prefix {pa})"
    )
    assert f'action_arg_basis("out:{out}", 1, "-Werror", "{basis}")' in text
    assert 'import_same_package("Greet::greet-tool", "Greet::whisper")' in text
    assert f'package_prefix("{pa}")' in text
    assert "av_pkg_offer(" in text


def test_import_same_package_is_checked_against_the_users_selection(tmp_path):
    pa, pb = prefixes(tmp_path, "pa", "pb")
    tool = f"{pa}/lib/cmake/Greet/Greet-targets.cmake"
    lib = f"{pb}/lib/cmake/GreetWhisper/GreetWhisper-targets.cmake"
    user_extra = (
        f"package_prefix = [[{json.dumps(pa)}], [{json.dumps(pb)}]]\n"
        f'package_pin = [["Greet::greet-tool", {json.dumps(tool)}], '
        f'["Greet::whisper", {json.dumps(lib)}]]\n'
    )
    proj, user = imported_tool_project(tmp_path, user_extra, SAME)
    errors = configure_errors(proj, user, tmp_path)
    assert [
        "imported targets come from different packages",
        (
            f"Greet::greet-tool on arm64-macos is from cmake prefix {pa}, Greet::whisper on "
            f"arm64-macos from cmake prefix {pb} (import_same_package)"
        ),
    ] in errors
    # Not consistent, so the tool is the user's, and the -Werror an error too.
    assert [KIND, "app"] in [[k, d.split(":")[0]] for k, d in errors]


def test_import_same_package_needs_the_library_bound(tmp_path):
    (pa,) = prefixes(tmp_path, "pa")
    proj, user = imported_tool_project(
        tmp_path,
        f"package_prefix = [[{json.dumps(pa)}]]\n",
        'import_same_package("Greet::greet-tool", "Greet::nothing").\n',
    )
    errors = configure_errors(proj, user, tmp_path)
    assert [
        "import_same_package names a target not bound",
        "Greet::nothing on arm64-macos (import_same_package with Greet::greet-tool, a tool of app)",
    ] in errors
