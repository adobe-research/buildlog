# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exported native components and the derived compatibility claim (a library component installs its
artifact (a shared library with its links) and the export states its type,
location and install name; the package's compatibility defaults to the
libraries' ABI claim from native_version, and a looser stated claim is an
error unless [author_decides] gives a reason."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from buildlog import builtins, export_formats
from tests import host
from tests.test_cpp_mechanisms import configure_project, needs_souffle, project

FILES = {"foo.c": "int foo(void) { return 1; }\n", "foo.h": "int foo(void);\n"}

SPEC = """
target = [["foo"]]
native_kind = [["foo", "library"]]
default_linkage = [["foo", "shared"]]
c_source = [["foo", "foo.c"]]
native_version = [["foo", "1.3.2", "1"]]
prop = [["foo", "cc.include_dir", "src:", "public"]]
request_all = [["foo"]]
export_package = [["Foo"]]
export_version = [["Foo", "1.3.2"]]
export_component = [["Foo", "foo", "foo"]]
install_map = [["foo", "src:", "includedir", ""]]
install_file = [["foo", "src:foo.h"]]
"""


def configure(proj, tmp_path, prefix=None, name="build", emit=True):
    users = []
    if prefix is not None:
        (proj / "user.toml").write_text(f'install_prefix = "{prefix}"\n')
        users = [proj / "user.toml"]
    build = tmp_path / name
    _, metrics = configure_project(proj, build, user_files=users, update_lock=True, emit=emit)
    return build, metrics


def facts(build, rel):
    path = build / "souffle" / "facts" / f"{rel}.csv"
    return [r.split("\t") for r in path.read_text().splitlines()] if path.exists() else []


def configure_errors(proj, tmp_path, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit):
        configure_project(proj, tmp_path / "err", emit=False, update_lock=True)
    return capsys.readouterr().err


@needs_souffle
def test_abi_claims_derived_from_native_version(tmp_path):
    extra = """
target = [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]
native_kind = [["a", "library"], ["b", "library"], ["c", "library"],
  ["d", "library"], ["e", "library"], ["f", "library"]]
default_linkage = [["a", "shared"], ["b", "shared"], ["c", "shared"], ["d", "shared"], ["e", "shared"], ["f", "shared"]]
native_version = [["a", "1.3.2", "1"], ["b", "25.12.19", "25.12.19"], ["c", "2.3.1", "2.3"],
  ["d", "0.9.4", "0"], ["e", "1.0.17", "0"], ["f", "2.1", "2.1"]]
"""
    proj = project(tmp_path, extra, FILES)
    build, metrics = configure(proj, tmp_path, emit=False)
    assert metrics["errors"] == []
    got = dict(facts(build, "native_abi_compat"))
    # e's soversion is libtool's numbering, unrelated to the version: no claim.
    assert got == {
        "a": "same_major",
        "b": "exact",
        "c": "same_minor",
        "d": "same_minor",
        "f": "same_minor",
    }


@needs_souffle
def test_derived_compat_and_the_looser_claim(tmp_path, capsys):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path, emit=False)
    assert metrics["errors"] == []
    assert facts(build, "export_compat_in") == [["Foo", "same_major"]]
    spec = proj / "spec.toml"
    spec.write_text(spec.read_text() + 'export_compat = [["Foo", "any_newer"]]\n')
    err = configure_errors(proj, tmp_path, capsys)
    assert (
        "stated compatibility is looser than the library's ABI: Foo: any_newer but foo's soversion claims same_major"
        in err
    )
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text() + 'export_compat = "upstream says AnyNewerVersion (CMakeLists.txt)"\n'
    )
    build, metrics = configure(proj, tmp_path, name="b2", emit=False)
    assert metrics["errors"] == []
    assert facts(build, "export_compat_in") == [["Foo", "any_newer"]]
    assert facts(build, "export_compat_looser") == [
        ["Foo", "any_newer", "same_major", "foo", "upstream says AnyNewerVersion (CMakeLists.txt)"]
    ]
    # A stricter statement needs no reason.
    spec.write_text(spec.read_text().replace('"any_newer"', '"exact"'))
    toml.write_text(toml.read_text().replace('export_compat = "upstream', '# "'))
    build, metrics = configure(proj, tmp_path, name="b3", emit=False)
    assert metrics["errors"] == []
    assert facts(build, "export_compat_in") == [["Foo", "exact"]]


@needs_souffle
def test_unversioned_shared_library_claims_exact_with_a_warning(tmp_path):
    proj = project(tmp_path, SPEC.replace('native_version = [["foo", "1.3.2", "1"]]\n', ""), FILES)
    build, metrics = configure(proj, tmp_path, emit=False)
    assert metrics["errors"] == []
    assert facts(build, "export_compat_in") == [["Foo", "exact"]]
    assert any("has no version" in k for k, _ in metrics["warnings"])


