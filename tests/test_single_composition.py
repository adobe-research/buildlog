# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One certified executable union, with no package-driven restart or state oracle."""

import json
import platform
import shutil
from pathlib import Path

import pytest

from buildlog import engine, providers, rule_index, uptodate
from buildlog.engine import STATE_FILE, configure_project
from buildlog.errors import LoaderError
from buildlog.facts import read_rows
from buildlog.paths import souffle_path

from .host import MACOS
from .test_importers import BARE_HUM
from .test_objfile import ARM64, X86_64, fat
from .test_packages import FOUND, actions, lock, make_project
from .test_probe_seeding import _final_in, assert_same_derivation

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)
    monkeypatch.setenv("BUILDLOG_UPTODATE", "1")


def project_for(tmp_path: Path, rules: Path = FOUND) -> tuple[Path, Path]:
    directory = tmp_path / "metadata"
    directory.mkdir()
    metadata = directory / "rules.pc"
    metadata.write_text(f"buildlog_rules={rules}\nName: rules\nVersion: 1\n")
    cpu = {"aarch64": "arm64", "AMD64": "x86_64"}.get(platform.machine(), platform.machine())
    host = f"{cpu}-{'macos' if MACOS else 'linux'}"
    spec = (
        f"package_search_dir = [['pc', {json.dumps(str(directory))}]]\n"
        'target = [["root"]]\ndep = [["root", "pc:rules", "public"]]\n'
        f'config_decl = [["d", "{host}"]]\nrequest = [["root", "d"]]\n'
    )
    project = make_project(tmp_path, ["buildlog-cpp"], spec)
    return project, metadata


def snapshot(build):
    return {f.name: f.read_bytes() for f in (build / "souffle/facts").iterdir()}


def test_cold_composes_once_and_warm_skips_preparation_decoding_and_inference(
    tmp_path, monkeypatch
):
    project, _ = project_for(tmp_path)
    build = tmp_path / "build"
    facts, cold = configure_project(project, build)
    assert cold["composition_calls"] == cold["evaluator_startups"] == 1
    assert len(cold["outer"]) == 1
    assert actions(facts) == ["out:found/marker"]
    assert "found" in lock(project)
    before = snapshot(build), (build / "souffle/build.ninja").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("an unchanged configure must reuse its certified result")

    monkeypatch.setattr(rule_index, "prepare", forbidden)
    monkeypatch.setattr(providers, "decode_file", forbidden)
    monkeypatch.setattr(engine, "compose_program", forbidden)
    monkeypatch.setattr(engine.SouffleRounds, "__init__", forbidden)
    for _ in range(2):
        _, warm = configure_project(project, build)
        assert warm["up_to_date"]
        assert warm["composition_calls"] == warm["evaluator_startups"] == warm["evaluations"] == 0
        assert (snapshot(build), (build / "souffle/build.ninja").read_bytes()) == before


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_metadata_certificate_invalidates_warm_reuse(tmp_path, change):
    project, metadata = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    if change == "edit":
        metadata.write_text(metadata.read_text().replace("Version: 1", "Version: 2"))
    elif change == "add":
        (metadata.parent / "other.pc").write_text("Name: other\n")
    else:
        metadata.unlink()
    if change == "remove":
        with pytest.raises(SystemExit):
            configure_project(project, build, emit=False)
        changed = json.loads((build / "metrics.json").read_text())
    else:
        _, changed = configure_project(project, build, emit=False)
    assert not changed.get("up_to_date")
    assert changed["composition_calls"] == 1
    assert bool(changed["errors"]) == (change == "remove")


def test_author_edit_matches_fresh_same_program_without_probes(tmp_path, monkeypatch):
    project, _ = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build)
    spec = project / "spec.toml"
    spec.write_text(spec.read_text() + 'prop = [["root", "cc.define", "EDIT", "public"]]\n')
    _, edited = configure_project(project, build)
    assert edited["composition_calls"] == 1
    assert edited["executed"] == 0
    monkeypatch.setenv("BUILDLOG_SEED_PROBES", "0")
    configure_project(project, tmp_path / "fresh")
    assert_same_derivation(build, tmp_path / "fresh")


@pytest.mark.parametrize("damage", ["reason", "missing-dir", "malformed"])
def test_recorded_modules_never_supply_code(tmp_path, monkeypatch, damage):
    project, _ = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    state = json.loads((build / STATE_FILE).read_text())
    if damage == "reason":
        state["discovered"][0][1] = "untrusted reason"
    elif damage == "missing-dir":
        state["discovered"] = [[str(tmp_path / "gone"), "untrusted directory"]]
    else:
        state["discovered"] = "nonsense"
    (build / STATE_FILE).write_text(json.dumps(state))
    monkeypatch.setenv("BUILDLOG_UPTODATE", "0")
    facts, metrics = configure_project(project, build, emit=False)
    assert metrics["composition_calls"] == 1
    assert actions(facts) == ["out:found/marker"]
    state = json.loads((build / STATE_FILE).read_text())
    assert state["discovered"] == [[str(FOUND.resolve()), "used rule module"]]


