# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""C++ named modules (docs/spec-reference.md): configure derives the static
structure (interface units, scan/collate/BMI actions, one BMI per
compatibility class); a build-time collator reads P1689 scan results and
writes Ninja dyndep files. Built with Homebrew llvm@22."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import host
from tests.test_include_order import (
    LLVM_BIN,
    configure,
    configure_errors,
    needs_llvm,
    project,
)

# macOS: Homebrew's llvm@22 (Apple clang has no scanner); Linux: the family
# the run selects (GCC 14 or Clang 22, with their matching scanners).
TC = "llvm" if host.MACOS else host.FAMILY
pytestmark = needs_llvm if host.MACOS else []
no_gcc_std = pytest.mark.skipif(
    not host.MACOS,
    reason="import std is tested on macOS only (GCC 14 has no std module; Linux libc++ is untested)",
)

HEAD = """
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
"""


@pytest.fixture(autouse=True)
def _llvm(monkeypatch, tmp_path_factory):
    if host.MACOS:
        monkeypatch.setenv("BUILDLOG_LLVM_BIN", str(LLVM_BIN))
    elif host.FAMILY == "gcc" and shutil.which("g++-14"):
        # The image's default GCC is 13; modules need 14.
        shim = tmp_path_factory.mktemp("gcc14")
        for tool in ("gcc", "g++", "gcc-ar"):
            real = shutil.which(f"{tool}-14") or shutil.which(tool)
            if real:
                (shim / tool).symlink_to(real)
        monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    elif host.FAMILY == "clang" and (compiler := shutil.which("clang++-22")):
        llvm_bin = Path(compiler).resolve().parent
        if (llvm_bin / "clang-scan-deps").is_file():
            monkeypatch.setenv("PATH", f"{llvm_bin}{os.pathsep}{os.environ['PATH']}")


