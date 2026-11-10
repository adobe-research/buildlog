# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from buildlog.facts import read_rows
from buildlog.layers import PackageRef, check_layers, compose_program, qualify_layer_text
from buildlog.paths import souffle_path


def write_layer(root, name, rank, text, **manifest):
    layer_dir = root / name
    layer_dir.mkdir()
    (layer_dir / "layer.dl").write_text(text)
    parts = [f'name = "{name}"', 'version = "0.1.0"', f"rank = {rank}", 'layers = ["layer.dl"]']
    for key, value in manifest.items():
        if isinstance(value, dict):
            items = ", ".join(f'{k} = "{v}"' for k, v in value.items())
            parts.append(f"{key} = {{ {items} }}")
        elif isinstance(value, list):
            parts.append(f"{key} = {value!r}".replace("'", '"'))
        else:
            parts.append(f'{key} = "{value}"')
    (layer_dir / "manifest.toml").write_text("\n".join(parts) + "\n")
    return layer_dir / "manifest.toml"


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_layer_rejects_head_in_non_open_lower_relation(tmp_path):
    lower = write_layer(tmp_path, "lower", 0, '.decl r(x:symbol)\nr("a.h").\n', exports=["r"])
    upper = write_layer(
        tmp_path,
        "upper",
        1,
        '.decl s(x:symbol)\nr(x) :- s(x).\ns("a.h").\n',
        requires={"lower": ">=0.1"},
    )
    errors = check_layers([lower, upper], tmp_path / "check")
    assert errors == ["upper: clause head r is owned by lower; not declared in extends"]


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_layer_rejects_head_in_private_lower_relation(tmp_path):
    lower = write_layer(tmp_path, "lower", 0, '.decl r(x:symbol)\nr("a.h").\n')
    upper = write_layer(tmp_path, "upper", 1, 'r("b.h").\n', requires={"lower": ">=0.1"})
    errors = check_layers([lower, upper], tmp_path / "check")
    assert errors == ["upper: clause head r is private to lower"]


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_layer_rejects_upward_body_reference_multiline(tmp_path):
    lower = write_layer(
        tmp_path,
        "lower",
        0,
        '.decl low(x:symbol)\nlow(x) :-\n  high("a.h"),\n  x = "1.0".\n',
    )
    # `high` must be readable (exported); private names are qualified away.
    upper = write_layer(
        tmp_path, "upper", 1, '.decl high(x:symbol)\nhigh("a.h").\n', exports=["high"]
    )
    errors = check_layers([lower, upper], tmp_path / "check")
    assert "lower: body relation high belongs to higher layer upper" in errors


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_layer_rejects_input_and_redeclaration(tmp_path):
    lower = write_layer(tmp_path, "lower", 0, '.decl r(x:symbol)\nr("x").\n', exports=["r"])
    upper = write_layer(tmp_path, "upper", 1, ".decl r(x:symbol)\n.input r\n")
    errors = check_layers([lower, upper], tmp_path / "check")
    assert any("forbidden directive .input" in e for e in errors)
    assert any("redeclares relation r" in e for e in errors)


