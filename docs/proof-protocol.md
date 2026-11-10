<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# The buildlog proof protocol (version 1)

`buildlog why --format json` prints a **buildlog-proof** document. The
text tree and the HTML viewer render the same document. Other tools can
consume it too. This page specifies version 1. Fields may be added within
a version, and a consumer must ignore fields it does not know. Removing
or changing the meaning of a field bumps `version`.

The implementation is in `src/buildlog/provenance/`. `proof.py` builds
documents, `shape.py` shapes them and `render.py` renders them.
`targets.py`, `query.py` and `whynot.py` implement `--flag/--output`,
`buildlog query` and `buildlog why-not`.

## How a proof is made

`why` never recomposes the program and never re-runs discovery. It reads
these parts of the configured build directory:

- `program/build.dl`: the composed, qualified layers.
- `facts-in/round-<final>/`: the spec, package and probe-result facts of
  the final discovery round, plus `source_map.json`.
- `souffle/facts/`: the derived relations, used for `--flag/--output`
  mapping and for the configuration legend.
- `program/sources.json`, written by `configure`: package directories,
  the original path of every program file, the manifest
  `structural`/`legend` lists and the probe declarations.

It then runs `souffle -t explain` over the same inputs, with `format
json` and `setdepth 100000`. Each `explain` goes to its own file through
the REPL's `output` command, and the driver parses Soufflé's string
labels into typed values.

## Document

```json
{
  "format": "buildlog-proof",
  "version": 1,
  "queries": ["action_arg(\"out:obj/…/main.cpp.o\", 21, \"-D\", \"HAVE_SIMD=1\")"],
  "roots": ["n0"],
  "nodes": {"n0": {"…": "…"}, "n1": {"…": "…"}},
  "rules": {"buildlog-cpp:layers/L4_cxx.dl:23": {"…": "…"}},
  "relations": {"buildlog_cpp__cxx_object": {"…": "…"}},
  "stats": {"raw_bytes": 69697, "raw_tree_nodes": 321, "dag_nodes": 64, "shaped_nodes": 57},
  "shape": {"raw": false, "collapse": true, "hide_structural": true, "depth": null,
            "leaves": false, "path_to": null, "prune_demand": true,
            "with_target_demand": false, "fold": true}
}
```

- `queries[i]` is the atom whose proof is rooted at `roots[i]`. It is the
  fact as the user wrote it, or the tuple that target mapping selected.
- `nodes` maps an id to a node. **Proofs are DAGs.** Soufflé proves every
  occurrence of a tuple the same way, so each distinct label becomes one
  node, and `children` lists child ids. Ids are `n<k>`, numbered in
  depth-first preorder of first occurrence. They are deterministic for a
  given build and query list, and shaping never renumbers them.
- `rules` maps a rule id to its clause. See "Rules".
- `relations` maps each (qualified) relation in the document to
  `{name, package, attributes: [{name, type}], declared: {file, line},
  input, probe?, synthetic?, structural?, demand?, fold?, fold_origins?, elide?,
  flatten?}`. `name` is the relation as its
  package wrote it. `demand` is the probe kind when the relation is that
  probe's `request` or `domain` relation, and `"target"` when a manifest
  lists it under `demand` (see "Answers and demand").
- `stats`:
  - `raw_bytes`: the size of Soufflé's JSON output (the proof plus its rule dump).
  - `raw_tree_nodes`: the node count of Soufflé's tree.
  - `dag_nodes`: the count after sharing.
  - `shaped_nodes`: the count after shaping, excluding collapsed nodes.
- `shape` holds the shaping options. It is absent from unshaped documents.
  Under `why --demand PATTERN` it also has `"demand": PATTERN`.

## Nodes

Every node has `id`, `kind` and `text`. `text` is the tuple in engine
syntax with display names, for example
`cxx_object("app", ["x86_64-macos", …], …)`. Atom nodes also have
`relation` (the qualified name, a key of `relations`) and `tuple` (typed
values, see "Values").