def ninja(build, *args):
    return subprocess.run(
        [shutil.which("ninja") or "ninja", "-C", str(build / "souffle"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def built(tmp_path, spec, files, **kw):
    proj, user = project(tmp_path, HEAD + spec, files, tc=TC, **kw)
    build, _ = configure(proj, user, tmp_path)
    r = ninja(build)
    assert r.returncode == 0, r.stdout + r.stderr
    return build


def run_app(build, name="app"):
    exe = next((build / "souffle" / "bin").glob(f"*/{name}"))
    return subprocess.run([str(exe)], capture_output=True, text=True, check=False)


def collate_errors(tmp_path, spec, files):
    proj, user = project(tmp_path, HEAD + spec, files, tc=TC)
    build, _ = configure(proj, user, tmp_path)
    r = ninja(build)
    assert r.returncode != 0
    text = r.stdout + r.stderr
    return text[text.index("buildlog -E collate: C++ modules:") :]


BASIC_SPEC = """
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"], ["lib", "impl.cpp"]]
cxx_module_interface = [["lib", "m.cppm", "public"], ["lib", "m_part.cppm", "public"], ["lib", "m_priv.cppm", "private"]]
dep = [["app", "lib", "private"]]
prop = [["lib", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""

BASIC = {
    "m.cppm": "export module m;\nexport import :part;\nexport import :priv;\nexport int f();\n",
    "m_part.cppm": "export module m:part;\nexport int g() { return 2; }\n",
    "m_priv.cppm": "export module m:priv;\nexport int hidden() { return 3; }\n",
    "impl.cpp": "module m;\nint f() { return 1 + hidden() - 3; }\n",
    "main.cpp": "import m;\nint main() { return f() + g() == 3 ? 0 : 1; }\n",
}


def test_partitions_and_implementation_unit(tmp_path):
    files = dict(BASIC)
    files["m_priv.cppm"] = "export module m:priv;\nexport int hidden() { return 3; }\n"
    build = built(tmp_path, BASIC_SPEC, files)
    assert run_app(build).returncode == 0
    assert ninja(build).stdout.strip().endswith("no work to do.")


def test_unchanged_scan_keeps_objects(tmp_path):
    files = dict(BASIC)
    files["m_priv.cppm"] = "export module m:priv;\nexport int hidden() { return 3; }\n"
    build = built(tmp_path, BASIC_SPEC, files)
    src = tmp_path / "proj" / "src" / "main.cpp"
    src.write_text(src.read_text() + "// touched\n")
    r = ninja(build, "-v")
    assert r.returncode == 0
    assert "-format=p1689" in r.stdout and "-c ../../proj/src/main.cpp " in r.stdout
    assert (
        "-c ../../proj/src/m.cppm" not in r.stdout and "-c ../../proj/src/impl.cpp" not in r.stdout
    )
    assert "--precompile" not in r.stdout
    assert run_app(build).returncode == 0


def test_cross_target_visibility_error(tmp_path):
    spec = """
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["lib", "m.cppm", "public"], ["lib", "p.cppm", "private"]]
dep = [["app", "lib", "private"]]
prop = [["lib", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
    files = {
        "m.cppm": "export module m;\nexport int f() { return 0; }\n",
        "p.cppm": "export module p;\nexport int h() { return 0; }\n",
        "main.cpp": "import m;\nimport p;\nint main() { return f() + h(); }\n",
    }
    out = collate_errors(tmp_path, spec, files)
    assert "import of a private module interface" in out and "imports p from" in out


def test_import_from_non_dependency_error(tmp_path):
    spec = """
target = [["app"], ["lib"], ["other"]]
native_kind = [["app", "executable"], ["lib", "library"], ["other", "library"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["lib", "m.cppm", "public"], ["other", "o.cppm", "public"]]
dep = [["app", "lib", "private"], ["app", "other", "private"], ["lib", "other", "private"]]
prop = [["lib", "cxx.std", "20", "public"], ["other", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
    # lib is a dependency of app, but `other`'s module is imported from
    # lib's closure: app depends on other too here, so make it not
    spec = spec.replace('["app", "other", "private"], ', "")
    files = {
        "m.cppm": "export module m;\nimport o;\nexport int f() { return h(); }\n",
        "o.cppm": "export module o;\nexport int h() { return 0; }\n",
        "main.cpp": "import m;\nimport o;\nint main() { return f(); }\n",
    }
    out = collate_errors(tmp_path, spec, files)
    assert (
        "import from a target that is not a dependency" in out and "never adds a link edge" in out
    )


def test_import_cycle_error(tmp_path):
    spec = """
target = [["app"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["app", "a.cppm", "public"], ["app", "b.cppm", "public"]]
prop = [["app", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
    files = {
        "a.cppm": "export module a;\nimport b;\nexport int fa();\n",
        "b.cppm": "export module b;\nimport a;\nexport int fb();\n",
        "main.cpp": "int main() { return 0; }\n",
    }
    out = collate_errors(tmp_path, spec, files)
    assert "import cycle" in out


def test_module_name_collision_error(tmp_path):
    spec = """
target = [["app"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["app", "a.cppm", "public"], ["app", "b.cppm", "public"]]
prop = [["app", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
    files = {
        "a.cppm": "export module m;\nexport int fa();\n",
        "b.cppm": "export module m;\nexport int fb();\n",
        "main.cpp": "int main() { return 0; }\n",
    }
    out = collate_errors(tmp_path, spec, files)
    assert "module name collision" in out and "m is provided by" in out


def test_flag_incompatible_consumer_gets_its_own_bmi(tmp_path):
    spec = """
target = [["app"], ["app2"], ["lib"]]
native_kind = [["app", "executable"], ["app2", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"], ["app2", "main2.cpp"]]
cxx_module_interface = [["lib", "m.cppm", "public"]]
dep = [["app", "lib", "private"], ["app2", "lib", "private"]]
prop = [["lib", "cxx.std", "20", "public"], ["app2", "cc.compile_flag", "-fno-rtti", "private"]]
request = [["app", "rel"], ["app2", "rel"]]
"""
    files = {
        "m.cppm": "export module m;\nexport int f() { return 0; }\n",
        "main.cpp": "import m;\nint main() { return f(); }\n",
        "main2.cpp": "import m;\nint main() { return f(); }\n",
    }
    build = built(tmp_path, spec, files)
    assert len(list((build / "souffle" / "bmi").rglob("*.pcm"))) == 2
    assert run_app(build).returncode == 0
    assert run_app(build, "app2").returncode == 0


GEN_SPEC = """
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"]]
modgen_interface = [["lib", "m.cppm.in"]]
dep = [["app", "lib", "private"]]
prop = [["lib", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
GEN_MANIFEST = """\
name = "modgen"
version = "0.1.0"
rank = 5
namespace = "modgen"
layers = ["modgen.dl"]
requires = { buildlog-cpp = ">=0.1" }
extends = ["action", "action_builtin", "action_arg", "action_in", "target_action",
           "cxx_generated_module_interface"]
accepts = ["modgen_interface"]
"""
# modgen_interface(t, in): out:gen/<config>/<t>/<in minus .in> is a copy of
# src:<in>, a module interface unit of t (public).
GEN_DL = """\
.decl modgen_interface(t:symbol, in:symbol)
.decl modgen_out(t:symbol, c:Cfg, o:symbol, i:symbol)
modgen_out(t, c, cat("out:gen/", n, "/", t, "/", substr(i, 0, strlen(i) - 3)), i) :- modgen_interface(t, i), needed(t, c), cfg_name(c, n).
action(o, "GEN"), action_builtin(o, "copy"), target_action(t, c, o), cxx_generated_module_interface(t, c, o, "public") :- modgen_out(t, c, o, _).
action_arg(o, 1, "", cat("src:", i)), action_arg(o, 2, "", o), action_in(o, cat("src:", i), "explicit") :- modgen_out(_, _, o, i).
"""


def test_generated_module_interface(tmp_path):
    files = {
        "main.cpp": "import m;\nint main() { return f(); }\n",
        "m.cppm.in": "export module m;\nexport int f() { return 0; }\n",
    }
    proj, user = project(tmp_path, HEAD + GEN_SPEC, files, tc=TC)
    pkg = proj / "packages" / "modgen"
    pkg.mkdir(parents=True)
    (pkg / "manifest.toml").write_text(GEN_MANIFEST)
    (pkg / "modgen.dl").write_text(GEN_DL)
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('uses = ["buildlog-cpp"]', 'uses = ["buildlog-cpp", "modgen"]')
    )
    build, _ = configure(proj, user, tmp_path)
    r = ninja(build)
    assert r.returncode == 0, r.stdout + r.stderr
    assert run_app(build).returncode == 0


def test_import_direction_flip(tmp_path):
    spec = """
target = [["app"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["app", "a.cppm", "public"], ["app", "b.cppm", "public"]]
prop = [["app", "cxx.std", "20", "public"]]
request = [["app", "rel"]]
"""
    files = {
        "a.cppm": "export module a;\nexport int fa() { return 1; }\n",
        "b.cppm": "export module b;\nimport a;\nexport int fb() { return fa() + 1; }\n",
        "main.cpp": "import b;\nint main() { return fb() == 2 ? 0 : 1; }\n",
    }
    build = built(tmp_path, spec, files)
    assert run_app(build).returncode == 0
    src = tmp_path / "proj" / "src"
    (src / "a.cppm").write_text(
        "export module a;\nimport b;\nexport int fa() { return fb() - 1; }\n"
    )
    (src / "b.cppm").write_text("export module b;\nexport int fb() { return 2; }\n")
    (src / "main.cpp").write_text("import a;\nint main() { return fa() == 1 ? 0 : 1; }\n")
    r = ninja(build)
    assert r.returncode == 0, r.stdout + r.stderr
    assert run_app(build).returncode == 0


@host.macos_only("Apple clang ships no module scanner")
def test_apple_clang_is_a_clean_error(tmp_path):
    proj, user = project(tmp_path, HEAD + BASIC_SPEC, BASIC)
    errors = configure_errors(proj, user, tmp_path)
    assert any("module" in json.dumps(e).lower() for e in errors)


def test_deterministic_collation(tmp_path):
    files = dict(BASIC)
    files["m_priv.cppm"] = "export module m:priv;\nexport int hidden() { return 3; }\n"
    build = built(tmp_path, BASIC_SPEC, files)
    dd = next((build / "souffle" / "modules").rglob("app.dd"))
    first = dd.read_bytes()
    assert os.path.getsize(dd) > 0
    dd.unlink()
    assert ninja(build).returncode == 0
    assert dd.read_bytes() == first


@no_gcc_std
def test_import_std(tmp_path):
    spec = """
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"]]
cxx_module_interface = [["lib", "m.cppm", "public"]]
cxx_import_std = [["app"], ["lib"]]
dep = [["app", "lib", "private"]]
prop = [["lib", "cxx.std", "23", "public"]]
request = [["app", "rel"]]
"""
    files = {
        "m.cppm": "export module m;\nimport std;\nexport int f() { std::vector<int> v{1}; return (int)v.size() - 1; }\n",
        "main.cpp": 'import m;\nimport std;\nint main() { std::cout << f() << "\\n"; }\n',
    }
    build = built(tmp_path, spec, files)
    assert run_app(build).stdout == "0\n"
    assert len(list((build / "souffle" / "bmi").rglob("std.*.pcm"))) == 1


@no_gcc_std
def test_std_import_without_declaration_is_an_error(tmp_path):
    spec = BASIC_SPEC
    files = dict(BASIC)
    files["m_priv.cppm"] = "export module m:priv;\nexport int hidden() { return 3; }\n"
    files["main.cpp"] = "import m;\nimport std;\nint main() { return f() + g() == 3 ? 0 : 1; }\n"
    out = collate_errors(tmp_path, spec, files)
    assert "import of a module no declared interface unit provides" in out and "imports std" in out


def test_scan_results_never_reenter_configure(tmp_path):
    """The loader rejects a configure layer that names a relation of the
    build-time stratum."""
    from buildlog.engine import configure_project
    from buildlog.errors import LoaderError

    proj, user = project(tmp_path, HEAD + BASIC_SPEC, BASIC, tc=TC)
    pkg = proj / "packages" / "peek"
    pkg.mkdir(parents=True)
    (pkg / "manifest.toml").write_text(
        'name = "peek"\nversion = "0.1.0"\nrank = 5\nnamespace = "peek"\nlayers = ["peek.dl"]\n'
        'requires = { buildlog-cpp = ">=0.1" }\nextends = ["error"]\n'
    )
    (pkg / "peek.dl").write_text('error("peeking", n) :- scan_requires(_, n).\n')
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('uses = ["buildlog-cpp"]', 'uses = ["buildlog-cpp", "peek"]')
    )
    with pytest.raises(LoaderError) as e:
        configure_project(proj, tmp_path / "b", update_lock=True, user_files=[user])
    assert "scan_requires" in str(e.value) and "build-time" in str(e.value)


def test_why_explains_dynamic_edges(tmp_path, capsys):
    from buildlog.cli import main

    build = built(
        tmp_path,
        BASIC_SPEC,
        {**BASIC, "m_priv.cppm": "export module m:priv;\nexport int hidden() { return 3; }\n"},
    )
    obj = "obj/arm64-macos.release.none/app/main.cpp.o"
    assert main(["why", obj, "-C", str(build), str(tmp_path / "proj")]) == 0
    out = capsys.readouterr().out
    assert "Dynamic edges, from the build-time collation:" in out
    assert "main.cpp imports m, provided by" in out and "of target lib (public interface)" in out
