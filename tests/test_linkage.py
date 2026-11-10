# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Linkage as a derived choice per (library, configuration): kind "library", its precedence levels (the author's
requirement, a dependent's pin, the user's per-library choice, the
configuration's native.linkage, the author's default, static), constraints
that contradict the user being errors, the mandatory reason on a constraint,
linkage-conditioned properties, PIC following linkage, the user's cc_pic, and
the other errors."""

from __future__ import annotations

import subprocess

import pytest

from buildlog.cli import main
from buildlog.errors import BuildlogError
from tests import host
from tests.test_cpp_mechanisms import (
    cmd_of,
    configure_project,
    needs_souffle,
    ninja_text,
    project,
    run_ninja,
)

REL = "arm64-macos.release.none"

FILES = {
    "foo.c": (
        "#ifdef FOO_STATIC\nint foo(void) { return 1; }\n#else\nint foo(void) { return 2; }\n#endif\n"
    ),
    "main.c": (
        "#include <stdio.h>\nint foo(void);\n"
        "#ifdef FOO_STATIC\n#define SEEN 1\n#else\n#define SEEN 2\n#endif\n"
        'int main(void) { printf("%d %d\\n", foo(), SEEN); return 0; }\n'
    ),
}

SPEC = """
target = [["foo"], ["app"]]
native_kind = [["foo", "library"], ["app", "executable"]]
c_source = [["foo", "foo.c"], ["app", "main.c"]]
dep = [["app", "foo", "private"]]
linkage_prop = [["foo", "static", "cc.define", "FOO_STATIC", "public"]]
request = [["app", "rel"]]
"""

# SPEC with its targets as tables, for a [target.foo] table to follow.
NO_FOO = (
    SPEC.replace('target = [["foo"], ["app"]]\n', "").replace('["foo", "library"], ', "")
    + "[target.app]\n"
)


def configure(proj, tmp_path, user="", name="build"):
    users = []
    if user:
        (proj / "user.toml").write_text(user)
        users = [proj / "user.toml"]
    build = tmp_path / name
    _, metrics = configure_project(proj, build, user_files=users, update_lock=True)
    return build, metrics


def set_configs(proj, decls, settings):
    spec = proj / "spec.toml"
    text = spec.read_text()
    text = text.replace('config_decl = [["rel", "arm64-macos"]]', f"config_decl = {decls}")
    text = text.replace(
        'config_decl_setting = [["rel", "native.opt", "release"]]',
        f"config_decl_setting = {settings}",
    )
    spec.write_text(text)


def rel_levels(build, cfg=REL):
    rows = (build / "souffle" / "facts" / "library_linkage_by.csv").read_text().splitlines()
    names = dict(
        r.split("\t")
        for r in (build / "souffle" / "facts" / "cfg_name.csv").read_text().splitlines()
    )
    return {(t, l, lv) for t, c, l, lv in (r.split("\t") for r in rows) if names.get(c) == cfg}


@needs_souffle
def test_a_library_with_no_stated_linkage_is_static(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert rel_levels(build) == {("foo", "static", "static")}
    text = ninja_text(build)
    assert f"out/lib/{REL}/libfoo.a" in text or f"lib/{REL}/libfoo.a" in text
    assert "-fPIC" not in cmd_of(build, "foo.c.o")
    assert "-DFOO_STATIC" in cmd_of(build, "main.c.o")
    run_ninja(build)
    exe = build / "souffle" / "bin" / REL / "app"
    assert subprocess.check_output([str(exe)], text=True) == "1 1\n"


@needs_souffle
def test_the_author_default_is_shared_and_pic_follows(tmp_path):
    proj = project(tmp_path, SPEC + 'default_linkage = [["foo", "shared"]]\n', FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert rel_levels(build) == {("foo", "shared", "author default")}
    assert "-fPIC" in cmd_of(build, "foo.c.o")
    assert "-DFOO_STATIC" not in cmd_of(build, "main.c.o")
    assert host.shlib("foo") in ninja_text(build)
    run_ninja(build)
    exe = build / "souffle" / "bin" / REL / "app"
    assert subprocess.check_output([str(exe)], text=True) == "2 2\n"


@needs_souffle
def test_both_flavours_are_two_configurations(tmp_path):
    """A packager's static and shared builds: native.linkage per
    configuration, one build, two configuration directories. The
    configuration named for the default keeps its name."""
    spec = SPEC.replace(
        'request = [["app", "rel"]]', 'request = [["app", "rel"], ["app", "rel-static"]]'
    )
    proj = project(tmp_path, spec + 'default_linkage = [["foo", "shared"]]\n', FILES)
    set_configs(
        proj,
        '[["rel", "arm64-macos"], ["rel-static", "arm64-macos"]]',
        '[["rel", "native.opt", "release"], ["rel-static", "native.opt", "release"],'
        ' ["rel-static", "native.linkage", "static"]]',
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert rel_levels(build) == {("foo", "shared", "author default")}
    assert rel_levels(build, f"{REL}.static") == {("foo", "static", "user global")}
    text = ninja_text(build)
    assert f"lib/{REL}/{host.shlib('foo')}" in text
    assert f"lib/{REL}.static/libfoo.a" in text
    run_ninja(build)
    out = {
        cfg: subprocess.check_output([str(build / "souffle" / "bin" / cfg / "app")], text=True)
        for cfg in (REL, f"{REL}.static")
    }
    assert out == {REL: "2 2\n", f"{REL}.static": "1 1\n"}


@needs_souffle
def test_precedence_constraints_over_user_library_over_user_global_over_author(tmp_path, capsys):
    proj = project(tmp_path, SPEC + 'default_linkage = [["foo", "static"]]\n', FILES)
    # The configuration's native.linkage (user global) beats the author.
    set_configs(
        proj,
        '[["rel", "arm64-macos"]]',
        '[["rel", "native.opt", "release"], ["rel", "native.linkage", "shared"]]',
    )
    build, metrics = configure(proj, tmp_path, name="b1")
    assert metrics["errors"] == []
    assert rel_levels(build, f"{REL}.shared") == {("foo", "shared", "user global")}
    # The user's per-library choice beats the configuration's.
    build, metrics = configure(proj, tmp_path, 'library_linkage = [["foo", "static"]]\n', name="b2")
    assert metrics["errors"] == []
    assert rel_levels(build, f"{REL}.shared") == {("foo", "static", "user per-library")}
    # A dependent's pin the user agrees with chooses, and why gives its reason.
    spec = proj / "spec.toml"
    spec.write_text(
        spec.read_text()
        + 'dep_linkage = [["app", "foo", "shared", "loads plugins that use foo"]]\n'
    )
    build, metrics = configure(proj, tmp_path, 'library_linkage = [["foo", "shared"]]\n', name="b3")
    assert metrics["errors"] == []
    assert rel_levels(build, f"{REL}.shared") == {("foo", "shared", "downstream pin")}
    capsys.readouterr()
    cfg = (
        '["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"],'
        ' [["native.linkage", "shared"], nil]]]]'
    )
    fact = f'library_linkage_in("foo", {cfg}, "shared")'
    assert main(["why", "-C", str(build), fact, str(proj)]) == 0
    out = capsys.readouterr().out
    assert "downstream pin" in out
    assert 'dep_linkage("app", "foo", "shared", "loads plugins that use foo")' in out


@needs_souffle
def test_an_author_requirement_chooses_and_why_gives_its_reason(tmp_path, capsys):
    """linkage = { require, why } on [target.X] desugars to linkage_require,
    which chooses over every user level and whose reason why shows."""
    text = NO_FOO + (
        '[target.foo]\nkind = "library"\n'
        'linkage = { require = "static", why = "uses app internals across the boundary" }\n'
    )
    proj = project(tmp_path, text, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert rel_levels(build) == {("foo", "static", "author requirement")}
    # The user accepting it states the same linkage: no error, still the author's.
    build, metrics = configure(proj, tmp_path, 'library_linkage = [["foo", "static"]]\n', name="b2")
    assert metrics["errors"] == []
    capsys.readouterr()
    assert (
        main(["why", "-C", str(build), "--leaves", "--output", f"lib/{REL}/libfoo.a", str(proj)])
        == 0
    )
    out = capsys.readouterr().out
    assert "uses app internals across the boundary" in out


@needs_souffle
def test_a_user_choice_a_library_cannot_provide_is_an_error(tmp_path, capsys):
    """(a): the user's per-library or global linkage against the author's
    requirement, or against a fixed kind, is an error naming both and the
    fix; never a silent fallback."""
    extra = """
