# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from buildlog.errors import SpecError
from buildlog.facts import (
    ProgramSchema,
    collect_project_facts,
    project_fact_rows,
    read_rows,
    tuple_key,
    write_rows,
)


@pytest.mark.parametrize(
    "kind", ["author-toml", "author-facts", "user-toml", "user-facts", "user-dl"]
)
def test_dormant_inputs_are_not_author_inputs(tmp_path, kind):
    project = tmp_path / "project"
    project.mkdir()
    schema = ProgramSchema(
        fields={"root": ["name"], "dormant": ["name"]},
        inputs={"root", "dormant"},
        author_relations={"root"},
        author_inputs={"root"},
    )
    suffix = kind.split("-")[1]
    path = project / f"dormant.{suffix}"
    path.write_text(
        'dormant = ["value"]\n'
        if suffix == "toml"
        else 'dormant("value").\n'
        if suffix == "dl"
        else "value\n"
    )
    is_user = kind.startswith("user")
    (project / "buildlog.toml").write_text(f"spec = {[] if is_user else [path.name]!r}\n")
    with pytest.raises(SpecError, match="dormant"):
        project_fact_rows(project, schema, user_files=[path] if is_user else [])


def test_dormant_package_facts_keep_the_union_schema(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text('root = ["source"]\n')
    package_facts = tmp_path / "package.toml"
    package_facts.write_text('dormant = ["payload"]\n')
    schema = ProgramSchema(
        fields={"root": ["name"], "dormant": ["name"]},
        inputs={"root", "dormant"},
        author_relations={"root"},
        author_inputs={"root"},
    )
    rows, _, _ = project_fact_rows(project, schema, package_facts=[package_facts])
    assert rows["root"] == {("source",)}
    assert rows["dormant"] == {("payload",)}


def test_source_markers_keep_author_origins_and_exclude_driver_answers(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text('target = ["source"]\nimported = ["bound"]\n')
    schema = ProgramSchema(
        fields={
            "target": ["name"],
            "imported": ["name"],
            "source_target_input": ["name"],
            "source_imported_input": ["name"],
        },
        inputs={"target", "imported", "source_target_input", "source_imported_input"},
        driver_inputs={"source_target_input", "source_imported_input"},
    )
    rows, origins, _ = project_fact_rows(project, schema)
    assert rows["source_target_input"] == {("source",)}
    assert rows["source_imported_input"] == {("bound",)}
    assert (
        origins[tuple_key("source_target_input", ("source",))]
        == origins[tuple_key("target", ("source",))]
    )


def test_toml_to_facts_and_source_map(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(
        """
[[target]]
name = "app"

[[dep]]
from = "app"
to = "lib"
visibility = "private"
"""
    )
    schema = ProgramSchema(
        fields={"target": ["name"], "dep": ["from", "to", "visibility"]},
        inputs={"target", "dep"},
    )
    source_map, _ = collect_project_facts(project, schema, tmp_path / "out")
    assert read_rows(tmp_path / "out" / "target.facts") == [("app",)]
    assert read_rows(tmp_path / "out" / "dep.facts") == [("app", "lib", "private")]
    assert any(str(project / "spec.toml") in origin for v in source_map.values() for origin in v)


def test_write_rows_is_verbatim_and_rejects_separators(tmp_path):
    path = tmp_path / "r.facts"
    write_rows(path, [('say "a\\b"', ""), ("x", "y")])
    assert path.read_text() == 'say "a\\b"\t\nx\ty\n'
    assert read_rows(path) == [('say "a\\b"', ""), ("x", "y")]
    for bad in ("a\tb", "a\nb", "a\rb"):
        with pytest.raises(ValueError, match="tab or line break"):
            write_rows(tmp_path / "bad.facts", [(bad,)])


def test_grouped_toml_unary_subject_and_ranks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(
        """
flag = ["a", "b"]

[target.app]
src = ["main.cpp", "util.cpp"]
dep = { private = ["lib", "z"] }
"""
    )
    schema = ProgramSchema(
        fields={
            "flag": ["name"],
            "target": ["t"],
            "src": ["t", "rank", "path"],
            "dep": ["t", "vis", "d"],
        },
        types={
            "flag": ["symbol"],
            "target": ["symbol"],
            "src": ["symbol", "number", "symbol"],
            "dep": ["symbol", "symbol", "symbol"],
        },
        inputs={"flag", "target", "src", "dep"},
    )
    collect_project_facts(project, schema, tmp_path / "out")
    assert read_rows(tmp_path / "out" / "flag.facts") == [("a",), ("b",)]
    assert read_rows(tmp_path / "out" / "target.facts") == [("app",)]
    assert read_rows(tmp_path / "out" / "src.facts") == [
        ("app", "0", "main.cpp"),
        ("app", "1", "util.cpp"),
    ]
    assert read_rows(tmp_path / "out" / "dep.facts") == [
        ("app", "private", "lib"),
        ("app", "private", "z"),
    ]


def test_named_row_sugar_states_one_row_and_its_parts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(
        """
[target.lib]
make = { from = "gen", fn = "f", flag = ["a", "b"], note = "n" }

[target.other]
make = { from = "gen" }
"""
    )
    schema = ProgramSchema(
        fields={
            "target": ["t"],
            "make": ["t", "from", "fn", "opt"],
            "make_flag": ["t", "flag"],
            "make_note": ["t", "note"],
        },
        types={
            "target": ["symbol"],
            "make": ["symbol"] * 4,
            "make_flag": ["symbol"] * 2,
            "make_note": ["symbol"] * 2,
        },
        inputs={"target", "make", "make_flag", "make_note"},
    )
    collect_project_facts(project, schema, tmp_path / "out")
    assert read_rows(tmp_path / "out" / "make.facts") == [
        ("lib", "gen", "f", ""),
        ("other", "gen", "", ""),
    ]
    assert read_rows(tmp_path / "out" / "make_flag.facts") == [("lib", "a"), ("lib", "b")]
    assert read_rows(tmp_path / "out" / "make_note.facts") == [("lib", "n")]


def test_subject_table_keys_have_their_own_lines(tmp_path):
    # Each subject table's keys are attributed to that table's lines, and a
    # multi-line array's rows do not end the table.
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(
        """[target.a]
prop = [
  ["k", "1"],
]
note = "a"

[target.b]
note = "b"
"""
    )
    schema = ProgramSchema(
        fields={"target": ["t"], "prop": ["t", "k", "v"], "note": ["t", "n"]},
        inputs={"target", "prop", "note"},
    )
    source_map, _ = collect_project_facts(project, schema, tmp_path / "out")
    lines = {k: {o.rsplit(":", 1)[1] for o in v} for k, v in source_map.items()}
    assert lines[tuple_key("note", ("a", "a"))] == {"5"}
    assert lines[tuple_key("note", ("b", "b"))] == {"8"}
    assert lines[tuple_key("prop", ("a", "k", "1"))] == {"2"}


def test_missing_entrypoint_and_bad_toml_are_clear(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    schema = ProgramSchema()
    with pytest.raises(Exception, match="missing project entry point"):
        collect_project_facts(project, schema, tmp_path / "out")
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text("x = 1\nx = 2\n")
    with pytest.raises(Exception, match=r"spec.toml:2:"):
        collect_project_facts(project, schema, tmp_path / "out")


def test_pyproject_tool_buildlog_is_an_entry_point(tmp_path):
    from buildlog.engine import project_uses
    from buildlog.facts import default_vars, project_source_root, project_spec

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """
[build-system]
requires = ["x"]

[project]
name = "demo"

[tool.buildlog]
uses = ["buildlog-cpp"]
source_root = "${SRC}"
spec = ["spec.toml"]

[tool.buildlog.vars]
SRC = "native"
"""
    )
    (project / "spec.toml").write_text('target = [["app"]]\n')
    schema = ProgramSchema(fields={"target": ["name"]}, inputs={"target"})
    collect_project_facts(project, schema, tmp_path / "out")
    assert read_rows(tmp_path / "out" / "target.facts") == [("app",)]
    assert project_uses(project) == ["buildlog-cpp"]
    assert default_vars(project)["SRC"] == "native"
    assert project_source_root(project) == project / "native"
    assert project_spec(project).config == (project / "pyproject.toml").resolve()


def test_pyproject_without_tool_buildlog_and_both_entry_points(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    schema = ProgramSchema()
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    with pytest.raises(Exception, match="missing project entry point"):
        collect_project_facts(project, schema, tmp_path / "out")
    (project / "pyproject.toml").write_text("[tool.buildlog]\nspec = []\n")
    (project / "buildlog.toml").write_text("spec = []\n")
    with pytest.raises(Exception, match="both buildlog.toml and pyproject.toml"):
        collect_project_facts(project, schema, tmp_path / "out")
