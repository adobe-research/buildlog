# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The authoring sugar: loader syntax (infix conditions, multi-line strings, `$${`, placement
warnings) and the core vocabulary it desugars to."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from buildlog import conditions
from buildlog.engine import configure_project
from buildlog.errors import SpecError
from buildlog.facts import (
    ProgramSchema,
    collect_project_facts,
    read_rows,
    substitute_vars,
    text_lines,
    tuple_key,
)


def _final_round(build):
    """The one round dir configure leaves: facts-in/round-<final_round>."""
    return f"round-{json.loads((build / 'metrics.json').read_text())['final_round']}"


@pytest.mark.parametrize(
    ("text", "adt"),
    [
        ("A", "$Named(A)"),
        ("!A", "$Not($Named(A))"),
        ("a & b & c", "$And($Named(a), $And($Named(b), $Named(c)))"),
        ("a | b & c", "$Or($Named(a), $And($Named(b), $Named(c)))"),
        ("(a | b) & !c", "$And($Or($Named(a), $Named(b)), $Not($Named(c)))"),
        ("native.opt == debug", "$Eq(native.opt, debug)"),
        ("native.opt=debug", "$Eq(native.opt, debug)"),
        ("platform != arm64-macos", "$Not($Eq(platform, arm64-macos))"),
        (
            "defined(ZLIB::ZLIB) & !cc.check.X",
            "$And($Defined(ZLIB::ZLIB), $Not($Named(cc.check.X)))",
        ),
        ("$Eq(option.X, on)", "$Eq(option.X, on)"),
    ],
)
def test_infix_conditions_parse_to_the_cond_adt(text, adt):
    assert conditions.parse(text, "here") == adt


@pytest.mark.parametrize("bad", ["", "a &", "(a", "a b", "a == ", "a | | b"])
def test_malformed_infix_conditions_are_errors(bad):
    with pytest.raises(SpecError, match="here: condition"):
        conditions.parse(bad, "here")


def test_condition_names_versus_inline_expressions():
    assert conditions.is_name("diagnostics")
    assert conditions.is_name("cc.check.HAVE_X=yes")
    assert conditions.is_name("zlib.no_asm")
    assert not conditions.is_name("!X")
    assert not conditions.is_name("a & b")
    assert not conditions.is_name("$Eq(a, b)")


def test_dollar_dollar_brace_is_a_literal():
    assert substitute_vars("$${exec_prefix}/lib ${P}", {"P": "x"}, "o") == "${exec_prefix}/lib x"


