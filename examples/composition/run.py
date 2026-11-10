# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Install independent generating libraries and validate their shared consumer."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from unittest.mock import patch

from buildlog import rule_index
from buildlog.engine import configure_project
from buildlog.errors import LoaderError
from buildlog.facts import read_rows

HERE = Path(__file__).resolve().parent
PACKAGES = ("constgen", "offsetgen", "unusedgen")


def run(work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=work)).resolve()
    cpu = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine(), platform.machine())
    system = {"Darwin": "macos", "Linux": "linux"}[platform.system()]
    host = f"{cpu}-{system}"
    family = (
        "apple-clang"
        if system == "macos"
        else os.environ.get("BUILDLOG_LINUX_TOOLCHAINS", "gcc").split(",")[0]
    )
    pins = [[language, host, host, family] for language in ("c", "cxx", "native-link")]
    report = {"platform": host, "work": str(root), "configurations": {}}
    for name in (*PACKAGES, "consumer"):
        shutil.copytree(HERE / name, root / name, ignore=shutil.ignore_patterns("buildlog.lock"))

    def configure(name: str, project: Path, prefixes: list[Path], install: Path | None = None):
        user = root / f"{name}.toml"
        text = f"toolchain_pin = {json.dumps(pins)}\n"
        text += (
            "package_search_dir = "
            + json.dumps([["buildlog", str(prefix / "share/buildlog")] for prefix in prefixes])
            + "\n"
        )
        if install:
            text += f"install_prefix = {json.dumps(str(install))}\n"
        user.write_text(text)
        preparation = 0.0
        real_prepare = rule_index.prepare_cached

        def timed_prepare(*args, **kwargs):
            nonlocal preparation
            start = time.perf_counter()
            try:
                return real_prepare(*args, **kwargs)
            finally:
                preparation += time.perf_counter() - start

        start = time.perf_counter()
        build = root / f"build-{name}"
        with patch.object(rule_index, "prepare_cached", timed_prepare):
            _, metrics = configure_project(
                project,
                build,
                user_files=[user],
                var_overrides={"EXAMPLE_PLATFORM": host},
            )
        assert not metrics["errors"], metrics["errors"]
        report["configurations"][name] = {
            "wall_s": time.perf_counter() - start,
            "preparation_s": preparation,
            "composition_calls": metrics["composition_calls"],
            "evaluator_startups": metrics["evaluator_startups"],
            "available_bundles": metrics["available_bundles"],
            "project": str(project),
            "user": str(user),
            "build": str(build),
        }
        return build

    def ninja(build: Path, *targets: str):
        subprocess.run(["ninja", "-C", str(build / "souffle"), *targets], check=True)

    prefixes = [root / f"prefix-{name}" for name in PACKAGES]
    for name, prefix in zip(PACKAGES, prefixes, strict=True):
        build = configure(name, root / name, [], install=prefix)
        ninja(build, "install")
        assert (prefix / "share/buildlog" / name / "manifest.toml").is_file()

    project = root / "consumer"
    baseline = configure("baseline", project, prefixes[:2])
    ninja(baseline)
    combined = configure("combined", project, prefixes)
    ninja(combined)
    executable = combined / "souffle/bin" / f"{host}.release.none/app"
    output = subprocess.check_output([str(executable)], text=True).strip()
    assert output == "42", output

    def rows(build: Path, relation: str):
        return set(read_rows(build / "souffle/facts" / f"{relation}.csv"))

    for relation in ("action", "action_arg", "action_in", "target_action", "needed"):
        before = {row for row in rows(baseline, relation) if "out:build.ninja" not in row}
        after = {row for row in rows(combined, relation) if "out:build.ninja" not in row}
        assert before == after, relation
    labels = {label for _, label in rows(combined, "action")}
    assert {"CONSTGEN", "OFFSETGEN"} <= labels, labels
    assert not any("unusedgen" in target for target, _ in rows(combined, "needed"))
    assert not any(kind == "unusedgen" for kind, _ in rows(combined, "warning"))
    registry = json.loads((combined / "program/registry.json").read_text())
    assert set(PACKAGES) <= registry["layers"].keys()
    lock = tomllib.loads((project / "buildlog.lock").read_text())
    active = {entry["name"] for entry in lock["package"]}
    assert set(PACKAGES[:2]) <= active and "unusedgen" not in active, active
    report.update(
        output=output,
        active_lock_packages=sorted(active),
        dormant_effects_absent=True,
        regeneration_inputs_excluded=True,
    )
    enabled_project = root / "consumer-enabled"
    shutil.copytree(HERE / "consumer", enabled_project)
    spec = enabled_project / "spec.toml"
    original = spec.read_text()
    spec.write_text(
        original.replace('request_all = [["app"]]', 'request_all = [["app"], ["unusedgen:rt"]]')
    )
    enabled = configure("enabled", enabled_project, prefixes)
    assert ("unusedgen", "package activated") in rows(enabled, "warning")
    assert any(target == "unusedgen:rt" for target, _ in rows(enabled, "needed"))
    enabled_lock = tomllib.loads((enabled_project / "buildlog.lock").read_text())
    assert "unusedgen" in {entry["name"] for entry in enabled_lock["package"]}
    report["explicit_activation_checked"] = True

    collision = root / "collision"
    shutil.copytree(prefixes[0], collision)
    rules = collision / "share/buildlog/constgen/generator.dl"
    rules.write_text(rules.read_text() + "\n.decl constgen_different_installation()\n")
    invalid = root / "invalid"
    shutil.copytree(prefixes[2], invalid)
    (invalid / "share/buildlog/unusedgen/unused.dl").write_text(
        'warning(42, "invalid dormant code").\n'
    )
    for name, roots, expected in (
        ("conflict", [*prefixes, collision], ("constgen: found twice",)),
        (
            "invalid-dormant",
            [*prefixes[:2], invalid],
            ("ambiguous constant", "unable to deduce type"),
        ),
    ):
        negative_project = root / f"consumer-{name}"
        shutil.copytree(HERE / "consumer", negative_project)
        try:
            configure(name, negative_project, roots)
        except LoaderError as error:
            diagnostic = str(error)
            assert all(part in diagnostic.lower() for part in expected), diagnostic
            report[name] = diagnostic
        else:
            raise AssertionError(f"{name} unexpectedly accepted")
    report["single_composition"] = all(
        row["composition_calls"] == 1 for row in report["configurations"].values()
    )
    assert report["single_composition"]
    assert all(row["evaluator_startups"] == 1 for row in report["configurations"].values())
    (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=Path("build/composition"))
    args = parser.parse_args()
    print(json.dumps(run(args.work), indent=2))


if __name__ == "__main__":
    main()
