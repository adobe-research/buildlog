# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded preparation reads files, never inference or environmental answers."""

import json
from types import SimpleNamespace

import pytest

from buildlog import providers, rule_index
from buildlog.available import Certificate, File
from buildlog.engine import project_packages
from buildlog.errors import LoaderError
from buildlog.facts import preset_path
from buildlog.paths import souffle_path


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    held = dict(providers._registry)
    yield
    providers._registry.clear()
    providers._registry.update(held)


def manifest(directory, name=None, extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.toml"
    path.write_text(
        f'name = "{name or directory.name}"\nversion = "1"\nrank = 100\nlayers = []\n{extra}'
    )
    return path


def project_with(tmp_path, text=""):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('uses = ["buildlog-cpp"]\nspec = []\n' + text)
    return project


def prepare(project, user=None):
    return rule_index.prepare(project, [user] if user else [], {}, project_packages(project), [])


def user_file(tmp_path, text):
    path = tmp_path / "user.toml"
    path.write_text(text)
    return path


def pc(path, rules=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((f"buildlog_rules={rules}\n" if rules else "") + "Name: test\nVersion: 1\n")
    return path


def test_all_formats_and_foreign_platform_roots_are_bounded(tmp_path):
    prefix = tmp_path / "prefix"
    buildlog = manifest(prefix / "share/buildlog/from-buildlog")
    pc_rules = manifest(tmp_path / "from-pc")
    pcfile = pc(prefix / "lib/pkgconfig/a.pc", pc_rules.parent)
    cps_rules = manifest(tmp_path / "from-cps")
    cps = prefix / "lib/cps/pkg/version/pkg.cps"
    cps.parent.mkdir(parents=True)
    cps.write_text(
        json.dumps(
            {"name": "pkg", "cps_version": "0.13", "x_buildlog_rules": [str(cps_rules.parent)]}
        )
    )
    cmake = prefix / "lib/cmake/pkg/Exports.cmake"
    cmake.parent.mkdir(parents=True)
    from buildlog_cpp.importers.cmake_export import MAIN_MARK

    cmake.write_text(MAIN_MARK + "\n")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_prefix_on = [["foreign", "{prefix}"]]\n')
    prepared = prepare(project, user)
    assert {p.name for p in prepared.packages} == {
        "buildlog",
        "buildlog-cpp",
        "from-pc",
        "from-cps",
    }
    assert {p.name for p in prepared.metadata_packages} == {"from-buildlog"}
    assert {f.path for f in prepared.files} == {buildlog, pcfile, cps, cmake}
    assert prepared.explicit_manifests == frozenset(
        p.candidate.manifest.resolve()
        for p in prepared.packages
        if p.name in {"buildlog", "buildlog-cpp"}
    )
    assert prepared.certificate.valid()


def test_selected_preset_does_not_preload_other_presets(tmp_path):
    one = pc(tmp_path / "one/pkg.pc")
    two = pc(tmp_path / "two/pkg.pc")
    project = project_with(
        tmp_path,
        f'[presets.one]\npackage_search_dir = [["pc", "{one.parent}"]]\n'
        f'[presets.two]\npackage_search_dir = [["pc", "{two.parent}"]]\n',
    )
    prepared = prepare(project, preset_path(project, "one"))
    assert [f.path for f in prepared.files] == [one]
    assert str(two) not in prepared.certificate.files


def test_includes_close_across_formats_and_cycles(tmp_path):
    root = tmp_path / "pkg"
    manifest(
        root,
        extra='imports = [{ format = "buildlog", file = "manifest.toml" }, '
        '{ format = "pc", file = "dep.pc" }]\n',
    )
    pc(root / "dep.pc")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["buildlog", "{root}/manifest.toml"]]\n')
    prepared = prepare(project, user)
    assert {f.path for f in prepared.files} == {root / "manifest.toml", root / "dep.pc"}
    assert len(prepared.files) == 2


def test_included_metadata_and_new_supplements_invalidate_certificate(tmp_path):
    cps = tmp_path / "metadata/Pkg.cps"
    cps.parent.mkdir()
    cps.write_text('{"name":"Pkg","cps_version":"0.13"}')
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["cps", "{cps}"]]\n')
    first = prepare(project, user)
    rules = manifest(tmp_path / "supplement-rules")
    (cps.parent / "Pkg-debug.cps").write_text(json.dumps({"x_buildlog_rules": [str(rules.parent)]}))
    assert not first.certificate.valid()
    second = prepare(project, user)
    assert "supplement-rules" in {p.name for p in second.packages}


