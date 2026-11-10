# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One library kind; both linkages in one configuration (one library kind). A built library is
always kind "library"; the old fixed kinds are a load error naming the
replacement. A configuration may build a library static and shared: its
static instance keeps the target's id, its shared instance is <t>@shared,
and an edge to it resolves by the dependent's pin, else the dependent's own
linkage, else it is an error."""

from __future__ import annotations

import subprocess

import pytest

from buildlog.errors import BuildlogError
from tests import host
from tests.test_cpp_mechanisms import cmd_of, needs_souffle, ninja_text, project, run_ninja
from tests.test_linkage import FILES, REL, configure, configure_errors, rel_levels

BOTH = """
target = [["foo"], ["app_s"], ["app_d"], ["mid"], ["dyn"]]
native_kind = [["foo", "library"], ["app_s", "executable"], ["app_d", "executable"],
  ["mid", "library"], ["dyn", "library"]]
c_source = [["foo", "foo.c"], ["app_s", "main.c"], ["app_d", "main.c"], ["mid", "mid.c"], ["dyn", "mid.c"]]
dep = [["app_s", "foo", "private"], ["app_d", "foo", "private"], ["mid", "foo", "public"],
  ["dyn", "foo", "public"]]
dep_linkage = [["app_s", "foo", "static", "a self-contained program"],
  ["app_d", "foo", "shared", "shares foo with its plugins"]]
