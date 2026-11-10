# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property tests of two core invariants on random small projects.

(a) Toolchain uniqueness, no silent choice (paper Lemma 4.3): for each
    (language, exec platform, target platform) a build uses, either exactly
    one toolchain is selected (the pin, or the only candidate), or an error is
    derived ("ambiguous toolchain", "no toolchain" for every target with an
    action there, a pin matching nothing). Never the first of several.
(b) Tool configuration from use (paper Prop 4.10): every program an action
    runs or loads is needed in the exec configuration of the action's
    configuration, and a target is needed in a configuration only if it is
    requested there, a dependency of something needed there, or run/loaded by
    an action whose configuration's exec configuration it is. The oracle is
    an independent least fixpoint over the generated spec, compared with the
    derived `needed` relation (both directions).

No hypothesis dependency: cases come from random.Random with a fixed seed, so
a failure is reproduced by its case index (the test id). Projects use the toy
language of tests/fixtures/defined (no compiler, no probe): the toolchains
are generated facts of a per-case rule package.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
DEFINED = ROOT / "tests" / "fixtures" / "defined"
EXEC_DEMO = DEFINED / "share" / "buildlog" / "exec-demo"

SEED = 20261012
CASES_A = 130
CASES_B = 110

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


# ---- generation ---------------------------------------------------------------

PLATFORMS = ("toy", "toy-a")  # toy is the host
CONFIGS = {  # name -> (platform, settings line or None)
    "cross": "toy-a",
    "native": "toy",
}


class Case:
    def __init__(self, rng: random.Random, *, vary_toolchains: bool):
        self.toolchains: list[tuple[str, set[str], set[str]]] = []
        if vary_toolchains:
            for i in range(rng.choice((0, 1, 2, 3))):
                runs = {"toy"} if rng.random() < 0.85 else {"toy-a"}
                can = {p for p in PLATFORMS if rng.random() < 0.6} or {rng.choice(PLATFORMS)}
                self.toolchains.append((f"tc{i}", runs, can))
        else:  # exactly one candidate everywhere
            self.toolchains.append(("tc0", {"toy"}, set(PLATFORMS)))
        names = [t[0] for t in self.toolchains]
        # pins[target platform] = list of toolchain names (several = a double pin)
        self.pins: dict[str, list[str]] = {}
        if vary_toolchains:
            for p in PLATFORMS:
                r = rng.random()
                if r < 0.35 and names:
                    self.pins[p] = [rng.choice(names)]
                elif r < 0.45 and len(names) > 1:
                    self.pins[p] = rng.sample(names, 2)
                elif r < 0.55:
                    self.pins[p] = ["ghost"]
        n = rng.randint(2, 6)
        self.targets: list[tuple[str, str]] = []  # (name, kind) kind: tool | gen
        self.run: dict[str, str] = {}
        self.load: dict[str, str] = {}
        self.deps: dict[str, list[str]] = {}
        for i in range(n):
            name = f"t{i}"
            kind = "tool" if i == 0 or rng.random() < 0.45 else "gen"
            self.targets.append((name, kind))
            earlier = [t for t, _ in self.targets[:i]]
            if kind == "gen" and earlier:  # a tool that runs a tool, and may load one
                self.run[name] = rng.choice(earlier)
                if rng.random() < 0.4:
                    self.load[name] = rng.choice(earlier)
            elif kind == "gen":
                self.targets[-1] = (name, "tool")
            self.deps[name] = [e for e in earlier if rng.random() < 0.3]  # also used as a library
        names_t = [t for t, _ in self.targets]
        self.requests = [
            (rng.choice(names_t), rng.choice(sorted(CONFIGS))) for _ in range(rng.randint(1, 3))
        ]
        self.requests = sorted(set(self.requests))

    def kind(self, t):
        return dict(self.targets)[t]

    def spec(self) -> str:
        q = json.dumps
        lines = [
            "target = " + q([[t] for t, _ in self.targets]),
            "execdemo_tool = " + q([[t, f"{t}.txt"] for t, k in self.targets if k == "tool"]),
            "execdemo_gen = "
            + q([[t, self.run[t], f"sym_{t}", "input.txt"] for t, k in self.targets if k == "gen"]),
            "execdemo_plugin = " + q([[t, p] for t, p in self.load.items()]),
            "dep = " + q([[t, d, "private"] for t, ds in self.deps.items() for d in ds]),
            'config_decl = [["cross", "toy-a"], ["native", "toy"]]',
            'config_decl_setting = [["native", "native.opt", "release"]]',
            "request = " + q([list(r) for r in self.requests]),
        ]
        pins = [["toy", "toy", p, tc] for p, tcs in self.pins.items() for tc in tcs]
        if pins:
            lines.append("toolchain_pin = " + q(pins))
        return "\n".join(lines) + "\n"

    def package_dl(self) -> str:
        keep = [
            ln
            for ln in (EXEC_DEMO / "exec_demo.dl").read_text().splitlines()
            if not ln.startswith("toolchain")
        ]
        out = []
        for name, runs, can in self.toolchains:
            out.append(f'toolchain("{name}", "toy", "1").')
            out += [f'toolchain_runs_on("{name}", "{p}").' for p in sorted(runs)]
            out += [f'toolchain_can_target("{name}", "toy", "{p}").' for p in sorted(can)]
            out.append(f'toolchain_tool("{name}", "cc", "cc-{name}").')
        # a generated target's output is a program too (a tool that runs a tool)
        out.append("run_artifact(t, c, o) :- execdemo_out(t, c, o), execdemo_gen(t, _, _, _).")
        return "\n".join(keep + out) + "\n"