def _project(tmp_path: Path, spec: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('uses = ["buildlog-cpp"]\nspec = ["spec.toml"]\n')
    (project / "spec.toml").write_text(spec)
    return project


HEADER = """
config_decl = [["release", "arm64-macos"], ["debug", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"], ["debug", "native.opt", "debug"]]
request_all = ["lib"]
option_decl = [["FANCY", "off"], ["LOUD", "on"]]
condition = [["both", "FANCY | (LOUD & native.opt == debug)"]]
"""


def _conds(facts: Path) -> set[tuple[str, str]]:
    out = set()
    for cfg, name in read_rows(facts / "cond_holds.csv"):
        out.add(("debug" if "debug" in cfg else "release", name))
    return out


def test_infix_conditions_options_and_anonymous_conditions(tmp_path):
    project = _project(
        tmp_path,
        HEADER
        + """
[target.lib]
prop_when = [
  ["cc.define", "PLAIN=1", "interface", "!FANCY"],
  ["cc.define", "BOTH=1", "interface", "both"],
  ["cc.define", "LOUD_REL=1", "interface", "LOUD & native.opt != debug"],
]
""",
    )
    facts, _ = configure_project(project, tmp_path / "build", emit=False)
    held = _conds(facts)
    # An option is the condition of its own name.
    assert ("release", "LOUD") in held and ("release", "FANCY") not in held
    assert ("release", "!FANCY") in held
    assert ("debug", "both") in held and ("release", "both") not in held
    assert ("release", "LOUD & native.opt != debug") in held
    assert ("debug", "LOUD & native.opt != debug") not in held
    # The anonymous condition's origin is the row that wrote it.
    source_map = json.loads(
        (
            tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build") / "source_map.json"
        ).read_text()
    )
    key = tuple_key("condition", ("!FANCY", "$Not($Named(FANCY))"))
    assert source_map[key] == [f"{project / 'spec.toml'}:9"]


def test_unknown_name_in_a_condition_is_an_error(tmp_path):
    project = _project(
        tmp_path,
        HEADER.replace('"FANCY | (LOUD & native.opt == debug)"', '"NOPE & LOUD"'),
    )
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    metrics = (tmp_path / "build" / "metrics.json").read_text()
    assert "unknown condition" in metrics and "NOPE" in metrics


def test_self_referential_condition_is_an_error(tmp_path):
    project = _project(
        tmp_path,
        HEADER.replace(
            '["both", "FANCY | (LOUD & native.opt == debug)"]', '["a", "b & LOUD"], ["b", "!a"]'
        ),
    )
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    assert "condition refers to itself" in (tmp_path / "build" / "metrics.json").read_text()


def test_key_captured_by_a_table_header_warns(tmp_path):
    project = _project(
        tmp_path,
        """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
request_all = ["lib"]

[target.lib]
prop = [["cc.define", "A", "interface"]]
condition = "native.opt == release"
""",
    )
    _facts, metrics = configure_project(project, tmp_path / "build", emit=False)
    (warning,) = [w for w in metrics["warnings"] if w[0] == "key captured by a table header"]
    assert "spec.toml:8" in warning[1] and "condition under [target.lib]" in warning[1]


def test_captured_row_arrays_name_the_table(tmp_path):
    project = _project(
        tmp_path,
        """
[target.lib]
prop = [["cc.define", "A", "interface"]]
config_decl = [["release", "arm64-macos"]]
""",
    )
    with pytest.raises(SpecError, match=r"inside \[target.lib\].*top-level relation after a table"):
        configure_project(project, tmp_path / "build", emit=False)


def test_multi_line_strings_survive_as_text(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text('spec = ["spec.toml"]\n')
    (project / "spec.toml").write_text('note = [["a", """first\nsecond\n"""]]\n')
    schema = ProgramSchema(fields={"note": ["k", "text"]}, inputs={"note"})
    collect_project_facts(project, schema, tmp_path / "out")
    ((_key, text),) = read_rows(tmp_path / "out" / "note.facts")
    assert "\n" not in text and text_lines(text) == "first\nsecond\n"


# ---- the core vocabulary (V_vocabulary.dl, L1_options.dl) ----


def _query(builddir: Path, pattern: str) -> list[tuple]:
    from buildlog.provenance.query import run_query
    from buildlog.provenance.souffle import load_model

    return run_query(load_model(builddir), pattern).rows


def _uses(facts: Path, target: str) -> set[tuple[str, str]]:
    return {(k, v) for t, _c, k, v, _r in read_rows(facts / "use.csv") if t == target}


VOCAB = """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
request_all = ["app"]

[option.FANCY]
help = "fancy output"
[option.LOUD]
default = "on"

[defaults.target]
profile = "common"
private = { "cc.define" = "DEFAULTED=1" }

[target.lib]
kind = "library"
sources = ["lib.cpp", "lib.h"]
public = { "cc.define" = "LIB" }

[target.app]
kind = "executable"
sources = "main.cpp"
deps = { private = ["lib"] }
private = { "cxx.std" = "20" }
when."!FANCY".private."cc.define" = "PLAIN=1"
when."LOUD & native.opt == release" = { private = { "cc.define" = ["LOUD=1", "REL=1"] } }
"""


def _vocab_project(tmp_path: Path, spec: str = VOCAB) -> Path:
    project = _project(tmp_path, spec)
    (project / "profiles.toml").write_text(
        '[profile.common]\nprivate = { "cxx.std" = "17", "cc.define" = ["COMMON=1"] }\n'
    )
    (project / "buildlog.toml").write_text(
        'uses = ["buildlog-cpp"]\nspec = ["spec.toml", "profiles.toml"]\n'
    )
    return project


def test_target_tables_defaults_and_profiles(tmp_path):
    project = _vocab_project(tmp_path)
    facts, metrics = configure_project(project, tmp_path / "build", emit=False)
    assert metrics["errors"] == []
    app = _uses(facts, "app")
    # Map values add up across the table, its file's defaults and the
    # profile; cxx.std's lattice takes the highest.
    assert {
        ("cc.define", d) for d in ("COMMON=1", "DEFAULTED=1", "PLAIN=1", "LOUD=1", "REL=1")
    } <= app
    assert ("cc.define", "LIB") in app
    stds = _query(tmp_path / "build", 'use_join(t, _, "cxx.std", v)')
    assert set(stds) == {("app", "20"), ("lib", "17")}
    # A header is a claimed source that compiles nothing.
    kinds = (
        {(t, k) for t, k in read_rows(facts / "native_kind.csv")}
        if (facts / "native_kind.csv").exists()
        else None
    )
    if kinds is not None:
        assert ("app", "executable") in kinds
    source_map = json.loads(
        (
            tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build") / "source_map.json"
        ).read_text()
    )
    key = tuple_key("target_private", ("app", "cc.define", "COMMON=1"))
    assert source_map[key] == [f"{project / 'profiles.toml'}:2"]


def test_inline_subject_tables_cite_their_own_line(tmp_path):
    spec = VOCAB.replace(
        '[option.FANCY]\nhelp = "fancy output"\n[option.LOUD]\ndefault = "on"\n',
        '[option]\nFANCY = { help = "fancy output" }\nLOUD = { default = "on" }\n',
    )
    project = _vocab_project(tmp_path, spec)
    configure_project(project, tmp_path / "build", emit=False)
    source_map = json.loads(
        (
            tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build") / "source_map.json"
        ).read_text()
    )
    line = spec.splitlines().index('LOUD = { default = "on" }') + 1
    assert source_map[tuple_key("option_default", ("LOUD", "on"))] == [
        f"{project / 'spec.toml'}:{line}"
    ]


def test_compact_check_tables(tmp_path):
    spec = (
        'cc_check_symbol = [["HAVE_STRTOD", "strtod", "stdlib.h"]]\n'
        + VOCAB
        + '\n[cc_check_include]\nHAVE_STDLIB_H = "stdlib.h"\n'
        + '[cc_check_flag]\nHAVE_WALL = "-Wall"\n'
        + '[cc_check_function]\nHAVE_MEMSET = "memset"\n'
    )
    project = _vocab_project(tmp_path, spec)
    configure_project(project, tmp_path / "build", emit=False)
    content = {
        r[0]: r[1:] for r in _query(tmp_path / "build", "cc_check_content(n, l, k, s, h, a)")
    }
    assert content["HAVE_STDLIB_H"] == ("c", "include", "stdlib.h", "", "")
    assert content["HAVE_WALL"] == ("c", "compiles", "int main(void) { return 0; }", "", "-Wall")
    assert content["HAVE_MEMSET"] == ("c", "function", "memset", "", "")
    program = "int main(int argc, char** argv) { (void)argv; return ((int*)(&strtod))[argc]; }"
    assert content["HAVE_STRTOD"] == ("c", "compiles", program, "stdlib.h", "")
    answers = {
        a: v
        for _c, a, v in _query(tmp_path / "build", "config_attr(c, a, v)")
        if a.startswith("cc.check.")
    }
    assert answers["cc.check.HAVE_STDLIB_H"] == "yes"
    assert answers["cc.check.HAVE_STRTOD"] == "yes"
    source_map = json.loads(
        (
            tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build") / "source_map.json"
        ).read_text()
    )
    line = spec.splitlines().index('HAVE_STDLIB_H = "stdlib.h"') + 1
    assert source_map[tuple_key("cc_check_include", ("HAVE_STDLIB_H", "stdlib.h"))] == [
        f"{project / 'spec.toml'}:{line}"
    ]


def test_unclaimed_source_suffix_is_an_error(tmp_path):
    project = _vocab_project(tmp_path, VOCAB.replace('"main.cpp"', '"main.swift"'))
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    metrics = (tmp_path / "build" / "metrics.json").read_text()
    assert "no plugin compiles this source" in metrics and "main.swift" in metrics


def test_unknown_profile_is_an_error(tmp_path):
    project = _project(tmp_path, VOCAB)
    with pytest.raises(SpecError, match="unknown profile common"):
        configure_project(project, tmp_path / "build", emit=False)


GATED = """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]

[option.EXTRA]
requires = "FANCY"
[option.FANCY]
[option.FLAVOR]
choice = ["plain", "spicy"]
default = "plain"

[target.tool]
kind = "executable"
sources = "main.cpp"
request_when = "EXTRA & option.FLAVOR == spicy"
kind_when = { module_library = "FANCY" }

[target.check]
kind = "executable"
sources = "check.cpp"
test = true
request_when = "FANCY"
"""


def _needed(facts: Path) -> set[str]:
    return {t for t, _c in read_rows(facts / "needed.csv")}


def test_options_decide_what_is_built(tmp_path):
    project = _project(tmp_path, GATED)
    build = tmp_path / "build"
    facts, _ = configure_project(project, build, emit=False)
    assert not {"tool", "check"} & _needed(facts)
    facts, _ = configure_project(
        project,
        build,
        emit=False,
        option_overrides={"EXTRA": "on", "FANCY": "on", "FLAVOR": "spicy"},
    )
    assert {"tool", "check"} <= _needed(facts)
    assert _query(build, 'native_kind("tool", k)') == [("module_library",)]
    # -O is recorded: a reconfigure without it keeps the values.
    state = json.loads((build / "buildlog-state.json").read_text())
    assert state["options"] == {"EXTRA": "on", "FANCY": "on", "FLAVOR": "spicy"}
    facts, _ = configure_project(project, build, emit=False)
    assert "tool" in _needed(facts)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"EXTRA": "on"}, "option requirement not met"),
        ({"FLAVOR": "sour"}, "option value is not one of its choices"),
        ({"FANCY": "maybe"}, "option value is not on or off"),
    ],
)
def test_option_constraints(tmp_path, options, message):
    project = _project(tmp_path, GATED)
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False, option_overrides=options)
    assert message in (tmp_path / "build" / "metrics.json").read_text()