| `kind` | meaning | extra fields |
|---|---|---|
| `derived` | derived by a rule | `rule` (a key of `rules`), `children` |
| `fact` | leaf: an input fact, program fact or aggregate result | `origins`, `structural`? |
| `negation` | `!rel(…)` held when the rule fired | none |
| `constraint` | a builtin constraint that held (`2 > 0`, `"a" != "b"`) | `constraint: {op, lhs, rhs}`, typed operands, when binary |
| `truncated` | Soufflé's own depth limit (`subproof rel(k)`) | none |
| `missing` | `Tuple not found` / `Relation not found` | none |

Shaping adds these fields:

- `via: {child_id: [collapsed ids]}` on a node whose edge skips a
  collapsed chain.
- `collapsed: true` on each skipped node. The node is kept, without
  children, so that `via` resolves.
- `truncated: true` and `hidden_children: n` on nodes cut by `--depth`.
- `demand_pruned: true` on a request or domain node whose subproof was
  pruned (step 3 of "Shaping"). It has no children. The text renderer
  shows `[asked: probe <kind> request; --demand explains why]` in place
  of its rule, and for target demand `[exists here because it is built in
  this configuration; --demand explains why]`.

## Values

Symbols are JSON strings and numbers are JSON numbers. Floats come from
Soufflé's six-decimal output. Structured values are objects:

| Soufflé | JSON |
|---|---|
| `["a", 1]` | `{"record": ["a", 1]}` |
| `nil` | `{"nil": true}` |
| `$Eq("k", "v")` | `{"adt": "Eq", "args": ["k", "v"]}` |
| `$buildlog__Eq(…)` (qualified) | `{"adt": "Eq", "qualified": "buildlog__Eq", "package": "buildlog", "args": […]}` |

Cell types come from the relation declarations. A record-typed cell read
from a CSV, where Soufflé prints bare symbols, still gets its structure.

## Rules

A rule id is `<package>:<path inside the package>:<line>`, for example
`buildlog-cpp:layers/L4_cxx.dl:23`, so ids are stable across machines and
checkouts. A rule entry looks like this:

```json
{"relation": "action_arg", "number": 19, "package": "buildlog-cpp",
 "file": "/abs/…/plugins/buildlog-cpp/src/buildlog_cpp/layers/L4_cxx.dl",
 "line": 23, "source": "action_arg(o, 20 + r, \"-D\", v) :- …",
 "engine_text": "action_arg(o,(20+r),\"-D\",v) :- …"}
```

Soufflé numbers rules per relation and prints no location. `number` and
`engine_text` are exactly what it printed. `package`, `file`, `line` and
`source` come from matching the printed rule against every clause with
the same head. The match is the clause with the most shared tokens (Dice
coefficient), with ties broken by source order. `file` is the original
layer file, not the qualified copy under `program/`, and the renderers
show qualified names in `source` as their package wrote them. Rules that
Soufflé introduced (`+disconnected0`, aggregate subclauses) have no
source and get the id `<relation>#R<number>`.

## Origins

A `fact` node has `origins`. It is a list because one row can come from
several places. Each origin has a `kind`:

| `kind` | fields | from |
|---|---|---|
| `spec` | `file`, `line` | a project TOML or `.facts` row (source map) |
| `package` | `file`, `line`, `package` | a manifest-declared package facts file (source map) |
| `probe` | `probe` (kind), `package`, `entry`, `request` (id), `request_relation`, `request_args` | a discovery result `res_*`, with its `req_*` request row |
| `layer` | `file`, `line`, `package`, `rule` | a fact clause in a layer (`setting_key("native.opt", 0).`) |
| `aggregate` | `file`, `line`, `package`, `rule`, `detail`? | an aggregate result (`nkeys(2)` from a `count`), which Soufflé does not justify further. Engine-introduced aggregate relations (`__agg_single`) name the rule that contains the aggregate |
| `manifest` | `file`, `line`, `package`, `detail` | a driver-written row that a manifest declares: `package_provider("cps")` is the `[[provider]] name = "cps"` line, `target_qualifier(pkg, q)` the package's `name`/`target_namespace` line |
| `driver` | `relation`, `detail` | a row of a core `driver` relation that no file states (`configure_arg`, `configure_input`, `fact_key`, `config_select`, ...); `detail` says what the driver recorded |
| `unknown` | `detail` | none of the above. `detail` says why there is no origin (an input row with no source-map entry, or no clause states the row and the engine gives no subproof). Any `unknown` in the output is a gap to report |

