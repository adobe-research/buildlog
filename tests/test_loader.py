# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Loader checks, generated IO, qualification, visibility and probes through configure_project."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog import layers
from buildlog.engine import LoaderError, configure_project, project_packages
from buildlog.facts import read_rows
from buildlog.paths import CORE_RULES, souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COLLISION = FIXTURES / "collision"
REJECTED = FIXTURES / "rejected"
RULE_PACKAGES = FIXTURES / "rule-packages"
CORE = CORE_RULES / "manifest.toml"
CPP = ROOT / "plugins" / "buildlog-cpp" / "src" / "buildlog_cpp" / "manifest.toml"
TEST_PLUGINS = ROOT / "tests" / "plugins" / "buildlog-testplugins" / "src" / "buildlog_testplugins"
HALIDE = RULE_PACKAGES / "share" / "buildlog" / "halide" / "manifest.toml"
BLURGEN = ROOT / "examples" / "cross" / "packages" / "blurgen" / "manifest.toml"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def _final_round(build):
    """The one round dir configure leaves: facts-in/round-<final_round>."""
    return f"round-{json.loads((build / 'metrics.json').read_text())['final_round']}"


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


def make_project(root, uses, spec=""):
    project = root / "project"
    project.mkdir()
    listing = 'spec = ["spec.toml"]\n' if spec else ""
    decides = (
        '[author_decides]\ntoolchain_pin = "test fixture pins Apple clang"\n'
        if "toolchain_pin" in spec
        else ""
    )
    (project / "buildlog.toml").write_text(f"uses = {json.dumps(uses)}\n{listing}{decides}")
    if spec:
        (project / "spec.toml").write_text(spec)
    return project


def _rejected(tmp_path, *uses, spec=""):
    project = make_project(tmp_path, list(uses), spec)
    build = tmp_path / "build"
    with pytest.raises(LoaderError) as exc:
        configure_project(project, build, emit=False, package_paths=[REJECTED])
    # The checks run before any probe or derivation, and lock nothing.
    assert not (build / "env-facts").exists()
    assert not (build / "rounds").exists()
    assert not (build / "probe-cache.json").exists()
    assert not (project / "buildlog.lock").exists()
    return exc.value.errors


def test_shipped_layers_carry_no_io():
    manifests = [
        CORE,
        CPP,
        HALIDE,
        *sorted((ROOT / "examples").glob("*/packages/*/manifest.toml")),
        *sorted(TEST_PLUGINS.glob("*/manifest.toml")),
    ]
    for manifest in manifests:
        data = layers.parse_manifest(manifest)
        for name in data["layers"]:
            text = (manifest.parent / name).read_text()
            for directive in layers.FORBIDDEN:
                assert f"\n{directive}" not in "\n" + text, (manifest, name, directive)


def test_io_directive_in_package_layer_fails_configure(tmp_path):
    errors = _rejected(tmp_path, "io-directive")
    assert any("io-directive: forbidden directive .input" in e for e in errors), errors
    assert any("io-directive: forbidden directive .output" in e for e in errors), errors


def test_extends_outside_open_registry_rejected(tmp_path):
    errors = _rejected(tmp_path, "closed-extends")
    assert errors == [
        (
            "closed-extends: extends needed, which is not an open relation of a lower layer "
            "(owned by buildlog, not in its open registry)"
        )
    ]


def test_invalid_probe_declarations_rejected(tmp_path):
    errors = _rejected(tmp_path, "test-bad-probe")
    expected = [
        (
            "test-bad-probe: probe stolen: request relation req_inventory is not declared by "
            "test-bad-probe (it is owned by buildlog-cpp)"
        ),
        (
            "test-bad-probe: probe derived_result: request req_derived(symbol, symbol) and "
            "domain domain_derived(symbol) must have the same attribute types"
        ),
        "test-bad-probe: probe derived_result: result relation res_derived has rule clauses",
    ]
    for want in expected:
        assert any(e.startswith(want) for e in errors), (want, errors)


def test_unloadable_probe_entry_rejected(tmp_path):
    errors = _rejected(tmp_path, "test-bad-entry")
    assert len(errors) == 1
    assert errors[0].startswith("test-bad-entry: probe bad_entry: cannot load entry point")


def test_pure_rule_package_declaring_probe_rejected(tmp_path):
    errors = _rejected(tmp_path, "pure-probe")
    assert errors == [
        (
            "pure-probe: rule package found on a search path declares [[probe]] (sneaky); "
            "only registered Python plugins (entry point group buildlog.plugins) may run probes"
        )
    ]


def test_pure_rule_package_declaring_provider_rejected(tmp_path):
    """package providers are plugin code too."""
    errors = _rejected(tmp_path, "pure-provider")
    assert errors == [
        (
            "pure-provider: rule package found on a search path declares [[provider]] (sneaky); "
            "only registered Python plugins (entry point group buildlog.plugins) may provide "
            "package discovery"
        )
    ]