@needs_souffle
def test_zero_y_soversion_warns(tmp_path):
    proj = project(tmp_path, SPEC.replace('"1.3.2", "1"', '"0.9.4", "0"'), FILES)
    build, metrics = configure(proj, tmp_path, emit=False)
    assert facts(build, "export_compat_in") == [["Foo", "same_minor"]]
    assert any("cannot express breaks between minors" in k for k, _ in metrics["warnings"])


@needs_souffle
@pytest.mark.parametrize("linkage", ["shared", "static"])
def test_a_library_component_installs_and_exports_its_artifact(tmp_path, linkage):
    spec = SPEC.replace(
        'default_linkage = [["foo", "shared"]]', f'default_linkage = [["foo", "{linkage}"]]'
    )
    proj = project(tmp_path, spec, FILES)
    prefix = tmp_path / "prefix"
    build, metrics = configure(proj, tmp_path, prefix=prefix)
    assert metrics["errors"] == []
    out = subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "install"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    lib = prefix / "lib"
    cps = json.loads((lib / "cps" / "Foo.cps").read_text())
    comp = cps["components"]["foo"]
    assert cps["compat_version"] == "1.0.0"
    targets = (lib / "cmake" / "Foo" / "FooTargets.cmake").read_text()
    version = (lib / "cmake" / "Foo" / "FooConfigVersion.cmake").read_text()
    pc = (lib / "pkgconfig" / "Foo.pc").read_text()
    assert 'CMAKE_SIZEOF_VOID_P STREQUAL "8"' in version
    assert "Libs: -L${prefix}/lib -lfoo" in pc
    if linkage == "shared":
        full, so = host.shlib("foo", "1.3.2"), host.shlib("foo", "1")
        assert os.readlink(lib / so) == full
        assert os.readlink(lib / host.shlib("foo")) == so
        assert (lib / full).is_file()
        assert comp == {
            "type": "dylib",
            "location": f"@prefix@/lib/{full}",
            "includes": ["@prefix@/include"],
        }
        assert "add_library(Foo::foo SHARED IMPORTED)" in targets
        assert f'IMPORTED_SONAME "{"@rpath/" if host.MACOS else ""}{so}"' in targets
    else:
        assert (lib / "libfoo.a").is_file() and not list(lib.glob(f"*{host.SHLIB_SUFFIX}*"))
        assert comp["type"] == "archive" and comp["location"] == "@prefix@/lib/libfoo.a"
        assert "add_library(Foo::foo STATIC IMPORTED)" in targets
        assert "IMPORTED_SONAME" not in targets
    assert 'IMPORTED_LOCATION "${_IMPORT_PREFIX}/lib/libfoo.' in targets


@needs_souffle
def test_unsupported_native_components_are_errors(tmp_path, capsys):
    spec = """
target = [["foo"], ["bar"], ["tool"]]
native_kind = [["foo", "library"], ["bar", "library"], ["tool", "executable"]]
c_source = [["foo", "foo.c"], ["bar", "foo.c"], ["tool", "foo.c"]]
dep = [["foo", "bar", "private"]]
request_all = [["foo"], ["tool"]]
export_package = [["Foo"]]
export_component = [["Foo", "foo", "foo"], ["Foo", "tool", "tool"]]
"""
    proj = project(tmp_path, spec, FILES)
    err = configure_errors(proj, tmp_path, capsys)
    assert (
        "exported static library has link-only requirements (not supported yet): Foo:foo -> bar"
        in err
    )
    assert (
        "exported component with a native artifact (not supported yet): Foo:tool (executable)"
        in err
    )


def test_install_builtin_installs_sibling_links_as_links(tmp_path):
    (tmp_path / "libx.1.0.dylib").write_text("x")
    os.symlink("libx.1.0.dylib", tmp_path / "libx.1.dylib")
    prefix = tmp_path / "usr"
    args = ["--prefix", str(prefix)]
    for name in ("libx.1.0.dylib", "libx.1.dylib"):
        args += ["--file", str(tmp_path / name), f"{prefix}/lib/{name}"]
    builtins.install(*args)
    builtins.install(*args)  # idempotent
    assert os.readlink(prefix / "lib" / "libx.1.dylib") == "libx.1.0.dylib"
    assert (prefix / "lib" / "libx.1.0.dylib").read_text() == "x"


def test_writer_native_component_needs_its_artifact():
    base = ["--format", "cps", "-o", "x", "--package", "p", "--install-path", "lib/cps/p.cps"]
    with pytest.raises(export_formats.ExportError, match="needs --location"):
        export_formats.parse([*base, "--component=dylib:p"])
    with pytest.raises(export_formats.ExportError, match="needs --soname"):
        export_formats.parse(
            [*base, "--component=dylib:p", "--location=p=lib/libp.dylib", "--link-name=p=p"]
        )
    with pytest.raises(export_formats.ExportError, match="unknown kind"):
        export_formats.parse([*base, "--component=bogus:p"])