The text renderer prints origins inline, as in
`← examples/cross/spec.toml:81` or
`← probe inventory request "inventory:default"`. The HTML viewer links
`file`/`line` as `file://…#L<line>`.

### Hook for importer origins

The source map (`facts-in/round-N/source_map.json`) maps a fact key
`["rel", [cells…]]` to a list of origins. The loader writes strings
(`"path:line"`). An importer (`.pc`, `.cps` or CMake) can write
**objects** instead. `why` passes any object through unchanged as an
origin, defaulting `kind` to `unknown`. The renderers only interpret
`kind`, `file`, `line` and `package`. For example:

```json
{"[\"prop\", [\"zlib\", \"cc.include_dir\", \"/opt/zlib/include\", \"interface\"]]":
  [{"kind": "pkg-config", "file": "/opt/zlib/lib/pkgconfig/zlib.pc", "line": 7,
    "package": "zlib", "field": "Cflags"}]}
```

This origin renders as `pkg-config /opt/zlib/lib/pkgconfig/zlib.pc:7`,
links like any other file origin, and matches `--path-to pkg-config`,
`--path-to 'zlib.pc:7'` and `--path-to package:zlib`. Keys follow
`facts.tuple_key`: cells as their fact-file text, with structured cells
in fact syntax.

## Shaping

Unless you pass `--raw`, `why` shapes the document. Shaping produces a
smaller document with the same node ids, in these steps:

1. **Splice.** Nodes of engine-synthetic relations (`+disconnected0`,
   `__agg_subclause`, `@…`) are replaced by their children.
2. **Hide.** Structural positive atoms are dropped, with their
   subproofs; see "Structural leaves". Negations stay, because they are
   the causes in error proofs. `--structural` keeps them, and `--raw`
   shapes nothing.
