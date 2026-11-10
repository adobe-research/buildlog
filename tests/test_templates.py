# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Target templates (coexisting variants):
target_template(b) states rows like a target's; instance_of(t, b) makes t
a target with them, placeholders {k} expanded by instance_param(t, k, v)
(several values, several rows; none, no row) and {name} by t."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from buildlog.paths import souffle_path
from tests.test_cpp_mechanisms import (
    cmd_of,
    configure,
    configure_fails,
    needs_souffle,
    ninja_text,
    project,
)

REL = "arm64-macos.release.none"

FILES = {
    "impl_a.c": "int impl(void) { return 1; }\n",
    "impl_b.c": "int impl(void) { return 2; }\n",
    "common.c": "int impl(void);\nint value(void) { return impl() * LEVEL; }\n",
    "main.c": '#include <stdio.h>\nint value(void);\nint main(void) { printf("%d\\n", value()); }\n',
}

SPEC = """
request = [["app_x", "rel"], ["app_y", "rel"]]

[target_template.kernel]
param = ["impl", "level", "flag"]
kind = "library"
sources = ["common.c", "impl_{impl}.c"]
private = { "cc.define" = ["LEVEL={level}", "KERNEL_{name}"], "cc.compile_flag" = ["{flag}"] }
deps = { private = ["{name}_helper"] }

[target_template.helper]
kind = "object_library"

[target.kernel_x]
instance_of = "kernel"
instance_param = [["impl", "a"], ["level", "3"], ["flag", "-Wall"], ["flag", "-Wextra"]]

[target.kernel_y]
instance_of = "kernel"
instance_param = [["impl", "b"], ["level", "5"]]
private = { "cc.define" = "ONLY_Y" }

[target.kernel_x_helper]
instance_of = "helper"
[target.kernel_y_helper]
instance_of = "helper"

[target.app_x]
kind = "executable"
sources = ["main.c"]
deps = { private = ["kernel_x"] }

[target.app_y]
kind = "executable"
sources = ["main.c"]
deps = { private = ["kernel_y"] }
"""