def configure_case(case: Case, tmp_path: Path):
    proj = tmp_path / "project"
    pkg = proj / "packages" / "prop-toy"
    pkg.mkdir(parents=True)
    (pkg / "manifest.toml").write_text(
        (EXEC_DEMO / "manifest.toml").read_text().replace('name = "exec-demo"', 'name = "prop-toy"')
    )
    (pkg / "exec_demo.dl").write_text(case.package_dl())
    (proj / "buildlog.toml").write_text(
        'uses = ["toy-platform", "prop-toy"]\nspec = ["spec.toml"]\n'
        '[author_decides]\ntoolchain_pin = "property test"\n'
    )
    (proj / "spec.toml").write_text(case.spec())
    (proj / "input.txt").write_text("x\n")
    for t, k in case.targets:
        (proj / f"{t}.txt").write_text(t)
    build = tmp_path / "build"
    try:
        configure_project(proj, build, package_paths=[DEFINED], update_lock=True)
    except SystemExit:
        pass
    facts = build / "souffle" / "facts"
    metrics = json.loads((build / "metrics.json").read_text())
    return build, facts, metrics


def rows(facts: Path, rel: str) -> set[tuple[str, ...]]:
    path = facts / f"{rel}.csv"
    if not path.exists():
        return set()
    return {tuple(ln.split("\t")) for ln in path.read_text().splitlines() if ln}


def cfg_names(facts: Path) -> dict[str, str]:
    return {c: n for c, n in rows(facts, "cfg_name")}


def cfg_platform(c: str) -> str:
    return c[1:].split(",", 1)[0]


# ---- (a) toolchain uniqueness ---------------------------------------------------


def needed_pairs(facts, names):
    return {(t, names.get(c, c)) for t, c in rows(facts, "needed")}


def expected_needed(case: Case, facts: Path) -> set[tuple[str, str]]:
    """Least fixpoint of the needed closure, from the spec and the exec map."""
    names = cfg_names(facts)
    exec_of = {
        names[c]: names[e] for c, e in rows(facts, "exec_config") if c in names and e in names
    }
    cfg_by_name = {"cross": "toy-a.debug.none", "native": "toy.release.none"}
    need = {(t, cfg_by_name[c]) for t, c in case.requests}
    while True:
        new = set(need)
        for t, c in need:
            new |= {(d, c) for d in case.deps.get(t, [])}
            for g in (case.run.get(t), case.load.get(t)):
                if g:
                    new.add((g, exec_of[c]))
        if new == need:
            return need
        need = new