target = [["foo"], ["app"], ["fixed"]]
native_kind = [["foo", "library"], ["app", "executable"], ["fixed", "module_library"]]
c_source = [["foo", "foo.c"], ["app", "main.c"], ["fixed", "foo.c"]]
dep = [["app", "foo", "private"], ["app", "fixed", "private"]]
linkage_require = [["foo", "static", "uses app internals"]]
request = [["app", "rel"]]
"""
    proj = project(tmp_path, extra, FILES)
    errs = configure_errors(
        proj, tmp_path, capsys, 'library_linkage = [["foo", "shared"], ["fixed", "shared"]]\n'
    )
    assert (
        "user linkage contradicts the library's requirement: foo: the user's library_linkage asks "
        "for shared, but its author requires static (uses app internals); fix: drop "
        "library_linkage(foo, shared) or state static"
    ) in errs
    assert (
        "library_linkage of a target whose kind is not library: fixed: the user's library_linkage "
        "asks for shared, but it is a module_library; fix: drop "
        "library_linkage(fixed, shared)"
    ) in errs
    set_configs(
        proj,
        '[["rel", "arm64-macos"]]',
        '[["rel", "native.opt", "release"], ["rel", "native.linkage", "shared"]]',
    )
    errs = configure_errors(proj, tmp_path, capsys)
    assert (
        f"user linkage contradicts the library's requirement: foo in {REL}.shared: the user's "
        "native.linkage asks for shared, but its author requires static (uses app internals); "
        "fix: state library_linkage(foo, static) to accept it, or drop native.linkage"
    ) in errs
    # Accepting it per library is the fix.
    build, metrics = configure(proj, tmp_path, 'library_linkage = [["foo", "static"]]\n', name="ok")
    assert metrics["errors"] == []
    assert rel_levels(build, f"{REL}.shared") == {("foo", "static", "author requirement")}


@needs_souffle
def test_a_pin_against_the_users_choice_is_an_error(tmp_path, capsys):
    """(b): a dependent's pin never silently overrides the user's
    per-library choice, nor the configuration's."""
    spec = SPEC + 'dep_linkage = [["app", "foo", "shared", "loads plugins that use foo"]]\n'
    proj = project(tmp_path, spec, FILES)
    errs = configure_errors(proj, tmp_path, capsys, 'library_linkage = [["foo", "static"]]\n')
    assert (
        "linkage pin contradicts the user's choice: app -> foo pins shared (loads plugins that use "
        "foo), but the user's library_linkage asks for static; fix: drop "
        "library_linkage(foo, static) or the pin"
    ) in errs
    set_configs(
        proj,
        '[["rel", "arm64-macos"]]',
        '[["rel", "native.opt", "release"], ["rel", "native.linkage", "static"]]',
    )
    errs = configure_errors(proj, tmp_path, capsys)
    assert (
        f"linkage pin contradicts the user's choice: app -> foo pins shared (loads plugins that "
        f"use foo), but in {REL}.static the user's native.linkage asks for static"
    ) in errs