def test_private_and_export_only_names_rejected(tmp_path):
    errors = _rejected(tmp_path, "private-read")
    unreadable = "(not exported or open)"
    assert sorted(errors) == sorted(
        [
            (
                "private-read: extends tc_for, which is not an open relation of a lower layer "
                "(owned by buildlog, not in its open registry)"
            ),
            f"private-read: type Settings (in private_read_cfg) is private to buildlog {unreadable}",
            f"private-read: body relation nkeys is private to buildlog {unreadable}",
            f"private-read: body relation tc_candidate is private to buildlog {unreadable}",
            f"private-read: body relation tc_select is private to buildlog {unreadable}",
            f"private-read: body relation cxx_object is private to buildlog-cpp {unreadable}",
            f"private-read: body relation buildlog__nkeys is private to buildlog {unreadable}",
            (
                "private-read: clause head domain_cc_capability is owned by buildlog-cpp; "
                "not declared in extends"
            ),
        ]
    )


def test_reading_an_unrequired_package_rejected(tmp_path):
    errors = _rejected(tmp_path, "buildlog-cpp", "unrequired-read")
    assert errors == [
        "unrequired-read: reads domain_cc_capability from buildlog-cpp, which it does not require"
    ]


def test_namespace_prefix_violation_rejected(tmp_path):
    errors = _rejected(tmp_path, "bad-namespace")
    assert sorted(errors) == [
        "bad-namespace: accepts item, outside its namespace (must start with bns_)",
        "bad-namespace: declares helper, outside its namespace (must start with bns_)",
        "bad-namespace: declares item, outside its namespace (must start with bns_)",
    ]


def test_requires_version_mismatch_rejected(tmp_path):
    errors = _rejected(tmp_path, "requires-skew")
    assert errors == [
        "requires-skew: requires buildlog-cpp >=2.0, but buildlog-cpp 0.1.0 is loaded"
    ]


def test_plugin_manifest_version_must_match_distribution(tmp_path):
    errors = _rejected(tmp_path, "test-version-skew")
    assert errors == [
        (
            "test-version-skew: manifest version 9.9.9 differs from its distribution "
            "buildlog-testplugins 0.1.0"
        )
    ]


def test_open_registries_are_exact():
    core = layers.parse_manifest(CORE)
    cpp = layers.parse_manifest(CPP)
    halide = layers.parse_manifest(HALIDE)
    blurgen = layers.parse_manifest(BLURGEN)
    for data in (core, cpp):
        assert len(set(data["open"])) == len(data["open"])
        assert (set(data["open"]) & set(data["exports"])) <= {"request"}
    open_rels = set(core["open"]) | set(cpp["open"])
    assert set(halide["extends"]) | set(blurgen["extends"]) | set(cpp["extends"]) <= open_rels
    assert set(cpp["extends"]) <= set(core["open"])
    assert {"action_depfile", "default_output"} <= set(core["open"])
    assert {"linker_group_delims", "tc_setting_arg", "native_kind", "toolchain"} <= set(
        core["open"]
    )
    assert {"cxx_source", "family_pic_arg"} <= set(cpp["open"])
    # Plugins own their probe domains; higher layers may add requests.
    assert "req_cc_capability" in cpp["open"]
    assert "domain_cc_capability" in cpp["exports"]


def _actions(facts):
    """Build actions, without core's regeneration (generator) action."""
    generators = set((facts / "action_generator.csv").read_text().split())
    rows = (line.split("\t") for line in (facts / "action.csv").read_text().splitlines())
    return sorted(tuple(row) for row in rows if row[0] not in generators)


def test_colliding_private_helpers_compose_through_configure(tmp_path):
    facts, metrics = configure_project(COLLISION, tmp_path / "build")
    assert metrics["errors"] == []
    assert _actions(facts) == [("out:gen/alpha/logo.cc", "ALPHA"), ("out:gen/beta/api.cc", "BETA")]
    ninja = (tmp_path / "build" / "souffle" / "build.ninja").read_text()
    assert "cmd = alpha-gen --name=logo" in ninja
    assert "cmd = beta-gen --name=api" in ninja
    assert "alpha-gen --name=api" not in ninja and "beta-gen --name=logo" not in ninja
    registry = json.loads((tmp_path / "build" / "program" / "registry.json").read_text())
    assert registry["layers"]["alpha"]["qualified"] == {"gen_item": "alpha__gen_item"}
    assert registry["layers"]["beta"]["qualified"] == {"gen_item": "beta__gen_item"}
    assert registry["layers"]["buildlog"]["qualified"]["nkeys"] == "buildlog__nkeys"