default_linkage = [["foo", "both"], ["dyn", "shared"]]
linkage_prop = [["foo", "static", "cc.define", "FOO_STATIC", "public"]]
request = [["app_s", "rel"], ["app_d", "rel"], ["mid", "rel"], ["dyn", "rel"]]
"""

FILES_BOTH = {**FILES, "mid.c": "int foo(void);\nint mid(void) { return foo(); }\n"}


def query(build, pattern):
    from buildlog.provenance.query import run_query
    from buildlog.provenance.souffle import load_model

    return {tuple(r) for r in run_query(load_model(build), pattern).rows}


def eff_deps(build):
    return {
        (t, d)
        for t in ("app_s", "app_d", "mid", "dyn")
        for (d,) in query(build, f'eff_dep("{t}", _, d, _)')
    }


@pytest.mark.parametrize(
    ("spec", "kind", "linkage"),
    [
        ('[target.foo]\nkind = "static_library"\n', "static_library", "static"),
        ('native_kind = [["foo", "shared_library"]]\n', "shared_library", "shared"),
    ],
)
def test_a_fixed_library_kind_is_a_load_error_naming_the_replacement(tmp_path, spec, kind, linkage):
    proj = project(tmp_path, ("" if spec.startswith("[") else 'target = [["foo"]]\n') + spec)
    with pytest.raises(BuildlogError) as exc:
        configure(proj, tmp_path)
    msg = str(exc.value)
    assert f"foo: kind '{kind}' was removed; write kind = \"library\"" in msg
    assert f'linkage = {{ require = "{linkage}", why = "..." }} only if foo cannot' in msg


@needs_souffle
def test_a_rule_deriving_a_fixed_kind_is_an_error(tmp_path, capsys):
    proj = project(tmp_path, 'target = [["foo"]]\nc_source = [["foo", "foo.c"]]\n', FILES)
    (proj / "rules.dl").write_text('native_kind("foo", "static_library").\n')
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "rules.dl"]')
    )
    errs = configure_errors(proj, tmp_path, capsys)
    assert (
        'fixed library kind: foo: kind "static_library" was removed; write kind = "library"' in errs
    )


@needs_souffle
def test_one_library_built_both_ways_in_one_configuration(tmp_path):
    proj = project(tmp_path, BOTH, FILES_BOTH)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert rel_levels(build) >= {
        ("foo", "static", "author default"),
        ("foo", "shared", "author default"),
    }
    # The static instance is foo, the shared one foo@shared; each artifact
    # has the library's name, and only the shared one is PIC.
    assert query(build, 'linkage_twin(i, "foo")') == {("foo@shared",)}
    text = ninja_text(build)
    assert f"lib/{REL}/libfoo.a" in text and f"lib/{REL}/{host.shlib('foo')}" in text
    assert "-fPIC" not in cmd_of(build, "/foo/foo.c.o")
    assert "-fPIC" in cmd_of(build, "/foo@shared/foo.c.o")
    # Pins choose the instance; a library follows its own linkage (mid is
    # static, dyn shared); FOO_STATIC reaches only the static instance's users.
    assert eff_deps(build) >= {
        ("app_s", "foo"),
        ("app_d", "foo@shared"),
        ("mid", "foo"),
        ("dyn", "foo@shared"),
    }
    assert ("app_s", "foo@shared") not in eff_deps(build)
    assert "-DFOO_STATIC" in cmd_of(build, "/app_s/main.c.o")
    assert "-DFOO_STATIC" not in cmd_of(build, "/app_d/main.c.o")
    run_ninja(build)
    out = {
        a: subprocess.check_output([str(build / "souffle" / "bin" / REL / a)], text=True)
        for a in ("app_s", "app_d")
    }
    assert out == {"app_s": "1 1\n", "app_d": "2 2\n"}


@needs_souffle
def test_a_user_choice_of_one_linkage_builds_one_instance(tmp_path):
    proj = project(
        tmp_path,
        BOTH.replace(
            '["app_d", "foo", "shared", "shares foo with its plugins"]',
            '["app_d", "foo", "static", "r"]',
        ).replace(', ["dyn", "shared"]', ""),
        FILES_BOTH,
    )
    build, metrics = configure(proj, tmp_path, 'library_linkage = [["foo", "static"]]\n')
    assert metrics["errors"] == []
    assert {(t, ln) for t, ln, _ in rel_levels(build) if t == "foo"} == {("foo", "static")}
    assert "foo@shared" not in ninja_text(build)


@needs_souffle
def test_an_undetermined_linkage_edge_is_an_error(tmp_path, capsys):
    spec = BOTH.replace(
        'dep_linkage = [["app_s", "foo", "static", "a self-contained program"],\n'
        '  ["app_d", "foo", "shared", "shares foo with its plugins"]]\n',
        'dep_linkage = [["app_s", "foo", "static", "a self-contained program"]]\n',
    )
    proj = project(tmp_path, spec, FILES_BOTH)
    errs = configure_errors(proj, tmp_path, capsys)
    assert "ambiguous linkage" in errs and "app_d" in errs and "foo" in errs
    assert "dep_linkage" in errs


@needs_souffle
def test_a_linkage_output_name_names_one_instance(tmp_path):
    proj = project(
        tmp_path, BOTH + 'linkage_output_name = [["foo", "shared", "foo-shared"]]\n', FILES_BOTH
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    text = ninja_text(build)
    assert f"lib/{REL}/libfoo.a" in text
    assert f"lib/{REL}/{host.shlib('foo-shared')}" in text
    assert host.shlib("foo") not in text


@needs_souffle
def test_a_component_of_a_library_built_both_ways_names_its_linkage(tmp_path, capsys):
    pkg = 'export_package = [["pkg"]]\nexport_component = [["pkg", "foo", "foo"]]\n'
    proj = project(tmp_path, BOTH + pkg, FILES_BOTH)
    errs = configure_errors(proj, tmp_path, capsys)
    assert "component of a library built both ways needs a linkage" in errs
    assert 'export_component_linkage = [["pkg", "foo", "static" or "shared"]]' in errs

    pkg = (
        'export_package = [["pkg"]]\n'
        'export_component = [["pkg", "foo", "foo"], ["pkg", "foo-shared", "foo"]]\n'
        'export_component_linkage = [["pkg", "foo", "static"], ["pkg", "foo-shared", "shared"]]\n'
    )
    proj = project(tmp_path / "ok", BOTH + pkg, FILES_BOTH)
    build, metrics = configure(proj, tmp_path / "ok")
    assert metrics["errors"] == []
    inst = query(build, 'export_inst("pkg", m, i, _)')
    assert inst == {("foo", "foo"), ("foo-shared", "foo@shared")}