@needs_souffle
def test_instances_take_the_templates_rows_with_placeholders_expanded(tmp_path):
    proj = project(tmp_path, SPEC, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    x = cmd_of(build, f"obj/{REL}/kernel_x/impl_a.c.o")
    assert "-DLEVEL=3" in x and "-DKERNEL_kernel_x" in x and "-Wall" in x and "-Wextra" in x
    y = cmd_of(build, f"obj/{REL}/kernel_y/impl_b.c.o")
    # No flag values: no flag row. The instance's own row is added.
    assert "-DLEVEL=5" in y and "-DONLY_Y" in y and "-Wall" not in y and "-Wextra" not in y
    text = ninja_text(build)
    assert f"lib/{REL}/libkernel_x.a" in text and f"lib/{REL}/libkernel_y.a" in text
    assert "impl_b.c" not in cmd_of(build, f"lib/{REL}/libkernel_x.a")
    # The template is not a target: nothing of its own is built.
    assert "/kernel/" not in text and "libkernel.a" not in text
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    out = build / "souffle" / "bin" / REL
    assert subprocess.check_output([str(out / "app_x")], text=True) == "3\n"
    assert subprocess.check_output([str(out / "app_y")], text=True) == "10\n"


@needs_souffle
@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            (
                'instance_of = "helper"\n[target.kernel_y_helper]',
                'instance_of = "nope"\n[target.kernel_y_helper]',
            ),
            "instance of an undeclared template",
        ),
        (
            ('param = ["impl", "level", "flag"]', 'param = ["impl", "flag"]'),
            "unknown template parameter",
        ),
        (
            ('["impl", "b"], ["level", "5"]', '["impl", "b"], ["level", "5"], ["colour", "red"]'),
            "instance parameter of none of its templates",
        ),
        (('"LEVEL={level}"', '"LEVEL={level"'), "unclosed template placeholder"),
        (
            ("[target_template.helper]", "[target.helper]\n[target_template.helper]"),
            "target template is also a target",
        ),
    ],
)
def test_template_errors(tmp_path, capsys, edit, message):
    old, new = edit
    assert old in SPEC
    proj = project(tmp_path, SPEC.replace(old, new, 1), FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert message in err, err


def _defines(tmp_path, template_define, params, declared=()):
    spec = f"""
request = [["app", "rel"]]

[target_template.k]
param = {sorted({p for p, _ in params} | set(declared))!r}
kind = "library"
sources = ["common.c"]
private = {{ "cc.define" = ["{template_define}"] }}

[target.k1]
instance_of = "k"
instance_param = {[list(p) for p in params]!r}

[target.app]
kind = "executable"
sources = ["main.c"]
deps = {{ private = ["k1"] }}
""".replace("'", '"')
    proj = project(tmp_path, spec, FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cmd = cmd_of(build, f"obj/{REL}/k1/common.c.o")
    return sorted(t[2:] for t in cmd.split() if t.startswith("-DV="))


@needs_souffle
def test_one_key_has_one_value_per_row(tmp_path):
    got = _defines(tmp_path, "V={p}/{p}", [("p", "a"), ("p", "b")])
    assert got == ["V=a/a", "V=b/b"]


@needs_souffle
def test_distinct_keys_multiply(tmp_path):
    got = _defines(
        tmp_path, "V={p}{q}{p}", [("p", "a"), ("p", "b"), ("q", "1"), ("q", "2"), ("q", "3")]
    )
    assert len(got) == 6 and len(set(got)) == 6
    assert all(g[2] == g[4] for g in got)


@needs_souffle
def test_key_without_values_drops_the_row(tmp_path):
    got = _defines(tmp_path, "V={p}{q}{p}", [("p", "a")], declared=("q",))
    assert got == []


@needs_souffle
def test_repeated_key_does_not_blow_up(tmp_path):
    import time

    t0 = time.time()
    got = _defines(tmp_path, "V=" + "{p}" * 12, [("p", "a"), ("p", "b")])
    assert got == ["V=" + "a" * 12, "V=" + "b" * 12]
    assert time.time() - t0 < 20


H = 12


def _step_counts(build):
    """tmpl_step / ptmpl_step sizes of the last round, by rerunning the composed
    program with .printsize on them."""
    prog = build / "program"
    (prog / "count.dl").write_text(
        '#include "build.dl"\n.printsize buildlog__tmpl_step\n.printsize buildlog__ptmpl_step\n.printsize tmpl_value\n.printsize ptmpl_value\n'
    )
    facts = max((build / "facts-in").iterdir(), key=lambda d: int(d.name.split("-")[1]))
    out = build / "count-out"
    out.mkdir()
    cp = subprocess.run(
        [str(souffle_path()), "-w", "-F", str(facts), "-D", str(out), str(prog / "count.dl")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return {
        name.removeprefix("buildlog__"): int(n)
        for name, n in (
            ln.split()
            for ln in cp.stdout.splitlines()
            if ln.startswith(("buildlog__", "tmpl_", "ptmpl_"))
        )
    }


@needs_souffle
@pytest.mark.parametrize("row", ["source", "property"])
def test_row_with_unbound_placeholder_builds_no_partial_terms(tmp_path, row):
    """h placeholders, two values for h-1 of them and none for the last: the
    row drops before expansion begins (no error, as ever), and no partial
    term is built for it (it used to build about 2**(h-1))."""
    holes = "".join(f"{{p{i}}}" for i in range(1, H + 1))
    params = [[f"p{i}", v] for i in range(1, H) for v in ("a", "b")]
    rows = (
        f'sources = ["common.c", "x_{holes}.c"]'
        if row == "source"
        else (f'sources = ["common.c"]\nprivate = {{ "cc.define" = ["V={holes}"] }}')
    )
    spec = f"""
request = [["app", "rel"]]

[target_template.k]
param = {[f"p{i}" for i in range(1, H + 1)]!r}
kind = "library"
{rows}

[target.k1]
instance_of = "k"
instance_param = {params!r}

[target.app]
kind = "executable"
sources = ["main.c"]
deps = {{ private = ["k1"] }}
""".replace("'", '"')
    proj = project(tmp_path, spec, FILES)
    t0 = time.time()
    build, metrics = configure(proj, tmp_path)
    elapsed = time.time() - t0
    assert metrics["errors"] == []
    cmd = cmd_of(build, f"obj/{REL}/k1/common.c.o")
    assert "-DV=" not in cmd and "x_" not in cmd
    counts = _step_counts(build)
    # the complete rows ("common.c", the empty ones) take one step each
    assert counts["tmpl_step"] + counts["ptmpl_step"] < 20, (counts, elapsed)


@needs_souffle
@pytest.mark.parametrize("row", ["source", "property"])
@pytest.mark.parametrize(
    ("tail", "message"),
    [("{zz}", "unknown template parameter"), ("{p", "unclosed template placeholder")],
)
def test_row_with_bad_placeholder_errors_without_partial_terms(
    tmp_path, capsys, row, tail, message
):
    """An unknown or an unclosed placeholder after 10 two-valued parameters is
    reported where the walk reaches it, and the row never expands (it used to
    build 2**10 partial terms first)."""
    holes = "".join(f"{{p{i}}}" for i in range(1, 11)) + tail
    params = [[f"p{i}", v] for i in range(1, 11) for v in ("a", "b")]
    rows = (
        f'sources = ["common.c", "x_{holes}.c"]'
        if row == "source"
        else (f'sources = ["common.c"]\nprivate = {{ "cc.define" = ["V={holes}"] }}')
    )
    spec = f"""
request = [["app", "rel"]]

[target_template.k]
param = {[f"p{i}" for i in range(1, 11)]!r}
kind = "library"
{rows}

[target.k1]
instance_of = "k"
instance_param = {params!r}

[target.app]
kind = "executable"
sources = ["main.c"]
deps = {{ private = ["k1"] }}
""".replace("'", '"')
    proj = project(tmp_path, spec, FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert message in err and "k: " in err, err
    if message.startswith("unknown"):
        assert "{zz}" in err, err
    counts = _step_counts(tmp_path / "build")
    assert counts["tmpl_step"] + counts["ptmpl_step"] < 20, counts


def _two_template_spec(row, a_params, b_params, holes, values):
    """An instance k1 of templates A and B, both with the row `holes`."""

    def template(name, params):
        body = (
            f'sources = ["common.c", "x_{holes}.c"]'
            if row == "source"
            else f'sources = ["common.c"]\nprivate = {{ "cc.define" = ["V={holes}"] }}'
        )
        kind = 'kind = "library"\n' if name == "a" else ""
        return f"[target_template.{name}]\nparam = {params!r}\n{kind}{body}\n"

    return f"""
request = [["app", "rel"]]

{template("a", a_params)}
{template("b", b_params)}
[target.k1]
instance_of = ["a", "b"]
instance_param = {values!r}

[target.app]
kind = "executable"
sources = ["main.c"]
deps = {{ private = ["k1"] }}
""".replace("'", '"')


@needs_souffle
@pytest.mark.parametrize("row", ["source", "property"])
def test_each_template_checks_its_own_declarations(tmp_path, capsys, row):
    """a declares only the first five parameters, b only the last five, and the
    instance binds all ten with two values: the walk must not take a's
    declarations for one placeholder and b's for the next. Both rows are
    invalid for their own template, are reported, and never expand."""
    keys = [f"p{i}" for i in range(1, 11)]
    holes = "".join(f"{{{k}}}" for k in keys)
    spec = _two_template_spec(
        row, keys[:5], keys[5:], holes, [[k, v] for k in keys for v in ("a", "b")]
    )
    proj = project(tmp_path, spec, FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert "unknown template parameter" in err, err
    assert "a: {p6} in" in err and "b: {p1} in" in err, err
    counts = _step_counts(tmp_path / "build")
    assert counts["tmpl_step"] + counts["ptmpl_step"] < 20, counts
    assert counts["tmpl_value"] + counts["ptmpl_value"] <= 2, counts


@needs_souffle
@pytest.mark.parametrize("row", ["source", "property"])
def test_row_valid_for_one_template_expands_for_it_only(tmp_path, capsys, row):
    """a declares only p1 and b declares p1 and p2, both with the row
    {p1}{p2}: it is reported for a and expands, four values, for b alone."""
    values = [[k, v] for k in ("p1", "p2") for v in ("a", "b")]
    spec = _two_template_spec(row, ["p1"], ["p1", "p2"], "{p1}{p2}", values)
    proj = project(tmp_path, spec, FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert "a: {p2} in" in err and "b: {p" not in err, err
    counts = _step_counts(tmp_path / "build")
    if row == "source":
        # common.c once per template, and b's four expansions
        assert counts["tmpl_value"] == 6, counts
    else:
        assert counts["ptmpl_value"] == 4, counts


SELF_SPEC = """
request = [["k", "rel"]]

[target_template.k]
param = ["v"]
kind = "library"
sources = ["common.c"]
private = { "cc.define" = ["V={v}"] }

[target.k]
instance_of = "k"
instance_param = [["v", "VALUE"]]
"""


@needs_souffle
@pytest.mark.parametrize("value", ["x", "{v}{v}"])
def test_self_instantiating_template_fails_fast(tmp_path, value):
    """A template that is its own instance is an error, and its expansion
    must not feed itself (a placeholder-bearing value used to run forever)."""
    proj = project(tmp_path, SELF_SPEC.replace("VALUE", value), FILES)
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from buildlog.engine import configure_project\n"
        "configure_project(Path(sys.argv[1]), Path(sys.argv[2]), emit=False, update_lock=True)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(proj), str(tmp_path / "build")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, err = child.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)  # also the runaway souffle
        child.communicate()
        pytest.fail("template expansion did not terminate")
    assert child.returncode != 0
    assert "target template is also a target" in err, err