3. **Prune demand.** A derived node of a demand relation that is not a
   root is kept as one line, with its content, and its subproof is
   dropped. Demand relations are a probe's `request` and `domain`
   relations, and the manifest-declared relations of target demand
   (core's `needed`), which are pruned only beneath attributes of an
   action. See "Answers and demand" and "Existence and attributes".
   `--raw` keeps them; `--with-demand` keeps target demand only (below).
4. **Fold** (default; `--full` and `--raw` turn it off). Builtin
   constraint nodes (`1 > 0`), negations of engine-synthetic relations
   and pruned probe requests (the probe's result leaves cite their
   request) are dropped. A package manifest may list relations to
   shape: `fold` (a derived node that is not a root is kept as one line,
   marked `folded`, without its subproof), `fold_origins` (the same, but
   its children are the authored leaves of its subproof: facts with a
   `spec`, `package`, `user` or `driver` origin; for relations whose
   proof concerns one declaration), `elide` (dropped with its subproof)
   and `flatten` (a child of the same relation is spliced, so a recursion
   over a list is one node). Core lists its configuration construction:
   `fold` has `config`, `config_setting`, `exec_config` and the
   declaration steps, `fold_origins` has `decl_cfg`, `elide` has
   `cfg_name`, `config_platform`, `settings_suffix`, `cond_sub` and the
   like, and `flatten` has `exec_settings`. A dropped premise still
   counts when collapsing plumbing, so folding never merges a rule into
   its neighbour. Under `--leaves` and `--path-to` only the builtins,
   engine negations and `elide` rows are dropped, so every origin fact of
   a reason stays reachable. An `elide` row is a guard over a term the
   proof already names (`cond_sub(e)` for a subterm of a condition,
   `config_platform`, `settings_suffix`); its subproof is whichever other
   declaration the engine chose as a witness for that term (another
   configuration, another condition naming the same option), which is
   not a reason. `--full` keeps them.
5. **Collapse plumbing.** A derived node that is not a root and has
   exactly one premise after steps 1–4 is plumbing. Its fact follows from
   that one premise, so it adds a rule name but no new reason. Edges
   skip to the end of such a chain, and the skipped nodes are recorded in
   `via`, shown as `(via settings_suffix ← settings_suffix)`. The
   criterion is structural and uses no list of relation names. Renaming,
   projection and re-keying rules collapse. Joins, and rules with a
   negation or constraint premise, stay, because every premise is a
   reason. A package can hide a multi-premise relation by declaring it
   `structural`.
6. **Focus.**
   - `--leaves` gives each root the sorted list of its origin facts. A
     pruned demand node counts as one, except a probe request whose
     result is listed: the result's origin cites it, as in the tree.
     Leaves one authored row states (a sugar row and the facts it
     desugars to, such as `source_set_when` and its `condition`) share a
     line, `(+ condition, same row)`, as in the tree.
   - `--path-to PATTERN` keeps only the nodes on chains that reach
     matching leaves. `PATTERN` is an `fnmatch` glob, or a substring
     when it has no glob characters. It is tested against the leaf's
     text, its relation name (qualified or as written) and its origin
     labels: `file:line` and `basename:line` (`spec.toml:12`),
     `probe:<kind>` and `probe:<kind>:<request>`, `<kind>:` (`layer:`,
     `spec:`) and `package:<name>`. A pruned demand node also matches
     `demand:` and `probe:<kind>` (`demand:target` for target demand).
7. **Depth.** `--depth N` expands nodes at most N edges below a root.
   Traversal is breadth-first, so a shared node is expanded at its
   shallowest use. Nodes at the limit are marked `truncated`, with a
   count in `hidden_children`.

## Structural leaves

`why` explains why a fact holds, and some leaves of every proof are not
reasons: they declare the vocabulary the rules are written in. That the
setting key `native.opt` sits at position 0, that `"private"` is a
visibility, that `nkeys` counts two keys: no user or author decision
produced them, and they are the same in every build. They are hidden by
default. The rule has two parts, and one field (`structural` on a
relation entry or a node) that shaping reads:

1. **Core ground facts.** A leaf that is a ground fact stated in a
   *core* layer (package `source = "core"`) is structural. The core states
   vocabulary (`used`, `exported`, `builtin_command`, `test_regex_kind`,
   `set_key`), registries (`setting_key`, `setting_value`,
   `setting_default`, `setting_exec_value`) and base cases of its own
   algebras (`exec_settings(nil, nil)`, `settings_name(nil, "")`). Core
   holds no knowledge of a toolchain, a platform or a project, so a fact
   it states never explains an attribute of the build. The proof of the
   node says `structural: true`.
2. **Declared relations.** A manifest lists relations under
   `structural = [...]` whose every row is vocabulary or bookkeeping.
   Core lists `nkeys` (a `count` over the registry, a check) and
   `action_core`/`dep_core`, the cores the cycle checks restrict
   reachability to (a filter that keeps every node of a cycle, so a
   cycle error's proof is the cycle's edges). buildlog-cpp
   lists `set_key`, `lattice_rank`, `cc_check_kind` and `cxx_abi_key`.
   Every positive atom of a listed relation is dropped, with its subproof
   (Halide's `halide_runtime_piece` is a derived example).

What stays: everything an author, a user or a probe stated, wherever it
is stated, and plugin knowledge that carries meaning, such as
`family_setting_arg("clang", "compile", "native.opt", "release",
"-DNDEBUG")` (why release spells `-DNDEBUG`), `cc_default_warning` and
`cc_include_class` (why `-isystem` is spelled so). A plugin's vocabulary
is hidden only where its manifest declares it.

Decisions on the rows that the noisy `--leaves` output listed:

| row | shown? | why |
|---|---|---|
| `nkeys(2)` | hidden | a count that checks the registry |
| `exec_settings(nil, nil)`, `settings_name(nil, "")` | hidden | base cases of core's list recursions |
| `setting_key`, `setting_value` | hidden | the registry of setting keys and domains |
| `setting_default("native.sanitize", "none")` | hidden | core default; not the reason for `-DNDEBUG`. The build's configuration record includes every key, so every proof reaches it |
| `setting_exec_value(...)` | hidden | core's exec policy is a registry row. `--structural` shows the reason a host tool is optimized |
| `used("private")`, `exported(...)` | hidden | visibility vocabulary |
| `set_key("cc.define")`, `cc_check_kind("compiles")` | hidden | plugin vocabulary, declared `structural` |
| `family_setting_arg(...)` | shown | plugin knowledge: it is the reason for the flag |
| `config_decl_setting("release", "native.opt", "release")` | shown | the author's or user's decision |

Hiding is presentation only. A structural fact that is a *root* of the
query is shown (`why 'setting_key(k, p)'`), and `--structural` or `--raw`
brings every leaf back. A derived node whose premises are all hidden
looks like a leaf in the tree, and does not count as one in `--leaves`.

## Answers and demand

A probe result is joined through the probe's request in most rules
(`res_cc_check(id, …), req_cc_check(id, …)`), and the request is derived
from demand: the configurations, targets and requests that need the
answer. An unshaped proof of a flag therefore mixes two explanations:
why the answer holds, and why the question was asked. In zlib, the proof
of `-DHAVE_HIDDEN` on `adler32.c.o` reached `c_source("minigzip", …)`
and `request_all("minigzip")`, because minigzip happens to be the C
target through which the C toolchain was required.

The probes' demand relations are identified from the probe
declarations, not listed by hand: every `[[probe]]` names a `request`
and a `domain` relation, and those are the demand relations. The one
other kind, target demand, is declared by a manifest (`demand = [...]`,
see "Existence and attributes"). `tc_required` and the like are ordinary
relations, because above a probe they are often the answer (why is there
no toolchain error).

- **Default `why`** prunes demand only where it is demand: at request and
  domain nodes, that is, beneath the answers that need them. Roots are
  never pruned, and when every root is a demand node (`why
  'req_cc_check(…)'`), nothing is pruned. The answer's own provenance,
  the probe result with its origin (probe kind, request id, cache), stays.
- **`why --demand PATTERN …`** asks the other question. It builds the
  answer's proof as usual, collects every probe request that proof relies
  on (its pruned request and domain nodes, and the request id in every
  probe-result origin, since a rule may consume a result without joining
  its request, as `import_candidate` consumes `res_pkg_find`), keeps
  those that match `PATTERN`, and explains them. `PATTERN` matches the
  request atom's text, its relation name or `probe:<kind>`, as
  `--path-to` does; `*` selects all. The result is an ordinary document
  whose roots are request atoms. It is an error when no request matches.

### Existence and attributes

The same distinction applies to actions. `-DNDEBUG` is on greet's object
because `native.opt` is `release` and the clang family spells release
that way. That the object exists at all, because `hello` depends on
`greet` in the `release` configuration and the user requested `hello`,
is demand: a proof of the flag that lists `request("hello", "release")`
and `dep("hello", "greet", "private")` answers a second question.

Core's manifest declares `demand = ["needed"]`: `needed(t, c)` says that
target `t` is built in configuration `c`, and the rules guard everything
that a target contributes on it. It is a demand relation of kind
`"target"`. The rule:

- Beneath an **attribute of an action**, the `needed` node is kept as
  one line and its subproof (the request and dependency chain) is pruned.
  Attributes are the L0 relations `action_<x>` other than `action_out`:
  `action_arg`, `action_seq`, `action_arg_basis`, `action_in`, ... These
  are what `--flag` selects. A proof of an attribute presupposes the
  action.
- Pruning applies only when **every root** is such an attribute. `why
  --output PATH` alone has the roots `action` and `action_out`, which are
  the action's existence, so it shows the request and dependency chain in
  full, as does asking for `needed(t, c)` itself. Roots are never pruned.
  A root that is neither (`use(...)`, `cxx_object(...)`) prunes no target
  demand: only actions have a designated existence relation.
- `why --demand PATTERN` also explains the pruned `needed` nodes. It
  collects them with the probe requests (`asked_targets`) and explains
  each as a root, so nothing is pruned in that proof. `PATTERN` matches
  the atom's text, its relation (`needed`) or `demand:target`.
- `--with-demand` keeps the target demand subproofs in an ordinary `why`
  (why the target is built), and `--raw` keeps every demand subproof.
  Probe requests stay pruned under `--with-demand`: their subproof says
  why a question was asked, and a probe's demand guard is existential
  (the C capability probe is asked because *some* C target is needed), so
  its witness is the engine's choice of target, not the one the proof is
  about. `--demand PATTERN` explains them.

The probe rule ("Answers and demand") is unconditional on the roots, and
this one is not: a probe request is never the answer to a question about
an answer, and a target's demand is the answer to a question about its
existence. That is why the two share one pruning mechanism but not one
trigger.

This is the distinction of the magic-sets (demand) transformation: a
magic or demand fact records why a subgoal was asked, not why it holds,
and the answer's proof does not depend on it.

## Targets: `--flag` and `--output`

`--output PATH` selects the actions that produce `PATH`, using the
derived `action` and `action_out` relations. `PATH` is a Ninja path,
relative to `<builddir>/souffle` or absolute.

`--flag TOKEN` selects the `action_arg` or `action_seq` tuples whose
emitted spelling is `TOKEN`. The spelling is the emitter's own:
`prefix + value`, with `out:` stripped and `src:` matched by path suffix.

- Both options together select a token on one action's command line.
- `--output` alone explains the action itself: its existence, with the
  request and dependency chain that makes it be built.
- `--flag` alone explains every command line that has the token, up to
  `--max-targets` (default 8).

Python does no derivation here: it reads the tuples from
`souffle/facts/`. When nothing matches, the error suggests the closest
tokens or paths.

## why-not

`buildlog why-not FACT` prints text or, with `--format json`, a
**buildlog-why-not** document. It uses Soufflé's `explainnegation`, which
explains one rule at a time, one level deep, and prompts for every
variable that the head does not bind. The prompts are printed only on a
terminal, so the session runs on a pty. For each rule whose head can
match the tuple:

- Head variables are bound from the tuple, and a head term like `(20+r)`
  is solved for `r`.
- The other variables get a witness: the first answer (via `query`) of
  the longest prefix of the rule body that has one.
- `--bind var=value` overrides any variable, and `--rule N` restricts the
  rules considered.

The document has these fields:

- `holds`: `true` means the fact is derived, and `why` applies.
- `rules`: a list of
  `{number, rule, bindings, literals: [{text, kind, relation, tuple, holds}], first_failure}`.
- `skipped`: the rules whose head constants cannot match the fact.

The text output ends with the next command to try, for example
`next: buildlog why-not 'use(…)'`.

## Soufflé requirement

buildlog needs the patched Soufflé, built with `-DSOUFFLE_DOMAIN_64BIT=ON`
from the `main` branch of `https://github.com/adobe-research/buildlog-souffle.git`
(see [the README](../README.md#prerequisites)). The patches sit on
upstream master `a1303be3c`. They give `explain` structured values: ADT head arguments, records and ADTs in
proofs and in query and constraint values, and negation. The fixes below
came out of this workstream. Older builds fail `why` on symbols that
contain quotes, report wrong `why-not` verdicts for rules with head
constants, and abort `query` on ADT-typed columns.

| Issue | Commit | Soufflé test |
|---|---|---|
| explainnegation misaligned variables when a rule head had a constant, so literals that hold were marked `x` | `efc8aa2` | `provenance/explainnegation_head_args` |
| `explain` rejected symbols containing escaped quotes | `57c274a` | `provenance/explain_escaped_symbols` |
| JSON proofs did not escape backslashes in symbols | `40ec672` | `provenance/explain_json_escaping` |
| Rule and measure output and explainnegation's early-exit messages were printed only on a tty | `a5bcc46` | `provenance/explain_noninteractive` |
| The REPL's `query` printed symbols unquoted | `c885883` | `provenance/query_symbol_values` |
| `IO=jsonfile` output aborted on ADT-typed columns | `2279adb` | `semantic/jsonfile_adt` |

Two REPL behaviours are by design and remain:

- `explainnegation` prompts for rules and variables only on a terminal,
  so `why-not` runs its session on a pty.
- The REPL's `query` sorts variables by name and pages its results
  interactively with `;`. `buildlog query` compiles a one-clause program
  instead and reads its `IO=jsonfile` output.

buildlog writes fact files verbatim, and Soufflé reads them verbatim, so a
symbol Soufflé holds is exactly the TOML value (a value containing a tab or
line break is rejected), and leaf origins are matched against it.

## Measurements

These were taken on an Apple-silicon Mac with the configured
`examples/cross` and `tests/projects/halide/bilateral_grid` builds, one
target per row. The columns:

- raw: Soufflé's JSON output, which is the proof tree plus the dump of
  all ~230 rules that every `explain` prints.
- DAG: the node count after sharing.
- shaped: the default `why`, with collapse on.
- JSON: the shaped document, compact.
- text: the default text tree.

| build | target | raw KB | raw tree nodes | DAG nodes | shaped nodes | leaves | shaped JSON KB | text lines | text KB | explain s | build+shape ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cross | `-DHAVE_SIMD=1` @ x86_64 `main.cpp.o` | 68.1 | 321 | 64 | 57 | 20 | 42.8 | 85 | 7.2 | 0.57 | 6.8 |
| cross | `-DCORE_CHECKED=1` @ debug `core.cpp.o` | 59.4 | 232 | 57 | 46 | 20 | 38.2 | 58 | 5.3 | 0.54 | 7.2 |
| cross | `-fsanitize=address` @ debug `main.cpp.o` | 75.3 | 392 | 96 | 85 | 31 | 60.8 | 134 | 12.0 | 0.59 | 7.5 |
| cross | `-std=c++17` (first match) | 54.5 | 198 | 50 | 43 | 19 | 31.0 | 52 | 4.5 | 0.52 | 6.1 |
| cross | `-DNDEBUG` @ x86_64 `core.cpp.o` | 77.3 | 407 | 87 | 75 | 28 | 58.7 | 112 | 10.3 | 0.60 | 7.2 |
| cross | `--output bin/universal2…/app` (LIPO) | 46.5 | 136 | 35 | 31 | 12 | 21.8 | 40 | 3.3 | 0.53 | 6.6 |
| bilateral_grid | `-DHALIDE_WITH_EXCEPTIONS` | 131.9 | 703 | 76 | 67 | 26 | 50.3 | 98 | 9.7 | 1.03 | 10.9 |
| bilateral_grid | `-std=c++17` @ `filter.cpp.o` | 65.2 | 186 | 55 | 50 | 20 | 33.4 | 71 | 6.4 | 0.80 | 10.4 |
| bilateral_grid | `autoscheduler=Mullapudi2016` | 61.4 | 139 | 49 | 44 | 17 | 30.6 | 65 | 6.0 | 0.81 | 10.0 |
| bilateral_grid | `target=arm-64-macos-no_runtime` | 65.2 | 178 | 57 | 51 | 20 | 36.5 | 75 | 6.8 | 0.80 | 10.9 |
| bilateral_grid | `--output lib/universal2…/libbilateral_grid.a` | 59.7 | 132 | 36 | 31 | 13 | 22.6 | 39 | 3.6 | 0.79 | 9.9 |

Sharing does most of the reduction. The HALIDE_WITH_EXCEPTIONS proof
shrinks from a 703-node tree to 76 DAG nodes, and collapsing removes
another 10–20%. `--leaves` output is 12–31 lines, and it is usually the
answer people want. Nearly all the time goes to Soufflé evaluating the
program in provenance mode (0.5–1.0 s), and the Python side takes about
10 ms. End-to-end times:

- `buildlog why`: 0.7 s on cross, 1.1 s on bilateral_grid.
- `buildlog query`: 0.18 s.
- `why-not`: 1.7 s (one REPL session plus a few witness queries).