def test_missing_root_becoming_empty_directory_invalidates(tmp_path):
    late = tmp_path / "late"
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{late}"]]\n')
    first = prepare(project, user)
    assert not first.files
    late.mkdir()
    assert not first.certificate.valid()
    second = prepare(project, user)
    pc(late / "new.pc")
    assert not second.certificate.valid()
    assert len(prepare(project, user).files) == 1


def test_symlink_retarget_with_identical_contents_invalidates(tmp_path):
    one = pc(tmp_path / "one/pkg.pc")
    two = pc(tmp_path / "two/pkg.pc")
    link = tmp_path / "link"
    link.symlink_to(one.parent, target_is_directory=True)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{link}"]]\n')
    first = prepare(project, user)
    link.unlink()
    link.symlink_to(two.parent, target_is_directory=True)
    assert not first.certificate.valid()


def test_metadata_edit_changes_certificate_not_program_identity(tmp_path):
    file = pc(tmp_path / "metadata/pkg.pc")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{file.parent}"]]\n')
    first = prepare(project, user)
    file.write_text(file.read_text().replace("Version: 1", "Version: 2"))
    second = prepare(project, user)
    assert first.code_digest == second.code_digest
    assert first.certificate.digest != second.certificate.digest
    assert first.files[0].identity == second.files[0].identity


def test_preparation_cache_hit_does_not_repeat_provider_traversal(tmp_path, monkeypatch):
    metadata = pc(tmp_path / "metadata/pkg.pc")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    packages = project_packages(project)
    cache = tmp_path / "build/prepared.pkl"
    first = rule_index.prepare_cached(project, [user], {}, packages, [], cache)

    def forbidden(*args, **kwargs):
        pytest.fail("a certified hit must not traverse providers")

    monkeypatch.setattr(rule_index, "prepare", forbidden)
    providers._registry.clear()
    held = rule_index.prepare_cached(project, [user], {}, packages, [], cache)
    assert held == first and held.cached
    assert providers.registered("pc")


@pytest.mark.parametrize("change", ["contents", "addition", "environment", "corrupt-cache"])
def test_preparation_cache_rechecks_inputs(tmp_path, change, monkeypatch):
    metadata = pc(tmp_path / "metadata/pkg.pc")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    packages = project_packages(project)
    cache = tmp_path / "build/prepared.pkl"
    rule_index.prepare_cached(project, [user], {}, packages, [], cache)
    if change == "contents":
        metadata.write_text(metadata.read_text() + "# edit\n")
    elif change == "addition":
        pc(metadata.with_name("new.pc"))
    elif change == "environment":
        monkeypatch.setenv("BUILDLOG_PREPARATION_TEST", "changed")
    else:
        cache.write_bytes(b"not a cache")
    actual = rule_index.prepare_cached(project, [user], {}, packages, [], cache)
    assert not actual.cached and actual.certificate.valid()
    if change == "addition":
        assert {f.path.name for f in actual.files} == {"pkg.pc", "new.pc"}