def test_a_condition_deciding_what_is_built_tests_only_options(tmp_path):
    project = _project(
        tmp_path, GATED.replace('"EXTRA & option.FLAVOR == spicy"', '"native.opt == debug"')
    )
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    assert "may test only options" in (tmp_path / "build" / "metrics.json").read_text()


def test_inline_spec_presets_and_captured_vars(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "buildlog.toml").write_text(
        """uses = ["buildlog-cpp"]
config_decl = [["release", "arm64-macos"]]
request_all = ["lib"]

[export.pkg]
version = "1.2"
component = { lib = "lib" }

[target.lib]
interface = { "cc.define" = "X" }

[vars]
X = "1"
option_decl = "oops"

[presets.mine]
install_prefix = "${PREFIX}"
"""
    )
    from buildlog.facts import preset_path

    build = tmp_path / "build"
    _facts, metrics = configure_project(
        project,
        build,
        emit=False,
        user_files=[preset_path(project, "mine")],
        var_overrides={"PREFIX": "/opt/p"},
    )
    rows = tmp_path / "build" / "facts-in" / _final_round(tmp_path / "build")
    assert read_rows(rows / "export_version.facts") == [("pkg", "1.2")]
    assert read_rows(rows / "install_prefix.facts") == [("/opt/p",)]
    (warning,) = [w for w in metrics["warnings"] if "under [vars]" in w[1]]
    assert re.search(r"buildlog\.toml:14\b.*option_decl under \[vars\]", warning[1]), warning[1]
    with pytest.raises(Exception, match="no \\[presets.theirs\\]"):
        preset_path(project, "theirs")