def test_changed_rule_pointer_reprepares_the_union(tmp_path):
    project, metadata = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    other = tmp_path / "found-too"
    shutil.copytree(FOUND, other)
    metadata.write_text(metadata.read_text().replace(str(FOUND), str(other)))
    facts, metrics = configure_project(project, build, emit=False, update_lock=True)
    assert metrics["composition_calls"] == metrics["evaluator_startups"] == 1
    assert actions(facts) == ["out:found/marker"]
    state = json.loads((build / STATE_FILE).read_text())
    assert state["discovered"] == [[str(other.resolve()), "used rule module"]]


def test_unused_module_stays_available_but_leaves_the_lock(tmp_path):
    project, _ = project_for(tmp_path)
    build = tmp_path / "build"
    configure_project(project, build, emit=False)
    assert "found" in lock(project)
    spec = project / "spec.toml"
    spec.write_text(spec.read_text().replace('dep = [["root", "pc:rules", "public"]]\n', ""))
    facts, metrics = configure_project(project, build, emit=False)
    assert "found" in metrics["outer"][0]["packages"]
    assert "found" not in lock(project)
    assert actions(facts) == []


def test_rule_generated_metadata_file_must_be_certified(tmp_path, capsys):
    project, _ = project_for(tmp_path)
    directory = tmp_path / "uncertified"
    directory.mkdir()
    metadata = directory / "late.pc"
    metadata.write_text("buildlog_rules=missing\nName: late\n")
    entry = project / "buildlog.toml"
    entry.write_text(
        entry.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "late.dl"]')
        + 'package_file = "test uncertified rule-generated scope"\n'
    )
    (project / "late.dl").write_text(
        ".decl late_seed()\nlate_seed().\n"
        f'package_file("pc", {json.dumps(str(metadata))}) :- late_seed().\n'
    )
    build = tmp_path / "build"
    with pytest.raises(SystemExit):
        configure_project(project, build, emit=False)
    assert "uncertified package file" in capsys.readouterr().err
    metrics = json.loads((build / "metrics.json").read_text())
    assert metrics["composition_calls"] == 1
    assert len(metrics["outer"]) == 1


def test_metadata_change_during_inference_cannot_publish_warm_success(tmp_path, monkeypatch):
    project, metadata = project_for(tmp_path)
    build = tmp_path / "build"
    run = engine.run_fixpoint

    def changing(*args, **kwargs):
        result = run(*args, **kwargs)
        metadata.write_text(metadata.read_text().replace("Version: 1", "Version: 2"))
        return result

    monkeypatch.setattr(engine, "run_fixpoint", changing)
    with pytest.raises(LoaderError, match="preparation inputs changed"):
        configure_project(project, build)
    assert not (build / uptodate.FILE).exists()
    assert not list(build.glob(".available-certificate-*"))