def test_preparation_cache_reresolves_entry_point_identities(tmp_path, monkeypatch):
    from dataclasses import replace

    rules = manifest(tmp_path / "rules")
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    packages = project_packages(project)
    cache = tmp_path / "build/prepared.pkl"
    rule_index.prepare_cached(project, [user], {}, packages, [], cache)
    real = rule_index.resolve_packages

    def changed(*args, **kwargs):
        resolved = real(*args, **kwargs)
        for package in resolved:
            if package.name == "rules":
                package.candidate = replace(package.candidate, entry_point="different:entry")
        return resolved

    monkeypatch.setattr(rule_index, "resolve_packages", changed)
    actual = rule_index.prepare_cached(project, [user], {}, packages, [], cache)
    assert not actual.cached
    assert (
        next(p for p in actual.packages if p.name == "rules").candidate.entry_point
        == "different:entry"
    )


def test_code_edit_changes_program_identity(tmp_path):
    rules = manifest(tmp_path / "pkg", extra='facts = ["data.facts"]\n')
    facts = rules.parent / "data.facts"
    facts.write_text("first\n")
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    first = prepare(project, user)
    facts.write_text("second\n")
    assert not first.certificate.valid()
    assert first.code_digest != prepare(project, user).code_digest


def test_unused_bad_metadata_is_deferred_but_bad_code_is_not(tmp_path):
    metadata = tmp_path / "metadata/broken.pc"
    metadata.parent.mkdir()
    metadata.write_text("not pkgconfig\n")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    first = prepare(project, user)
    assert File("pc", metadata) in first.deferred_errors
    broken = tmp_path / "bad-rules"
    broken.mkdir()
    (broken / "manifest.toml").write_text("name = \n")
    pc(metadata, broken)
    with pytest.raises(LoaderError, match="bad-rules"):
        prepare(project, user)


def test_conflicting_dormant_rule_identities_are_not_filtered_by_a_pin(tmp_path):
    one = manifest(tmp_path / "one", "same")
    two = manifest(tmp_path / "two", "same")
    pc(tmp_path / "metadata/a.pc", one.parent)
    pc(tmp_path / "metadata/b.pc", two.parent)
    project = project_with(tmp_path)
    user = user_file(
        tmp_path,
        f'package_search_dir = [["pc", "{tmp_path}/metadata"]]\n'
        f'package_pin = [["a", "{tmp_path}/metadata/a.pc"]]\n',
    )
    with pytest.raises(LoaderError, match="found twice"):
        prepare(project, user)


def test_missing_requirement_cannot_silently_remove_a_rule_package(tmp_path):
    rules = manifest(tmp_path / "pkg", extra='requires = { unavailable = ">=1" }\n')
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    with pytest.raises(LoaderError, match="unresolved requirements"):
        prepare(project, user)


def test_manifest_requirements_are_in_the_union_and_certificate(tmp_path):
    project = project_with(tmp_path)
    dep = manifest(project / "packages/dep")
    rules = manifest(tmp_path / "pkg", extra='requires = { dep = ">=1" }\n')
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    first = prepare(project, user)
    assert {p.name for p in first.packages} >= {"pkg", "dep"}
    dep.write_text(dep.read_text().replace('version = "1"', 'version = "2"'))
    assert not first.certificate.valid()


def test_rule_pointer_manifest_imports_are_walked_without_a_find(tmp_path):
    root = tmp_path / "pkg"
    rules = manifest(root, extra='imports = [{ format = "pc", file = "extra.pc" }]\n')
    extra_rules = manifest(tmp_path / "extra")
    pc(root / "extra.pc", extra_rules.parent)
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    prepared = prepare(project, user)
    assert {p.name for p in prepared.packages} >= {"pkg", "extra"}
    assert File("pc", root / "extra.pc") in prepared.files


def test_provider_declared_by_indexed_rule_code_is_rejected_before_registration(tmp_path):
    root = tmp_path / "pkg"
    rules = manifest(
        root,
        extra='imports = [{ format = "late", file = "extra.pc" }]\n'
        '[[provider]]\nname = "late"\nentry = "buildlog_cpp.importers:PC"\n',
    )
    pc(root / "extra.pc")
    metadata = pc(tmp_path / "metadata/pkg.pc", rules.parent)
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    with pytest.raises(
        LoaderError, match=r"pkg:.*\[\[provider\]\].*only registered Python plugins"
    ):
        prepare(project, user)


