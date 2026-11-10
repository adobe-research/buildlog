# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import shutil
import subprocess
import tomllib

import pytest

from buildlog.paths import souffle_path

from . import host
from .halide_env import HALIDE_SRC, SKIP_REASON, halide_available, halide_define

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def zlib_pc_dir():
    try:
        cp = subprocess.run(
            ["pkg-config", "--variable=pcfiledir", "zlib"],
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    path = cp.stdout.strip()
    return path or None


def assert_actions_owned(build):
    """Partial emission is only as fine as attribution: every action but the
    regeneration edge must be declared by some layer as built for a (target,
    configuration)."""
    facts = build / "souffle" / "facts"

    def column(rel, i):
        return {row.split("\t")[i] for row in (facts / f"{rel}.csv").read_text().splitlines()}

    actions = column("action", 0) - column("action_generator", 0)
    owned = column("target_action", 2)
    assert actions and actions <= owned, sorted(actions - owned)


@pytest.mark.integration
@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_hello_example_builds_and_runs(tmp_path):
    pc_dir = zlib_pc_dir()
    if pc_dir is None:
        pytest.skip("pkg-config zlib not found")
    project = ROOT / "examples" / "hello"
    build = tmp_path / "hello-build"
    # The preset names Homebrew's zlib; this machine's user file names the one
    # pkg-config finds (a user decision, stated in a user file).
    preset = (project / "presets" / "homebrew-arm64.toml").read_text()
    user = tmp_path / "user.toml"
    user.write_text(preset.replace("/opt/homebrew/opt/zlib/lib/pkgconfig", str(pc_dir)))
    subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "configure",
            "--user",
            str(user),
            "-C",
            str(build),
            str(project),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True)
    out = subprocess.check_output(
        [str(build / "souffle" / "bin" / "arm64-macos.release.none" / "hello")],
        text=True,
    )
    assert out.startswith("hello 42 ")


@host.cross
@pytest.mark.integration
@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_cross_configure_cache_shuffle_and_run(tmp_path):
    build = tmp_path / "cross-build"
    project = ROOT / "examples" / "cross"
    user = ["--user", str(project / "presets" / "apple-clang.toml")]
    cmd = ["uv", "run", "buildlog", "configure", *user, "-C", str(build), str(project)]
    subprocess.run(cmd, cwd=ROOT, check=True)
    first = (build / "souffle" / "build.ninja").read_bytes()
    assert_actions_owned(build)
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True)
    outputs = []
    for exe in sorted((build / "souffle" / "bin").glob("*/*")):
        if exe.name != "app":
            continue
        archs = subprocess.check_output(["lipo", "-archs", str(exe)], text=True).split()
        for arch in archs:
            outputs.append(subprocess.check_output(["arch", f"-{arch}", str(exe)], text=True))
    assert any("sum=45" in out and "zlib=" in out for out in outputs)

    subprocess.run(cmd, cwd=ROOT, check=True)
    assert (build / "souffle" / "build.ninja").read_bytes() == first
    assert json.loads((build / "metrics.json").read_text())["executed"] == 0

    shuffle = tmp_path / "cross-shuffle"
    subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *user,
            "--shuffle",
            "9",
            "--shuffle-probes",
            "4",
            "-C",
            str(shuffle),
            str(project),
        ],
        cwd=ROOT,
        check=True,
    )
    shuffled = (shuffle / "souffle" / "build.ninja").read_bytes()
    assert (
        shuffled.replace(
            str(shuffle / "available-certificate.json").encode(),
            str(build / "available-certificate.json").encode(),
        )
        == first
    )


@pytest.mark.integration
@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_why_contains_leaf_and_origin(cross_build):
    project = ROOT / "examples" / "cross"
    build = cross_build
    cp = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "why",
            "-C",
            str(build),
            'action("out:bin/universal2-macos.release.none/app", "LIPO")',
            str(project),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    spec = (project / "spec.toml").read_text().splitlines()
    line = next(i for i, text in enumerate(spec, 1) if '["app", "u2-release"]' in text)
    assert f'request("app", "u2-release")  ← examples/cross/spec.toml:{line}' in cp.stdout