@pytest.mark.parametrize("i", range(CASES_A))
def test_toolchain_selection_is_unique_or_an_error(i, tmp_path):
    rng = random.Random(f"{SEED}:a:{i}")
    case = Case(rng, vary_toolchains=True)
    _, facts, metrics = configure_case(case, tmp_path)
    names = cfg_names(facts)
    errors = [tuple(e) for e in metrics["errors"]]
    need = expected_needed(case, facts)
    assert need == needed_pairs(facts, names), case.spec()
    selected: dict[str, set[str]] = {}
    for lang, e, p, tc in rows(facts, "tc_select"):
        if e != "toy":  # toolchains that run elsewhere are not candidates of the host exec
            continue
        selected.setdefault(p, set()).add(tc)

    failed = {(a, k) for a, k, _ in rows(facts, "action_failed")}
    plat = {"toy-a.debug.none": "toy-a", "toy.release.none": "toy"}
    used = {  # target platform -> configurations where the toy language is required
        p: [
            c
            for c in {c for _, c in need if plat[c] == p}
            if any(case.kind(t) == "tool" for t, c2 in need if c2 == c)
        ]
        for p in PLATFORMS
    }
    for p, cfgs in used.items():
        if not cfgs:
            continue
        cands = {tc for tc, runs, can in case.toolchains if "toy" in runs and p in can}
        pins = case.pins.get(p, [])
        chosen = selected.get(p, set())
        ctx = f"platform {p} candidates {sorted(cands)} pins {pins}\n{case.spec()}"
        if pins:
            expect = {tc for tc in pins if tc in cands}
        else:
            expect = cands
        assert chosen == expect, ctx
        if len(chosen) == 1 and len(pins) <= 1:
            (tc,) = chosen
            assert not [
                e
                for e in errors
                if (e[0].startswith("ambiguous toolchain") and f" for {p}:" in e[1])
                or (e[0] == "no toolchain" and e[1].split("config ")[-1] in cfgs)
            ], ctx
            # the program is that toolchain's tool, in every tool target built there
            ninja = (facts.parent / "build.ninja").read_text() if not errors else ""
            for c in cfgs:
                for t, c2 in need:
                    if c2 == c and case.kind(t) == "tool":
                        assert not ninja or (f"cmd = cc-{tc} " in ninja and f"{c}/{t}" in ninja), (
                            ctx
                        )
                        assert (t, tc) in {
                            (a, b) for a, _l, cc, b in rows(facts, "tc_of") if names.get(cc) == c
                        }, ctx
        elif len(chosen) >= 2:
            assert any(
                (k.startswith("ambiguous toolchain") and f" for {p}:" in d)
                or (k == "toolchain pinned twice" and f" for {p}:" in d)
                for k, d in errors
            ), ctx
        elif (
            len(chosen) == 0
        ):  # no candidate: every target with an action in the configuration is an error
            assert not pins or any(
                k == "toolchain pin matches no candidate" and f" for {p}:" in d for k, d in errors
            ), ctx
            for c in cfgs:
                for t, c2 in need:
                    if c2 == c:
                        assert (f"out:{c}/{t}", "no toolchain") in failed, (t, c, errors, ctx)
        if len(pins) == 2 and all(tc in cands for tc in pins):
            assert any(
                k in ("toolchain pinned twice",) or k.startswith("ambiguous toolchain")
                for k, d in errors
                if f" for {p}:" in d
            ), ctx
        if pins == ["ghost"]:
            assert any(
                k == "toolchain pin matches no candidate" and f" for {p}:" in d for k, d in errors
            ), ctx


# ---- (b) tool configuration from use --------------------------------------------


@pytest.mark.parametrize("i", range(CASES_B))
def test_tool_configuration_follows_use_in_both_directions(i, tmp_path):
    rng = random.Random(f"{SEED}:b:{i}")
    case = Case(rng, vary_toolchains=False)
    _, facts, metrics = configure_case(case, tmp_path)
    assert metrics["errors"] == [], case.spec()
    names = cfg_names(facts)
    got = needed_pairs(facts, names)
    need = expected_needed(case, facts)
    assert got == need, (
        f"derived {sorted(got - need)} extra, {sorted(need - got)} missing\n{case.spec()}"
    )

    # direction 1: every program an action runs or loads is needed in its exec configuration
    exec_of = {
        names[c]: names[e] for c, e in rows(facts, "exec_config") if c in names and e in names
    }
    ninja = (facts.parent / "build.ninja").read_text()
    for t, c in need:
        for g in (case.run.get(t), case.load.get(t)):
            if g:
                assert (g, exec_of[c]) in got
                assert f"{exec_of[c]}/{g}" in ninja
    # direction 2: nothing is built that no request, dependency or action's use asks for
    deps_used = {(d, c) for t, c in need for d in case.deps.get(t, [])}
    requested = {
        (t, {"cross": "toy-a.debug.none", "native": "toy.release.none"}[c])
        for t, c in case.requests
    }
    used_as_tool = {
        (g, exec_of[c]) for t, c in need for g in (case.run.get(t), case.load.get(t)) if g
    }
    assert got <= requested | deps_used | used_as_tool, case.spec()
    # the derived tool_dep rows are exactly the runs and loads of needed targets
    td = {(t, names[c], g) for t, c, g in rows(facts, "tool_dep") if c in names}
    assert td == {(t, c, g) for t, c in need for g in (case.run.get(t), case.load.get(t)) if g}


def test_generator_covers_the_space():
    """The fixed seed exercises 0..3 toolchains, pins, tool chains and tool-as-library."""
    cases_a = [Case(random.Random(f"{SEED}:a:{i}"), vary_toolchains=True) for i in range(CASES_A)]
    assert {len(c.toolchains) for c in cases_a} == {0, 1, 2, 3}
    assert any(c.pins for c in cases_a)
    cases_b = [Case(random.Random(f"{SEED}:b:{i}"), vary_toolchains=False) for i in range(CASES_B)]
    assert any(len({*c.run.values()} & {t for t, k in c.targets if k == "gen"}) for c in cases_b)
    # a target that is both run as a tool and depended on as a library
    assert any(
        {g for g in c.run.values()} & {d for ds in c.deps.values() for d in ds} for c in cases_b
    )