def test_provider_without_bounded_contract_is_an_error(tmp_path, monkeypatch):
    project = project_with(tmp_path)
    monkeypatch.setattr(providers, "provider", lambda _: object())
    with pytest.raises(LoaderError, match="bounded enumerate_files and metadata_links"):
        prepare(project)


def test_source_project_uses_its_own_variables_for_roots(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    metadata = pc(source / "metadata/dep.pc")
    (source / "buildlog.toml").write_text(
        'spec = []\npackage_search_dir = [["pc", "${DEPS}"]]\n'
        f'[vars]\nDEPS = "{metadata.parent}"\n'
    )
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'source_project = [["source", "{source}"]]\n')
    prepared = prepare(project, user)
    assert File("pc", metadata) in prepared.files
    metadata.write_text(metadata.read_text() + "# source edit\n")
    assert not prepared.certificate.valid()


def test_direct_and_included_namespace_options_are_distinct_bundle_identities(tmp_path):
    root = tmp_path / "pkg"
    manifest(
        root,
        extra='imports = [{ format = "cmake", file = "Exports.cmake", namespace = "Included" }]\n',
    )
    cmake = root / "Exports.cmake"
    cmake.write_text("")
    project = project_with(tmp_path)
    user = user_file(
        tmp_path,
        f'package_file = [["buildlog", "{root}/manifest.toml"], ["cmake", "{cmake}"]]\n'
        f'package_namespace = [["{cmake}", "Direct"]]\n',
    )
    prepared = prepare(project, user)
    variants = [f for f in prepared.files if f.provider == "cmake"]
    assert {f.namespace for f in variants} == {"Direct", "Included"}
    assert len({f.identity for f in variants}) == 2


def test_named_root_tables_and_row_tables_are_in_the_static_scope(tmp_path):
    one = pc(tmp_path / "one/pkg.pc")
    two = pc(tmp_path / "two/pkg.pc")
    project = project_with(tmp_path)
    user = user_file(
        tmp_path,
        f'[package_search_dir.pc]\ndir = "{one.parent}"\n'
        f'[[package_file]]\nprovider = "pc"\npath = "{two}"\n',
    )
    assert {f.path for f in prepare(project, user).files} == {one, two}


def test_ground_rule_and_fact_roots_do_not_run_inference_or_package_probes(tmp_path, monkeypatch):
    from buildlog import engine

    one = pc(tmp_path / "one/pkg.pc")
    two = pc(tmp_path / "two/pkg.pc")
    project = project_with(tmp_path)
    (project / "buildlog.toml").write_text(
        'uses = ["buildlog-cpp"]\nspec = ["roots.dl", "package_search_dir.facts"]\n'
        '[author_decides]\npackage_search_dir = "fixture names both roots"\n'
    )
    (project / "roots.dl").write_text(f'package_search_dir("pc", "{one.parent}").\n')
    (project / "package_search_dir.facts").write_text(f"pc\t{two.parent}\n")

    def forbidden(*args, **kwargs):
        pytest.fail("preparation must not compose or execute package probes")

    monkeypatch.setattr(engine, "compose_program", forbidden)
    monkeypatch.setattr(providers, "pkg_find", forbidden)
    monkeypatch.setattr(providers, "pkg_import", forbidden)
    assert {f.path for f in prepare(project).files} == {one, two}