def test_a_linkage_constraint_without_a_reason_is_a_load_error(tmp_path):
    def load_error(sub, text, base=NO_FOO):
        proj = project(tmp_path / sub, base + text, FILES)
        with pytest.raises(BuildlogError) as exc:
            configure_project(proj, tmp_path / sub / "build", emit=False, update_lock=True)
        return str(exc.value)

    for sub, text in (
        ("bare", '[target.foo]\nkind = "library"\nlinkage = { require = "static" }\n'),
        ("blank", '[target.foo]\nkind = "library"\nlinkage = { require = "static", why = " " }\n'),
    ):
        assert "constrains linkage without a reason" in load_error(sub, text)
    err = load_error("pin", 'dep_linkage = [["app", "foo", "shared"]]\n', SPEC)
    assert "dep_linkage('app', 'foo', 'shared') constrains linkage without a reason" in err
    err = load_error("string", '[target.foo]\nkind = "library"\nlinkage = "static"\n')
    assert 'linkage must be { require = "static" | "shared" | "both", why = "..." }' in err


@needs_souffle
def test_a_user_can_make_a_static_librarys_objects_pic(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path, 'cc_pic = [["foo"]]\n')
    assert metrics["errors"] == []
    assert "-fPIC" in cmd_of(build, "foo.c.o")
    assert "-fPIC" not in cmd_of(build, "main.c.o")


def configure_errors(proj, tmp_path, capsys, user=""):
    (proj / "user.toml").write_text(user)
    with pytest.raises(SystemExit):
        configure_project(
            proj, tmp_path / "build", user_files=[proj / "user.toml"], emit=False, update_lock=True
        )
    return capsys.readouterr().err


@needs_souffle
def test_linkage_errors(tmp_path, capsys):
    extra = """
target = [["foo"], ["app"], ["app2"], ["fixed"]]
native_kind = [["foo", "library"], ["app", "executable"], ["app2", "executable"], ["fixed", "module_library"]]
c_source = [["foo", "foo.c"], ["app", "main.c"], ["app2", "main.c"], ["fixed", "foo.c"]]
dep = [["app", "foo", "private"], ["app2", "foo", "private"], ["app2", "fixed", "private"]]
dep_linkage = [["app", "foo", "static", "r1"], ["app2", "foo", "shared", "r2"],
  ["app2", "fixed", "shared", "r3"], ["app", "fixed", "static", "r4"], ["app", "foo", "dynamic", "r5"]]
default_linkage = [["fixed", "shared"]]
request = [["app", "rel"], ["app2", "rel"]]
"""
    proj = project(tmp_path, extra, FILES)
    errs = configure_errors(proj, tmp_path, capsys, 'library_linkage = [["foo", "either"]]\n')
    assert "conflicting linkage pins: foo: shared (app2: r2) and static (app: r1)" in errs
    assert (
        "linkage pin contradicts the library's kind: app2 -> fixed: shared, but fixed is a module_library"
        in errs
    )
    assert "linkage pinned on an edge that does not exist: app -> fixed" in errs
    assert "unknown linkage: app -> foo: dynamic (static or shared)" in errs
    assert "unknown linkage: foo: either (static, shared or both)" in errs
    assert "default_linkage of a target whose kind is not library: fixed" in errs