def test_colliding_helpers_without_qualification_error(tmp_path, monkeypatch):
    monkeypatch.setattr(layers, "qualify_names", lambda text, package, names: text)
    with pytest.raises(LoaderError) as exc:
        configure_project(COLLISION, tmp_path / "build", emit=False)
    # Unqualified, beta's helper clashes with alpha's and its rules would add
    # clauses to, and read, alpha's gen_item (a silent merge).
    assert sorted(exc.value.errors) == [
        "beta: body relation gen_item is private to alpha (not exported or open)",
        "beta: clause head gen_item is owned by alpha; not declared in extends",
        "beta: redeclares relation gen_item owned by alpha",
    ]


def test_colliding_helpers_unqualified_program_is_wrong(tmp_path):
    """The same composed program with the raw package sources: Soufflé either
    rejects the redeclaration or, with one shared declaration (a
    silent merge), each package emits the other's actions."""
    build = tmp_path / "build"
    configure_project(COLLISION, build, emit=False)
    program = build / "program"
    facts_in = build / "facts-in" / _final_round(build)
    raw = {n: (COLLISION / "packages" / n / f"{n}.dl").read_text() for n in ("alpha", "beta")}

    def run(beta_text):
        (program / "alpha" / "alpha.dl").write_text(raw["alpha"])
        (program / "beta" / "beta.dl").write_text(beta_text)
        out = tmp_path / "out"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
        cp = subprocess.run(
            [
                str(souffle_path()),
                "-w",
                "-F",
                str(facts_in),
                "-D",
                str(out),
                str(program / "build.dl"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        return cp, out

    cp, _ = run(raw["beta"])
    assert cp.returncode != 0
    assert "Redefinition of relation gen_item" in cp.stdout + cp.stderr

    shared = "\n".join(line for line in raw["beta"].splitlines() if ".decl gen_item" not in line)
    cp, out = run(shared)
    assert cp.returncode == 0, cp.stderr
    # Every output now has two producers' labels and both generators' argv.
    assert _actions(out) == [
        ("out:gen/alpha/logo.cc", "ALPHA"),
        ("out:gen/alpha/logo.cc", "BETA"),
        ("out:gen/beta/api.cc", "ALPHA"),
        ("out:gen/beta/api.cc", "BETA"),
    ]


@host.cross
@pytest.mark.integration
def test_blurgen_requests_cc_capability_and_reports_no(tmp_path):
    project = ROOT / "examples" / "cross"
    assert layers.parse_manifest(BLURGEN)["probe"] == []
    assert not list(BLURGEN.parent.glob("*.py"))
    build = tmp_path / "build"
    facts, metrics = configure_project(project, build, emit=False)
    rounds = metrics["rounds"]
    assert "blurgen_host" not in rounds[0]
    requests = max(r["cc_capability"]["requests"] for r in rounds)
    assert requests > 0
    reqs = {
        tuple(r.split("\t")) for r in (facts / "req_cc_capability.csv").read_text().splitlines()
    }
    # blurgen asks about C on its generator's host (the exec platform).
    hosts = {r for r in reqs if r[2] == "c" and r[3] == "arm64-macos"}
    assert hosts, reqs
    results = (build / "env-facts" / "res_cc_capability.facts").read_text().splitlines()
    assert {tuple(r.split("\t")[:4]) for r in results} >= hosts
    assert metrics["executed"] >= requests

    _, warm = configure_project(project, build, emit=False)
    assert warm["executed"] == 0
    assert max(r["cc_capability"]["requests"] for r in warm["rounds"]) == requests

    # If the probe says "no" for a generator host, blurgen reports it.
    facts_in = build / "facts-in" / f"round-{warm['final_round']}"
    rows = [r.split("\t") for r in (facts_in / "res_cc_capability.facts").read_text().splitlines()]
    forged = ["\t".join([*r[:4], "no"] if tuple(r[:4]) in hosts else r) for r in rows]
    (facts_in / "res_cc_capability.facts").write_text("\n".join(forged) + "\n")
    out = tmp_path / "forged"
    out.mkdir()
    subprocess.run(
        [
            str(souffle_path()),
            "-w",
            "-F",
            str(facts_in),
            "-D",
            str(out),
            str(build / "program" / "build.dl"),
        ],
        check=True,
    )
    errors = (out / "error.csv").read_text()
    assert "blurgen host compiler cannot compile C\tapple-clang on arm64-macos" in errors


def test_project_packages_find_collision_packages():
    packages = project_packages(COLLISION)
    assert [(p.name, p.candidate.source, p.reason) for p in packages] == [
        ("buildlog", "core", "root"),
        ("alpha", "path", "uses"),
        ("beta", "path", "uses"),
    ]


def test_spec_cannot_supply_probe_results(tmp_path):
    project = make_project(tmp_path, ["test-runaway"], 'res_runaway = [["runaway:0", "forged"]]\n')
    with pytest.raises(LoaderError, match="relation res_runaway is accepted by no loaded package"):
        configure_project(project, tmp_path / "build", emit=False)
    assert not (tmp_path / "build" / "rounds").exists()


@pytest.mark.integration
def test_dependency_without_definition_is_an_error(tmp_path):
    """Core L1: a needed dependency name that no loaded package defines."""
    spec = """
target = [["app"]]
dep = [["app", "libfoo", "public"]]
request = [["app", "native"], ["ghost", "native"]]
config_decl = [["native", "arm64-macos"]]
toolchain_pin = [["c", "arm64-macos", "arm64-macos", "apple-clang"], ["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]
"""
    project = make_project(tmp_path, ["buildlog-cpp"], spec)
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    metrics = json.loads((tmp_path / "build" / "metrics.json").read_text())
    assert sorted(tuple(e) for e in metrics["errors"]) == [
        ("unknown target", "ghost"),
        ("unresolved dependency", "dependency libfoo of app has no definition"),
    ]


def test_target_derived_from_needed_closure_rejected(tmp_path):
    """Available declarations cannot depend on the demand they determine."""
    errors = _rejected(tmp_path, "target-from-needed")
    assert any(
        error.startswith("declaration/selection stratum depends on demand or condition truth:")
        and "target" in error
        and "needed" in error
        for error in errors
    ), errors


@pytest.mark.parametrize("serve", ["0", "1"])
def test_stratification_cycle_names_the_project_rule(tmp_path, monkeypatch, serve):
    monkeypatch.setenv("BUILDLOG_SOUFFLE_SERVE", serve)
    project = make_project(
        tmp_path,
        ["buildlog-cpp"],
        'config_decl = [["rel", "arm64-macos"]]\n',
    )
    toml = project / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "rules.dl"]')
    )
    (project / "rules.dl").write_text(
        ".decl extra(t:symbol)\n"
        "extra(t) :- needed(t, _), !imported(t).\n"
        "request_all(t) :- extra(t).\n"
        ".decl lonely(t:symbol)\n"
        "lonely(t) :- target(t), !extra(t).\n"
        'dep(t, "zlib", "private") :- lonely(t).\n'
    )
    with pytest.raises(LoaderError) as exc:
        configure_project(project, tmp_path / "build", emit=False)
    (error,) = exc.value.errors
    assert "runs through dep, extra, lonely, request_all:" in error
    assert "rules.dl:5: lonely reads !extra" in error
    assert "rules.dl:2: extra reads needed" in error
    assert "rules.dl:3: request_all reads extra" in error
    assert "request_when, dep_when or prop_when" in error


def test_project_layer_can_derive_open_relations(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(
        'uses = ["buildlog-cpp"]\nspec = ["spec.toml", "rules.dl"]\n'
        '[author_decides]\ntoolchain_pin = "test fixture pins Apple clang"\n'
    )
    (project / "spec.toml").write_text(
        """
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
toolchain_pin = [["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]
project_exe = ["app"]
"""
    )
    (project / "rules.dl").write_text(
        """
.decl project_exe(t:symbol)
target(t), native_kind(t, "executable"), request(t, "rel") :- project_exe(t).
"""
    )
    facts, _ = configure_project(project, tmp_path / "build", emit=False)
    assert read_rows(
        tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build") / "project_exe.facts"
    ) == [("app",)]
    assert not (facts / "error.csv").read_text().strip()


def test_request_reading_a_probe_result_rejected(tmp_path):
    """(W5): a decision relation may not reach a probe result."""
    (error,) = _rejected(tmp_path, "buildlog-cpp", "request-reads-result")
    assert error.startswith("request depends on the probe result res_cc_capability")
    assert (
        "request <- res_cc_capability (tests/fixtures/rejected/share/buildlog/"
        "request-reads-result/request_reads_result.dl:1)"
    ) in error


def test_request_reading_needed_rejected_with_path(tmp_path):
    errors = _rejected(tmp_path, "buildlog-cpp", "request-reads-needed")
    (error,) = [e for e in errors if e.startswith("request depends on the probe result")]
    assert (
        "request <- needed (tests/fixtures/rejected/share/buildlog/request-reads-needed/" in error
    )
    assert "request_reads_needed.dl:1) <- " in error
    assert error.count(" <- ") >= 2


def test_shipped_program_keeps_decision_relations_answer_free(tmp_path):
    """(W5) holds for core and buildlog-cpp (the other packages and case studies are
    covered by the suites that load them)."""
    project = make_project(tmp_path, ["buildlog-cpp"])
    configure_project(project, tmp_path / "build", emit=False)