def test_available_database_is_decoded_without_find_or_package_probes(tmp_path, monkeypatch):
    from buildlog_cpp.importers import FormatProvider

    metadata = pc(tmp_path / "metadata/pkg.pc")
    metadata.write_text(metadata.read_text() + "Provides: virtual\n")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    prepared = prepare(project, user)

    def forbidden(*args, **kwargs):
        pytest.fail("available data must not depend on demand-driven find/import probes")

    monkeypatch.setattr(FormatProvider, "find", forbidden)
    monkeypatch.setattr(providers, "pkg_find", forbidden)
    monkeypatch.setattr(providers, "pkg_import", forbidden)
    data = rule_index.available_rows(
        prepared,
        providers.Vocabulary(frozenset({"linux", "macos"}), frozenset({"arm64", "x86_64"})),
    )
    assert {row[0] for row in data["av_pkg_offer"]} == {"pkg", "pc:pkg"}
    assert any(row[1:3] == ("virtual", "pc:pkg") for row in data["av_pkg_alias"])
    assert all(not name.startswith("res_") for name in data)
    assert data["av_pkg_bundle"] == [(File("pc", metadata).identity, "pc", str(metadata), "", "")]


def test_available_database_defers_bad_metadata_but_keeps_its_finder_offer(tmp_path):
    metadata = tmp_path / "metadata/broken.pc"
    metadata.parent.mkdir()
    metadata.write_text("invalid metadata\n")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_search_dir = [["pc", "{metadata.parent}"]]\n')
    prepared = prepare(project, user)
    data = rule_index.available_rows(
        prepared, providers.Vocabulary(frozenset({"linux"}), frozenset({"x86_64"}))
    )
    assert {row[0] for row in data["av_pkg_offer"]} == {"broken", "pc:broken"}
    assert data["av_pkg_target"] == []
    assert len(data["av_pkg_error"]) == 1
    assert data["av_pkg_import"] == [(File("pc", metadata).identity, "error")]


def test_mutation_during_preparation_is_rejected(tmp_path, monkeypatch):
    from buildlog_cpp.importers import FormatProvider

    metadata = pc(tmp_path / "metadata/pkg.pc")
    project = project_with(tmp_path)
    user = user_file(tmp_path, f'package_file = [["pc", "{metadata}"]]\n')
    real = FormatProvider.metadata_links

    def mutate(self, path, observations):
        result = real(self, path, observations)
        path.write_text(path.read_text() + "# changed\n")
        return result

    monkeypatch.setattr(FormatProvider, "metadata_links", mutate)
    with pytest.raises(LoaderError, match="changed"):
        prepare(project, user)


def test_certificate_roundtrip_and_file_namespace_identity(tmp_path):
    file = pc(tmp_path / "pkg.pc")
    certificate = Certificate()
    certificate.file(file)
    certificate.directory(tmp_path)
    held = Certificate.from_json(json.loads(json.dumps(certificate.to_json())))
    assert held is not None and held.valid() and held.digest == certificate.digest
    assert Certificate.from_json({"version": 1, "files": [], "directories": {}}) is None
    assert File("pc", file).identity != File("cps", file).identity
    assert File("cmake", file, "One").identity != File("cmake", file, "Two").identity


@pytest.mark.skipif(not souffle_path().is_file(), reason="patched Souffle required")
def test_static_vocabulary_comes_from_expanded_plugin_declarations(tmp_path):
    from buildlog.engine import prepare_program

    project = project_with(tmp_path)
    config = project / "buildlog.toml"
    config.write_text('uses = ["buildlog-cpp", "buildlog-ndk", "buildlog-emsdk"]\nspec = []\n')
    packages = project_packages(project)
    loaded = prepare_program(tmp_path / "build", [p.ref for p in packages])
    vocabulary = providers.static_vocabulary(loaded.layers)
    assert vocabulary.os == frozenset({"macos", "linux", "android", "emscripten"})
    assert vocabulary.cpu == frozenset({"arm64", "x86_64", "wasm32"})


def test_query_dependent_platform_vocabulary_is_rejected():
    layer = SimpleNamespace(
        manifest="dynamic/manifest.toml",
        info=SimpleNamespace(
            clauses=[
                (
                    "platform_vocabulary",
                    {"res_inventory"},
                    'platform_vocabulary("os", os) :- res_inventory(_, os).',
                )
            ]
        ),
    )
    with pytest.raises(LoaderError, match="unconditional literal"):
        providers.static_vocabulary([layer])