def test_private_qualification_keeps_public_names():
    text = ".decl helper(x:symbol)\n.decl action(x:symbol)\naction(x) :- helper(x).\n"
    qualified = qualify_layer_text(text, "pkg", {"action"})
    assert "pkg__helper" in qualified
    assert ".decl action" in qualified


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
@pytest.mark.parametrize("clause", ["local(x) :- dormant(x).", 'dormant("x").'])
def test_dormant_package_does_not_widen_project_schema(tmp_path, clause):
    explicit = write_layer(tmp_path, "explicit", 0, ".decl root(x:symbol)\n", open=["root"])
    dormant = write_layer(tmp_path, "dormant", 1, ".decl dormant(x:symbol)\n", open=["dormant"])
    project = tmp_path / "project.dl"
    project.write_text(".decl local(x:symbol)\n" + clause + "\n")
    program = compose_program(
        [explicit, dormant],
        tmp_path / "program",
        tmp_path / "check",
        project_files=[project],
        explicit_manifests=frozenset({explicit}),
    )
    assert program.build is None
    assert any("project:" in e and "dormant" in e for e in program.errors), program.errors
    assert {layer.name for layer in program.layers} == {"explicit", "dormant", "project"}
    assert {layer.name for layer in program.schema_layers} == {"explicit", "project"}


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_explicit_schema_must_include_required_packages(tmp_path):
    dependency = write_layer(tmp_path, "dependency", 0, ".decl item(x:symbol)\n")
    explicit = write_layer(tmp_path, "explicit", 1, "", requires={"dependency": ">=0.1"})
    program = compose_program(
        [dependency, explicit],
        tmp_path / "program",
        tmp_path / "check",
        explicit_manifests=frozenset({explicit}),
    )
    assert any("omits required package dependency" in e for e in program.errors)


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_source_surface_excludes_metadata_and_dormant_declarations(tmp_path):
    kernel = write_layer(
        tmp_path,
        "kernel",
        0,
        ".decl target(t:symbol)\n.decl imported(t:symbol)\n"
        ".decl alias(n:symbol,t:symbol)\n"
        ".decl source_target(t:symbol)\n.decl source_imported(t:symbol)\n"
        ".decl source_alias(n:symbol,t:symbol)\n"
        "target(t) :- imported(t).\ntarget(t) :- source_target(t).\n"
        "imported(t) :- source_imported(t).\nalias(n,t) :- source_alias(n,t).\n"
        'imported("metadata").\n',
        open=["target", "imported", "alias"],
    )
    root = write_layer(
        tmp_path,
        "root",
        1,
        '#define DECLARE(n) target(n).\nDECLARE("root::macro")\n'
        '.decl helper(t:symbol)\nhelper("root::computed").\n'
        "target(t) :- helper(t).\n"
        'alias("root::alias","root::macro").\n'
        'imported("root::ground").\n'
        'imported(cat(t,"::derived")) :- helper(t).\n',
        extends=["target", "imported", "alias"],
        requires={"kernel": ">=0.1"},
    )
    dormant = write_layer(
        tmp_path,
        "dormant",
        2,
        'target("dormant::target").\n',
        extends=["target"],
        requires={"kernel": ">=0.1"},
    )
    program = compose_program(
        [PackageRef(kernel, "core"), PackageRef(root, "plugin"), dormant],
        tmp_path / "program",
        tmp_path / "check",
        explicit_manifests=frozenset({kernel, root}),
    )
    assert not program.errors, program.errors
    with program.build.open("a") as stream:
        for relation in ("source_target", "source_imported", "source_alias"):
            stream.write(f".output kernel__{relation}\n")
    output = tmp_path / "output"
    output.mkdir()
    result = subprocess.run(
        [str(souffle_path()), "-w", "-D", str(output), str(program.build)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(read_rows(output / "kernel__source_target.csv")) == {
        ("root::macro",),
        ("root::computed",),
    }
    assert read_rows(output / "kernel__source_imported.csv") == [("root::ground",)]
    assert read_rows(output / "kernel__source_alias.csv") == [("root::alias", "root::macro")]


def test_qualification_skips_strings_comments_and_attribute_names():
    text = (
        "// helper is private\n"
        ".decl helper(helper:symbol)\n"
        ".type Kind = Leaf {x:symbol} | Node {k:Kind}\n"
        "#define MK(x) $Leaf(x)\n"
        ".decl action(a:symbol, k:Kind)\n"
        'action("helper", MK(h)) :- helper(h).\n'
    )
    qualified = qualify_layer_text(text, "pkg", {"action"})
    assert "// helper is private" in qualified
    assert ".decl pkg__helper(helper:symbol)" in qualified
    assert ".type pkg__Kind = pkg__Leaf {x:symbol} | pkg__Node {k:pkg__Kind}" in qualified
    assert "#define pkg__MK(x) $pkg__Leaf(x)" in qualified
    assert 'action("helper", pkg__MK(h)) :- pkg__helper(h).' in qualified


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_layer_rejects_extends_of_non_open_relation(tmp_path):
    lower = write_layer(
        tmp_path, "lower", 0, ".decl r(x:symbol)\n.decl s(x:symbol)\n", open=["s"], exports=["r"]
    )
    upper = write_layer(
        tmp_path,
        "upper",
        1,
        'r("a").\ns("b").\n',
        extends=["r", "s"],
        requires={"lower": ">=0.1"},
    )
    errors = check_layers([lower, upper], tmp_path / "check")
    assert errors == [
        (
            "upper: extends r, which is not an open relation of a lower layer "
            "(owned by lower, not in its open registry)"
        )
    ]


def test_qualification_prefix_of_hyphenated_package():
    text = ".decl helper(x:symbol)\n.decl action(x:symbol)\naction(x) :- helper(x).\n"
    qualified = qualify_layer_text(text, "buildlog-cpp", {"action"})
    assert "buildlog_cpp__helper" in qualified


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_every_package_is_qualified_including_rank_zero(tmp_path):
    lower = write_layer(
        tmp_path, "lower", 0, ".decl helper(x:symbol)\n.decl pub(x:symbol)\n", exports=["pub"]
    )
    program = compose_program([lower], tmp_path / "program", tmp_path / "ast")
    assert program.errors == []
    assert program.layers[0].qualified == {"helper": "lower__helper"}
    text = (tmp_path / "program" / "lower" / "layer.dl").read_text()
    assert ".decl lower__helper(x:symbol)" in text and ".decl pub(x:symbol)" in text