HALIDE_PROJECTS = ROOT / "tests" / "projects" / "halide"
# Halide is a pure rule package found on this search-path prefix.
PACKAGE_PATH = ["--package-path", str(ROOT / "tests" / "fixtures" / "rule-packages")]


def _configure(project, build, *extra):
    subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *PACKAGE_PATH,
            "--user",
            str(HALIDE_PROJECTS / "presets" / "homebrew-arm64.toml"),
            *halide_define(),
            *extra,
            "-C",
            str(build),
            str(project),
        ],
        cwd=ROOT,
        check=True,
    )


def _build_run_halide(tmp_path, name, exe, args, image, expected, archive, reason):
    project = HALIDE_PROJECTS / name
    build = tmp_path / name
    _configure(project, build)
    assert_actions_owned(build)
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True)
    image_path = HALIDE_SRC / "apps" / "images" / image
    output = subprocess.check_output(
        [
            str(build / "souffle" / "bin" / "arm64-macos.release.none" / exe),
            *[a.replace("{image}", str(image_path)) for a in args],
        ],
        cwd=tmp_path,
        text=True,
    )
    assert expected in output
    lock = tomllib.loads((project / "buildlog.lock").read_text())
    (halide,) = [p for p in lock["package"] if p["name"] == "halide"]
    assert halide["reason"] == reason
    archs = subprocess.check_output(
        [
            "lipo",
            "-archs",
            str(build / "souffle" / "lib" / "universal2-macos.release.none" / archive),
        ],
        text=True,
    ).split()
    assert archs == ["x86_64", "arm64"]
    return build


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_bilateral_grid_native_and_universal(tmp_path):
    _build_run_halide(
        tmp_path,
        "bilateral_grid",
        "bilateral_grid_process",
        ["{image}", "out.png", "0.1", "10"],
        "gray.png",
        "Success!",
        "libbilateral_grid.a",
        "uses",
    )


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_linear_blur_native_and_universal(tmp_path):
    _build_run_halide(
        tmp_path,
        "linear_blur",
        "linear_blur_process",
        ["1", "{image}", "out.png"],
        "rgb.png",
        "Using linear blur",
        "liblinear_blur.a",
        # linear_blur does not list halide in `uses`: loaded by namespace.
        "namespace:halide_generator",
    )


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_coverage_errors_emit_partial_graph(tmp_path):
    """Coverage errors break only the x86_64 exe: configure exits 1 but emits
    a graph whose native exe builds and runs while the x86_64 edge fails."""
    build = tmp_path / "coverage"
    configure = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *PACKAGE_PATH,
            "--user",
            str(HALIDE_PROJECTS / "presets" / "homebrew-arm64.toml"),
            *halide_define(),
            "-C",
            str(build),
            str(HALIDE_PROJECTS / "bilateral_grid_coverage"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert configure.returncode != 0
    # Homebrew's dylibs are arm64-only (core artifact coverage, from core's
    # file_archs probe): the import's own error names the missing slice, and
    # its consumer's names the file it needs.
    # The files are the ones the .pc Libs name (-lpng16 -> libpng16.dylib).
    for lib, dylib in (("libpng", "libpng16.dylib"), ("libjpeg", "libjpeg.dylib")):
        assert (
            f"imported file lacks the platform's architecture: no x86_64 slice of pc:{lib}: "
        ) in configure.stderr
        assert f"/lib/{dylib} (x86_64-macos.release.none)" in configure.stderr
        assert (
            "artifact lacks platform coverage: bilateral_grid_process in x86_64-macos.release.none "
            f"needs /opt/homebrew/opt/{'libpng' if lib == 'libpng' else 'jpeg-turbo'}/lib/{dylib} on x86_64-macos"
        ) in configure.stderr
    metrics = json.loads((build / "metrics.json").read_text())
    assert metrics["global_errors"] == []
    assert not (build / "souffle" / "build.ninja").exists()
    keep_going = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *PACKAGE_PATH,
            "--user",
            str(HALIDE_PROJECTS / "presets" / "homebrew-arm64.toml"),
            *halide_define(),
            "--keep-going",
            "-C",
            str(build),
            str(HALIDE_PROJECTS / "bilateral_grid_coverage"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert keep_going.returncode == 0
    assert (build / "souffle" / "build.ninja").exists()


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_without_binding_source_is_an_error(tmp_path):
    """Halide names libpng/libjpeg/zlib; only the workspace's aliases and
    the importers' search directories bind them. Without them the optional
    libpng/libjpeg edges disappear and the required zlib must fail loudly."""
    source = HALIDE_PROJECTS / "bilateral_grid"
    project = tmp_path / "halide" / "bilateral_grid"
    project.mkdir(parents=True)
    workspace = (HALIDE_PROJECTS / "workspace.toml").read_text()
    assert "package_want = " in workspace
    (project.parent / "workspace.toml").write_text(workspace)
    for name in ("project.toml", "buildlog.toml"):
        shutil.copy(source / name, project / name)
    check = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *PACKAGE_PATH,
            *halide_define(),
            "-C",
            str(tmp_path / "build"),
            str(project),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert check.returncode != 0
    # libpng/libjpeg are optional (dep :- defined): their edges disappear.
    # zlib is a plain dep, so it is the only error.
    metrics = json.loads((tmp_path / "build" / "metrics.json").read_text())
    assert metrics["errors"] == [
        ["unresolved dependency", "dependency zlib of Halide::ImageIO has no definition"]
    ]
    needed = (tmp_path / "build" / "souffle" / "facts" / "needed.csv").read_text()
    assert "Halide::ImageIO\t" in needed
    assert "libpng" not in needed and "libjpeg" not in needed
    # The error is attributed to Halide::ImageIO, so what needs it fails in
    # ninja when --keep-going asks for a partial graph; by default configure
    # fails before writing build.ninja.
    assert metrics["global_errors"] == []
    assert not (tmp_path / "build" / "souffle" / "build.ninja").exists()
    keep = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "configure",
            *PACKAGE_PATH,
            "--keep-going",
            "-C",
            str(tmp_path / "build"),
            str(project),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert keep.returncode == 0
    ninja = (tmp_path / "build" / "souffle" / "build.ninja").read_text()
    assert "build bin/arm64-macos.release.none/bilateral_grid_process: fail\n" in ninja
    assert "build bin/arm64-macos.release.none/bilateral_grid.generator: run " in ninja


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_why_optional_edge_shows_defined(bilateral_grid_build):
    project = HALIDE_PROJECTS / "bilateral_grid"
    build = bilateral_grid_build
    cp = subprocess.run(
        [
            "uv",
            "run",
            "buildlog",
            "why",
            *PACKAGE_PATH,
            "--no-collapse",
            "-C",
            str(build),
            'dep("Halide::ImageIO", "libpng", "public")',
            str(project),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [line.strip() for line in cp.stdout.splitlines()]
    assert lines[0].startswith('dep("Halide::ImageIO", "libpng", "public")')
    assert lines[1].startswith('defined("libpng")')


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_warm_cache_and_shuffle(tmp_path):
    project = HALIDE_PROJECTS / "bilateral_grid"
    build = tmp_path / "warm"
    _configure(project, build)
    first = (build / "souffle" / "build.ninja").read_bytes()
    _configure(project, build)
    assert (build / "souffle" / "build.ninja").read_bytes() == first
    assert json.loads((build / "metrics.json").read_text())["executed"] == 0
    shuffle = tmp_path / "shuffle"
    _configure(project, shuffle, "--shuffle", "11", "--shuffle-probes", "7")
    assert (shuffle / "souffle" / "build.ninja").read_bytes() == first