def test_path_helpers_and_join_parts(tmp_path):
    project = _project(
        tmp_path,
        'config_decl = [["release", "arm64-macos"]]\nrequest_all = ["t"]\ntarget = ["t"]\n',
    )
    (project / "buildlog.toml").write_text(
        'uses = ["buildlog-cpp"]\nspec = ["spec.toml", "rules.dl"]\n'
    )
    (project / "rules.dl").write_text(
        """.decl p(x:symbol)
p("a/b/c.fbs").
p("x.tar.gz").
p("dir/.hidden").
p("plain").
path_want(x) :- p(x).
join_part("g", 2, "+b").
join_part("g", 1, "-march=x").
join_part("g", 3, "+c").
"""
    )
    build = tmp_path / "build"
    configure_project(project, build, emit=False, update_lock=True)
    rows = _query(
        build, "p(x), path_basename(x, b), path_dirname(x, d), path_stem(x, s), path_ext(x, e)"
    )
    assert set(rows) == {
        ("a/b/c.fbs", "c.fbs", "a/b", "c", ".fbs"),
        ("x.tar.gz", "x.tar.gz", "", "x.tar", ".gz"),
        ("dir/.hidden", ".hidden", "dir", ".hidden", ""),
        ("plain", "plain", "", "plain", ""),
    }
    assert _query(build, 'joined("g", s)') == [("-march=x+b+c",)]
    assert _query(build, 'gen_dir("t", _, d)') == [("out:gen/arm64-macos.debug.none/t",)]