def test_late_sysroot_paths_and_ordered_library_watches_use_the_fixed_program(tmp_path):
    root = tmp_path / "sysroot"
    first, second = [root / f"opt/{name}" for name in ("first", "second")]
    first.mkdir(parents=True)
    second.mkdir()
    later = second / "libsample.so"
    later.write_bytes(fat(ARM64, X86_64))
    directory = root / "usr/lib/pkgconfig"
    directory.mkdir(parents=True)
    (directory / "sample.pc").write_text(
        "Name: sample\nCflags: -I/usr/include/sample\n"
        "Libs: -L/opt/first -L/opt/second -lsample -lmissing\n"
    )
    (directory / "unused.pc").write_text("Name: unused\nLibs: -L/opt/first -lunused\n")
    cpu = {"aarch64": "arm64", "AMD64": "x86_64"}.get(platform.machine(), platform.machine())
    host = f"{cpu}-{'macos' if MACOS else 'linux'}"
    project = make_project(
        tmp_path,
        ["buildlog-cpp"],
        f"package_search_dir = [['pc', {json.dumps(str(directory))}]]\n"
        'target = [["root"]]\ndep = [["root", "pc:sample", "public"]]\n'
        'native_kind = [["root", "executable"]]\ncxx_source = [["root", "main.cpp"]]\n'
        f'config_decl = [["d", "{host}"]]\nrequest = [["root", "d"]]\n',
    )
    (project / "main.cpp").write_text("int main() { return 0; }\n")
    entry = project / "buildlog.toml"
    entry.write_text(
        entry.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "roots.dl"]')
    )
    # Inventory supplies the host during inference, not during metadata decoding.
    (project / "roots.dl").write_text(
        f"platform_sysroot(p, {json.dumps(str(root))}) :- host_platform(p).\n"
    )
    build = tmp_path / "build"
    facts, cold = configure_project(project, build)
    assert cold["composition_calls"] == cold["evaluator_startups"] == 1
    assert any(row[2] == str(root) for row in read_rows(facts / "req_metadata_path.csv"))
    ninja = (build / "souffle/build.ninja").read_text()
    assert str(later) in ninja
    assert str(root / "usr/include/sample") in ninja
    assert "-lmissing" in ninja and "-lunused" not in ninja
    assert not any(
        "-lunused" in row for row in read_rows(_final_in(build) / "res_metadata_path.facts")
    )
    _, warm = configure_project(project, build)
    assert warm["up_to_date"] and warm["evaluations"] == 0

    earlier = first / "libsample.a"
    earlier.write_bytes(fat(ARM64, X86_64))
    facts, changed = configure_project(project, build)
    assert not changed.get("up_to_date")
    assert changed["composition_calls"] == changed["evaluator_startups"] == 1
    ninja = (build / "souffle/build.ninja").read_text()
    assert str(earlier) in ninja and str(later) not in ninja
    fresh = tmp_path / "fresh"
    configure_project(project, fresh, fresh=True)
    assert_same_derivation(build, fresh)


@pytest.mark.parametrize("mode", ["search-pin", "file-namespace", "generated-file"])
def test_relative_metadata_policy_joins_the_certified_spelling(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "metadata"
    directory.mkdir()
    cpu = {"aarch64": "arm64", "AMD64": "x86_64"}.get(platform.machine(), platform.machine())
    host = f"{cpu}-{'macos' if MACOS else 'linux'}"
    dependency = "Scoped::hum" if mode == "file-namespace" else "pc:sample"
    project = make_project(
        tmp_path,
        ["buildlog-cpp"],
        'target = [["root"]]\nnative_kind = [["root", "executable"]]\n'
        'cxx_source = [["root", "main.cpp"]]\n'
        f'dep = [["root", "{dependency}", "private"]]\n'
        f'config_decl = [["d", "{host}"]]\nrequest = [["root", "d"]]\n',
    )
    (project / "main.cpp").write_text("int main() { return 0; }\n")
    rules = ""
    if mode == "file-namespace":
        template = BARE_HUM.read_text()
        (directory / "Targets.cmake").write_text(
            template.split("add_library(", 1)[0] + "add_library(hum INTERFACE IMPORTED)\n"
            'set_target_properties(hum PROPERTIES INTERFACE_COMPILE_DEFINITIONS "CHOSEN")\n'
            "add_library(hum_dylib INTERFACE IMPORTED)\n"
            + template[template.index("set(_IMPORT_PREFIX)\n") :]
        )
        user = 'package_file = [["cmake", "metadata/Targets.cmake"]]\n'
        rules = 'package_namespace("metadata/Targets.cmake", "Scoped").\n'
    else:
        (directory / "sample.pc").write_text("Name: sample\nCflags: -DCHOSEN\n")
        user = 'package_search_dir = [["pc", "metadata/"]]\n'
        if mode == "search-pin":
            other = tmp_path / "other"
            other.mkdir()
            (other / "sample.pc").write_text("Name: sample\nCflags: -DNOT_CHOSEN\n")
            user = (
                'package_search_dir = [["pc", "metadata/"], ["pc", "other"]]\n'
                'package_pin = [["pc:sample", "metadata/sample.pc"]]\n'
            )
        else:
            rules = (
                ".decl selected_file()\nselected_file().\n"
                'package_file("pc", "metadata/sample.pc") :- selected_file().\n'
            )
    if rules:
        entry = project / "buildlog.toml"
        entry.write_text(
            entry.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "paths.dl"]')
            + (
                '[author_decides]\npackage_file = "test certified generated file"\n'
                if mode == "generated-file"
                else ""
            )
        )
        (project / "paths.dl").write_text(rules)
    preset = project / "user.toml"
    preset.write_text(user)
    build = tmp_path / "build"
    _, metrics = configure_project(project, build, user_files=[preset])
    assert metrics["composition_calls"] == metrics["evaluator_startups"] == 1
    ninja = (build / "souffle/build.ninja").read_text()
    assert "-DCHOSEN" in ninja and "NOT_CHOSEN" not in ninja
