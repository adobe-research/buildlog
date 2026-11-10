# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import configure_project
from .errors import BuildlogError, LoaderError
from .facts import read_rows


def _builddir(ns) -> Path:
    return Path(ns.builddir or (Path(ns.project) / "buildlog-build"))


def _var_overrides(ns) -> dict[str, str] | None:
    raw = getattr(ns, "define", None)
    if raw is None:
        return None
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item or not item.split("=", 1)[0]:
            raise BuildlogError(f"-D expects NAME=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _user_files(ns) -> list[Path] | None:
    """--user files and --preset tables, in that order; None if neither."""
    users = getattr(ns, "user", None)
    presets = getattr(ns, "preset", None)
    if users is None and presets is None:
        return None
    from .facts import preset_path

    return [Path(p) for p in users or []] + [
        preset_path(Path(ns.project), name) for name in presets or []
    ]


def _option_overrides(ns) -> dict[str, str] | None:
    raw = getattr(ns, "option", None)
    if raw is None:
        return None
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item or not item.split("=", 1)[0]:
            raise BuildlogError(f"-O expects NAME=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def cmd_configure(ns) -> int:
    project = Path(ns.project)
    configure_project(
        project,
        _builddir(ns),
        shuffle_seed=ns.shuffle,
        shuffle_probes=ns.shuffle_probes,
        package_paths=[Path(p) for p in ns.package_path or []],
        update_lock=ns.update_lock,
        keep_going=ns.keep_going,
        configs=ns.config,
        user_files=_user_files(ns),
        var_overrides=_var_overrides(ns),
        option_overrides=_option_overrides(ns),
        fresh=ns.fresh,
    )
    print(f"wrote {_builddir(ns) / 'souffle' / 'build.ninja'}")
    return 0


def cmd_check(ns) -> int:
    try:
        facts, _ = configure_project(
            Path(ns.project),
            _builddir(ns),
            emit=False,
            check_only=True,
            package_paths=[Path(p) for p in ns.package_path or []],
            update_lock=ns.update_lock,
            configs=ns.config,
            user_files=_user_files(ns),
            var_overrides=_var_overrides(ns),
            option_overrides=_option_overrides(ns),
        )
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    from .engine import error_order

    errors = read_rows(facts / "error.csv")
    for kind, detail in sorted(errors, key=error_order):
        print(f"error: {kind}: {detail}")
    return 1 if errors else 0


def _ensure_configured(ns) -> tuple[Path, Path]:
    builddir = _builddir(ns)
    facts = builddir / "souffle" / "facts"
    program = builddir / "program" / "build.dl"
    if not facts.exists() or not program.exists():
        configure_project(
            Path(ns.project),
            builddir,
            emit=False,
            package_paths=[Path(p) for p in ns.package_path or []],
            user_files=_user_files(ns),
            var_overrides=_var_overrides(ns),
            option_overrides=_option_overrides(ns),
        )
    metrics_path = builddir / "metrics.json"
    if metrics_path.exists():
        final_round = json.loads(metrics_path.read_text()).get("final_round", 0)
        in_facts = builddir / "facts-in" / f"round-{final_round}"
    else:
        in_facts = builddir / "facts-in" / "round-0"
    return in_facts, program


def _model(ns):
    from .provenance.souffle import load_model

    _ensure_configured(ns)
    return load_model(_builddir(ns))


def _split_positionals(ns, subjects: bool = False) -> list[str]:
    """`why`/`why-not` take atoms and an optional project directory; `why`
    also takes a subject (a target, source file or output) that is not a
    directory."""
    atoms = [a for a in ns.args if "(" in a]
    rest = [a for a in ns.args if "(" not in a]
    if subjects:
        ns.subjects = [a for a in rest if not Path(a).is_dir()]
        rest = [a for a in rest if Path(a).is_dir()]
    if len(rest) > 1:
        raise BuildlogError(f"expected at most one project directory, got {' '.join(rest)}")
    ns.project = rest[0] if rest else "."
    return atoms


def _engine_atom(model, text: str) -> str:
    from .provenance.query import _Compiler
    from .provenance.values import ValueSyntaxError, explain_text, parse_atom

    qualified = _Compiler(model).qualify(text.strip().rstrip("."))
    try:
        rel, values, negated = parse_atom(qualified)
    except ValueSyntaxError as exc:
        raise BuildlogError(f"cannot parse {text!r}: {exc}") from exc
    if negated:
        raise BuildlogError(f"why explains positive atoms, got {text!r}; try why-not")
    info = model.index.relations.get(rel)
    if info and len(info.attrs) != len(values):
        raise BuildlogError(
            f"{model.index.display(rel)[0]} has arity {len(info.attrs)}, "
            f"but {len(values)} argument(s) were given"
        )
    return f"{rel}(" + ", ".join(explain_text(v) for v in values) + ")"


def cmd_why(ns) -> int:
    import time

    from .provenance.proof import build_proof, legend, request_atom
    from .provenance.render import render_html, render_leaves, render_text
    from .provenance.shape import (
        Shape,
        ShapeError,
        asked_requests,
        asked_targets,
        request_matches,
        shape,
    )
    from .provenance.souffle import explain
    from .provenance.targets import config_filter, find_targets, is_target, needed_goals

    atoms = _split_positionals(ns, subjects=True)
    if len(ns.subjects) > 1 or (ns.subjects and ns.output is not None):
        raise BuildlogError(f"why takes one target or output, got {' '.join(ns.subjects)}")
    output = ns.subjects[0] if ns.subjects else ns.output
    if not atoms and ns.flag is None and output is None:
        raise BuildlogError(
            "why needs a target, a fact such as 'request(\"app\", c)', or --flag/--output"
        )
    model = _model(ns)
    queries = list(atoms)
    engine_atoms = [_engine_atom(model, a) for a in atoms]
    if ns.subjects and ns.flag is None and is_target(model, output):
        goals = needed_goals(model, output, config_filter(model, ns.config))
        if not goals:
            where = f" in {ns.config}" if ns.config else ""
            raise BuildlogError(f"{output} is not built{where}; try why-not 'needed(...)'")
        queries += goals
        engine_atoms += goals
    elif ns.flag is not None or output is not None:
        targets = find_targets(model, ns.flag, output, ns.config)
        if len(targets) > ns.max_targets:
            listing = "\n  ".join(t.atom for t in targets[:20])
            raise BuildlogError(
                f"{len(targets)} tuples match; narrow with --output or raise --max-targets:"
                f"\n  {listing}"
            )
        queries += [t.atom for t in targets]
        engine_atoms += [t.atom for t in targets]
    raws = explain(model, engine_atoms)
    t0 = time.perf_counter()
    doc = build_proof(model, queries, raws)
    if ns.demand is not None:
        # The other question: why were the probes behind these answers asked?
        answer = shape(doc, Shape(collapse=not ns.no_collapse, fold=False))
        queries = []
        for kind, req_id in asked_requests(answer):
            atom = request_atom(model, kind, req_id)
            if atom and atom not in queries and request_matches(kind, atom, ns.demand):
                queries.append(atom)
        for atom in asked_targets(answer):
            if atom not in queries and request_matches("target", atom, ns.demand):
                queries.append(atom)
        if not queries:
            raise BuildlogError(f"no probe request matching {ns.demand!r} under the proof")
        doc = build_proof(model, queries, explain(model, queries))
    t1 = time.perf_counter()
    opts = Shape(
        raw=ns.raw,
        collapse=not (ns.no_collapse or ns.raw),
        hide_structural=not (ns.raw or ns.structural),
        with_target_demand=ns.with_demand,
        depth=ns.depth,
        leaves=ns.leaves,
        path_to=ns.path_to,
        fold=not ns.full,
    )
    try:
        focused = ns.depth is not None or ns.leaves or ns.path_to is not None
        shaped = doc if ns.raw and not focused else shape(doc, opts)
        if ns.demand is not None and "shape" in shaped:
            shaped["shape"] = {**shaped["shape"], "demand": ns.demand}
    except ShapeError as exc:
        raise BuildlogError(str(exc)) from exc
    t2 = time.perf_counter()
    names = legend(model)
    if ns.html:
        title = "buildlog why " + ("--demand " if ns.demand is not None else "")
        Path(ns.html).write_text(render_html(shaped, names, title=title + " ".join(queries)))
        print(f"wrote {ns.html}", file=sys.stderr)
    if ns.format == "json":
        text = json.dumps(shaped, indent=2) + "\n"
    elif ns.leaves:
        text = render_leaves(shaped, names)
    else:
        text = render_text(shaped, names)
    t3 = time.perf_counter()
    if not ns.html or ns.format == "json" or ns.print:
        sys.stdout.write(text)
        if output is not None and ns.format != "json":
            from .stratum import dynamic_edges

            lines = dynamic_edges(_builddir(ns), output.removeprefix("out:"))
            if lines:
                sys.stdout.write(
                    "\nDynamic edges, from the build-time collation:\n" + "\n".join(lines) + "\n"
                )
    if ns.stats:
        stats = dict(shaped.get("stats", {}))
        stats.update(
            souffle_explain_s=round(model.timings.get("souffle_explain_s", 0.0), 3),
            build_s=round(t1 - t0, 4),
            shape_s=round(t2 - t1, 4),
            render_s=round(t3 - t2, 4),
            output_bytes=len(text.encode()),
        )
        print(json.dumps(stats, sort_keys=True), file=sys.stderr)
    return 0


def cmd_query(ns) -> int:
    from .provenance.proof import legend
    from .provenance.query import render_table, result_json, run_query

    model = _model(ns)
    result = run_query(model, ns.pattern)
    if ns.format == "json":
        print(json.dumps(result_json(model, result), indent=2))
    else:
        sys.stdout.write(render_table(model, result, legend(model)))
    return 0


def cmd_why_not(ns) -> int:
    from .provenance.whynot import dumps, render_text, why_not

    atoms = _split_positionals(ns)
    if len(atoms) != 1:
        raise BuildlogError("why-not takes exactly one fact")
    model = _model(ns)
    doc = why_not(model, atoms[0], rule_numbers=ns.rule, binds=ns.bind)
    if ns.format == "json":
        sys.stdout.write(dumps(doc))
    else:
        sys.stdout.write(render_text(model, doc, verbose=ns.verbose))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["-E"]:
        from .builtins import main as builtin_main

        return builtin_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="buildlog", epilog="buildlog -E <command>: see buildlog -E"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    package_path_help = (
        "prefix whose share/buildlog/<name>/ holds rule packages (repeatable; "
        "searched with BUILDLOG_PACKAGE_PATH and the project's packages/)"
    )

    def common(p):
        p.add_argument("-C", "--builddir")
        p.add_argument("--package-path", action="append", help=package_path_help)
        p.add_argument(
            "--update-lock",
            action="store_true",
            help="accept changed package versions, sources or hashes in buildlog.lock",
        )
        p.add_argument(
            "--config",
            action="append",
            metavar="NAME",
            help="build this declared configuration (repeatable; remembered by the build "
            "directory; default: the user's config_default, else every declared configuration)",
        )
        p.add_argument(
            "--user",
            action="append",
            metavar="FILE",
            help="load an opt-in user fact file (.toml, .facts or fact-only .dl); repeatable",
        )
        p.add_argument(
            "--preset",
            action="append",
            metavar="NAME",
            help="load buildlog.toml's [presets.NAME] as a user file; repeatable",
        )
        p.add_argument(
            "-D",
            dest="define",
            action="append",
            metavar="NAME=VALUE",
            help="override a [vars] value for this build directory (recorded for regeneration)",
        )
        p.add_argument(
            "-O",
            dest="option",
            action="append",
            metavar="NAME=VALUE",
            help="set a project option (option_value) for this build directory "
            "(recorded for regeneration)",
        )
        p.add_argument("project", nargs="?", default=".")

    configure = sub.add_parser("configure")
    common(configure)
    configure.add_argument("--shuffle", type=int)
    configure.add_argument("--shuffle-probes", type=int)
    configure.add_argument(
        "--fresh",
        action="store_true",
        help="run every probe: read no seed and no probe-cache row (rows are still written)",
    )
    configure.add_argument(
        "--keep-going",
        action="store_true",
        help="exit 0 when errors only break some targets (their edges fail in ninja)",
    )
    configure.set_defaults(func=cmd_configure)

    check = sub.add_parser("check")
    common(check)
    check.set_defaults(func=cmd_check)

    def model_args(p):
        p.add_argument("-C", "--builddir")
        p.add_argument("--package-path", action="append", help=package_path_help)
        p.add_argument("--format", choices=["text", "json"], default="text")

    query = sub.add_parser("query", help="run an ad-hoc Datalog query over the derived model")
    model_args(query)
    query.add_argument("pattern", help="a rule body, e.g. 'link_file(t, c, f), !imported(t)'")
    query.add_argument("project", nargs="?", default=".")
    query.set_defaults(func=cmd_query)

    why = sub.add_parser("why", help="explain a derived fact or a Ninja command-line token")
    model_args(why)
    why.add_argument(
        "args", nargs="*", metavar="FACT|PROJECT", help="facts to explain, then the project"
    )
    why.add_argument(
        "--flag",
        help="a token of a command line in build.ninja, e.g. -DNDEBUG; "
        "or its basename (clang++) or a prefix (--spirv-core-grammar)",
    )
    why.add_argument(
        "--output",
        help="a Ninja output path (alone: the action's own tuples), "
        "a target, a source file or an output's basename",
    )
    why.add_argument(
        "--config",
        metavar="NAME",
        help="only this configuration: a name as printed (arm64-macos.release.none), "
        "a declared configuration (release) or a prefix (arm64-macos)",
    )
    why.add_argument("--max-targets", type=int, default=8)
    why.add_argument("--depth", type=int, help="expand at most N levels below each root")
    why.add_argument("--no-collapse", action="store_true", help="keep single-premise chains")
    why.add_argument("--raw", action="store_true", help="no splicing, hiding or collapsing")
    why.add_argument(
        "--structural",
        action="store_true",
        help="also show structural leaves (vocabulary, registries and bookkeeping)",
    )
    why.add_argument(
        "--with-demand",
        action="store_true",
        help="keep the subproofs of target demand (why targets are built); "
        "--demand explains why probes were asked",
    )
    why.add_argument(
        "--full",
        action="store_true",
        help="do not fold configuration construction, builtin constraints and engine negations",
    )
    why.add_argument("--leaves", action="store_true", help="only the origin facts")
    why.add_argument("--path-to", metavar="PATTERN", help="only chains reaching matching leaves")
    why.add_argument(
        "--demand",
        metavar="PATTERN",
        help="explain why the probe requests under the proof were asked, not the answer; "
        "PATTERN selects requests as --path-to selects leaves ('*' for all)",
    )
    why.add_argument("--html", metavar="FILE", help="write a self-contained HTML viewer")
    why.add_argument("--print", action="store_true", help="with --html, also print the proof")
    why.add_argument("--stats", action="store_true", help="print sizes and timings to stderr")
    why.set_defaults(func=cmd_why)

    why_not = sub.add_parser("why-not", help="explain why a fact is not derived")
    model_args(why_not)
    why_not.add_argument("args", nargs="*", metavar="FACT|PROJECT")
    why_not.add_argument("--rule", type=int, action="append", help="only this engine rule number")
    why_not.add_argument("--bind", action="append", metavar="VAR=VALUE", help="fix a rule variable")
    why_not.add_argument("-v", "--verbose", action="store_true", help="list non-matching rules")
    why_not.set_defaults(func=cmd_why_not)

    from .testrun import add_parser as add_test_parser

    add_test_parser(sub)

    # `--flag -DNDEBUG`: the value looks like an option to argparse.
    for i, arg in enumerate(argv[:-1]):
        if arg == "--flag" and argv[i + 1].startswith("-"):
            argv[i : i + 2] = [f"--flag={argv[i + 1]}"]
            break
    ns, extra = parser.parse_known_args(argv)
    if extra:
        # Facts and the project may come after options (`why --flag X project`).
        if hasattr(ns, "args") and not any(e.startswith("-") for e in extra):
            ns.args += extra
        else:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return ns.func(ns)
    except LoaderError as exc:
        for error in exc.errors:
            print("error:", error, file=sys.stderr)
        return 1
    except BuildlogError as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