GENERATORS = """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
request_all = ["app"]
generate = [["schema", "app", "a"], ["schema", "app", "b"]]

[target.gen]
kind = "executable"
sources = "gen.cpp"

[target.app]
kind = "executable"
sources = "main.cpp"
gen_include = { private = [""] }

[generator.schema]
params = ["owner", "name"]
owner = "{owner}"
run = ["gen", "${list:FLAGS}", "-o", "${gen}", "--in=${src:{name}.def}"]
output = ["{name}.h"]

[generator.table]
owner = "app"
label = "TABLE"
run = ["gen", "--tool=${tool:gen}", "${src:t.def}"]
stdout = "${out:t.inc}"

[word_list]
FLAGS = ["-x", "-y"]
"""


def _args(builddir: Path, action_suffix: str) -> list[str]:
    rows = _query(builddir, "action_arg(a, r, p, v)")
    mine = sorted((r, p + v) for a, r, p, v in rows if a.endswith(action_suffix))
    return [w for _, w in mine]


def test_generator_templates_and_placeholders(tmp_path):
    project = _project(tmp_path, GENERATORS)
    build = tmp_path / "build"
    _facts, metrics = configure_project(project, build, emit=False)
    assert metrics["errors"] == []
    gen = "out:gen/arm64-macos.release.none/app"
    args = _args(build, "/app/a.h")
    assert args[1:] == ["-x", "-y", "-o", gen, "--in=src:a.def"]
    assert args[0].endswith("/gen")  # the program, by target reference
    assert _query(build, f'action("{gen}/t.inc", l)') == [("TABLE",)]
    assert _query(build, f'action_stdout("{gen}/t.inc", p)') == [(f"{gen}/t.inc",)]
    assert _query(build, f'action_in("{gen}/b.h", "src:b.def", k)') == [("explicit",)]
    # Outputs are the owner's generated headers; gen_include puts the
    # directory on its include path.
    headers = {v for (v,) in _query(build, 'use("app", _, "cc.generated_header", v, _)')}
    assert headers == {f"{gen}/a.h", f"{gen}/b.h", f"{gen}/t.inc"}
    assert (gen,) in _query(build, 'use("app", _, "cc.include_dir", v, _)')
    # why cites the template's line and the instance's row.
    source_map = json.loads(
        (build / "facts-in" / _final_round(build) / "source_map.json").read_text()
    )
    key = tuple_key("gen_word", ("schema(app, a)", "4000", "--in=", "src", "a.def", ""))
    assert source_map[key] == [f"{project / 'spec.toml'}:19", f"{project / 'spec.toml'}:5"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('"--in=${src:{name}.def}"', '"${src:x}${src:y}"'), "more than one placeholder"),
        (('"--in=${src:{name}.def}"', '"${src:x}.bak"'), "text after a placeholder"),
        (('["schema", "app", "b"]', '["schema", "app"]'), "takes 2 values"),
        (('stdout = "${out:t.inc}"', 'stdout = "${src:t.inc}"'), "must be a ${out:...}"),
    ],
)
def test_generator_mistakes(tmp_path, change, message):
    project = _project(tmp_path, GENERATORS.replace(*change))
    with pytest.raises(SpecError, match=re.escape(message)):
        configure_project(project, tmp_path / "build", emit=False)


