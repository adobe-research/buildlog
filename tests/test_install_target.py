# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""install_target / install_configured: install of built targets that are not
exported components (L3_install.dl, "installing built targets")."""

from __future__ import annotations

import pytest

from tests import host
from tests.test_cpp_mechanisms import configure_project, needs_souffle, project

FILES = {
    "foo.c": "int foo(void) { return 1; }\n",
    "mod.c": "int mod(void) { return 2; }\n",
    "main.c": "int main(void) { return 0; }\n",
    "conf.h.in": "#define V 1\n",
}

SPEC = """
target = [["foo"], ["mod"], ["app"]]
native_kind = [["foo", "library"], ["mod", "module_library"], ["app", "executable"]]
default_linkage = [["foo", "shared"]]
c_source = [["foo", "foo.c"], ["mod", "mod.c"], ["app", "main.c"]]
native_version = [["foo", "1.3.2", "1"]]
request_all = [["foo"], ["mod"], ["app"]]
"""


def run(tmp_path, capsys, spec, prefix=True, second_config=False, name="build"):
    tmp_path.mkdir(exist_ok=True)
    extra = SPEC + spec
    if second_config:
        extra += 'config_decl_extra = [["rel2", "arm64-macos"]]\n'
    proj = project(tmp_path, extra, FILES)
    if second_config:
        spec_file = proj / "spec.toml"
        spec_file.write_text(
            spec_file.read_text()
            .replace(
                'config_decl = [["rel", "arm64-macos"]]',
                'config_decl = [["rel", "arm64-macos"], ["rel2", "arm64-macos"]]',
            )
            .replace(
                'config_decl_setting = [["rel", "native.opt", "release"]]',
                'config_decl_setting = [["rel", "native.opt", "release"], ["rel2", "native.opt", "debug"]]',
            )
            .replace('config_decl_extra = [["rel2", "arm64-macos"]]\n', "")
        )
    users = []
    if prefix:
        (proj / "user.toml").write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
        users = [proj / "user.toml"]
    capsys.readouterr()
    try:
        _, metrics = configure_project(
            proj, tmp_path / name, user_files=users, update_lock=True, emit=False
        )
    except SystemExit:
        metrics = {"errors": []}
    err = capsys.readouterr().err
    return tmp_path / name, metrics, err


def rows(build, rel):
    path = build / "souffle" / "facts" / f"{rel}.csv"
    return [r.split("\t") for r in path.read_text().splitlines()] if path.exists() else []


def installed_names(build):
    return {r[2] for r in rows(build, "installed")} if rows(build, "installed") else set()


@needs_souffle
def test_library_module_and_program_install(tmp_path, capsys):
    spec = 'install_target = [["foo", "libdir"], ["mod", "bindir"], ["app", "bindir"]]\n'
    build, metrics, _err = run(tmp_path, capsys, spec)
    assert metrics["errors"] == []
    names = {r[2] for r in rows(build, "installed")}
    assert {f"lib/{host.shlib('foo', v)}" for v in ("1.3.2", "1", None)} <= names
    assert any(n.startswith("bin/") and "mod" in n for n in names)
    assert "bin/app" in names


@needs_souffle
def test_two_configurations_collide(tmp_path, capsys):
    spec = 'install_target = [["foo", "libdir"]]\n'
    _, metrics, err = run(tmp_path, capsys, spec, second_config=True)
    assert "two built files install to one destination" in err
    _, metrics, err = run(tmp_path / "np", capsys, spec, prefix=False, second_config=True)
    assert metrics["errors"] == []


@pytest.mark.parametrize(
    "spec, message",
    [
        ('install_target = [["nope", "libdir"]]\n', "does not exist"),
        ('install_target = [["foo", "nodir"]]\n', "has no layout"),
        (
            'install_configured = [["foo", "conf.h", "includedir"]]\n',
            "not configured",
        ),
    ],
)
@needs_souffle
def test_errors(tmp_path, capsys, spec, message):
    _, _m, err = run(tmp_path, capsys, spec)
    assert message in err


@needs_souffle
def test_exported_component_is_an_error(tmp_path, capsys):
    spec = (
        'install_target = [["foo", "libdir"]]\n'
        'export_package = [["Foo"]]\nexport_version = [["Foo", "1.3.2"]]\n'
        'export_component = [["Foo", "foo", "foo"]]\n'
    )
    _, _m, err = run(tmp_path, capsys, spec)
    assert "exported component" in err


@needs_souffle
def test_configured_file_installs(tmp_path, capsys):
    spec = (
        'install_configured = [["foo", "conf.h", "includedir"]]\n'
        'configure_file = [["foo", "conf.h"]]\n'
    )
    build, metrics, _err = run(tmp_path, capsys, spec)
    assert metrics["errors"] == []
    assert any(n == "include/conf.h" for n in installed_names(build))