GATE_ERROR = "target is requested although its options disable it"
NEEDED_GATE_ERROR = "target is needed although its options disable it"


def _gated_project(tmp_path: Path, extra: str = "") -> Path:
    return _project(
        tmp_path,
        extra
        + """
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
[option.FANCY]
[target.tool]
kind = "executable"
sources = "main.cpp"
request_when = "FANCY"
""",
    )


def _metrics_text(tmp_path: Path) -> str:
    return (tmp_path / "build" / "metrics.json").read_text()


def test_requesting_a_disabled_gated_target_is_an_error(tmp_path):
    project = _gated_project(tmp_path, 'request_all = ["tool"]\n')
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    assert GATE_ERROR in _metrics_text(tmp_path)


def test_an_enabled_gated_target_may_be_requested_again(tmp_path):
    project = _gated_project(tmp_path, 'request_all = ["tool"]\n')
    facts, _ = configure_project(
        project, tmp_path / "build", emit=False, option_overrides={"FANCY": "on"}
    )
    assert "tool" in _needed(facts)


def test_a_disabled_gated_target_nobody_requests_is_simply_not_built(tmp_path):
    project = _gated_project(tmp_path)
    facts, _ = configure_project(project, tmp_path / "build", emit=False)
    assert "tool" not in _needed(facts)


def test_a_target_needed_by_a_requested_one_is_an_error_when_its_gate_fails(tmp_path):
    project = _gated_project(
        tmp_path,
        'request_all = ["app"]\n',
    )
    spec = project / "spec.toml"
    spec.write_text(
        spec.read_text()
        + '\n[target.app]\nkind = "executable"\nsources = "app.cpp"\ndeps = { private = ["tool"] }\n'
    )
    with pytest.raises(SystemExit):
        configure_project(project, tmp_path / "build", emit=False)
    assert NEEDED_GATE_ERROR in _metrics_text(tmp_path)


def test_a_target_needed_by_a_requested_one_is_built_when_its_gate_holds(tmp_path):
    project = _gated_project(tmp_path, 'request_all = ["app"]\n')
    spec = project / "spec.toml"
    spec.write_text(
        spec.read_text()
        + '\n[target.app]\nkind = "executable"\nsources = "app.cpp"\ndeps = { private = ["tool"] }\n'
    )
    facts, _ = configure_project(
        project, tmp_path / "build", emit=False, option_overrides={"FANCY": "on"}
    )
    assert {"app", "tool"} <= _needed(facts)
