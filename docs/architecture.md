<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Architecture

buildlog derives a build graph with a logic program. A project's spec, the
facts that packages ship, and facts discovered about the machine go in. A
Soufflé Datalog program derives *actions* (commands with typed inputs and
outputs), and a deliberately thin translator serializes them as a Ninja file.
The same derivation answers questions about itself: `why` returns a proof for
any derived fact.

This page describes the components, the data flow, the discovery loop and the
layering. See [user-guide.md](user-guide.md) for the commands,
[rule-authoring.md](rule-authoring.md) for extending the rules and
[proof-protocol.md](proof-protocol.md) for the `why` document.

## Pipeline

```
 spec + packages ──► load ──► facts ──► derive (Soufflé) ──► L0 facts ──► translate ──► build.ninja
                        ▲                     │
                        │                     └─► probe requests
                  discover (probes) ◄──────────┘
                                             derived facts ──► why / query / why-not
```

| stage | input → output | contract |
|---|---|---|
| **load** | spec files, package fact files, discovered facts → one fact set | Set union per relation, no derivation. Every row keeps its origin (file and line) for provenance. Unknown relations are errors. |
| **discover** | probe requests → facts | The only effectful stage. Probes are declared by layers and requested by rules, and their results are plain facts. A probe never decides anything: a probe that depends on a toolchain runs for every candidate. |
| **derive** | fact set → derived relations | A deterministic Datalog program with stratified negation, records, and ADTs for closed algebras. There is no choice: every functional relation is unique or pinned by the user, otherwise it is an error. |
| **translate** | L0 relations → Ninja | "Linearize and serialize, never derive." It orders actions topologically with a canonical tie-break, collapses groups, relativizes path roots and escapes. Every emitted token comes from an L0 fact. |
| **explain** | derived fact → readable proof | A viewer over Soufflé's proofs: sharing, a legend for values, hiding of structural relations. |

Determinism: the output is byte-identical under any permutation of input rows
and of probe order. `buildlog configure --shuffle N --shuffle-probes N` checks
this, and the tests do.

## Components

- `src/buildlog/`: the core distribution.
  - `cli.py`: the command line (`configure`, `check`, `why`, `why-not`, `query`, `test`, `-E`).
  - `facts.py`: the loader. Reads `buildlog.toml` and the spec and user files named there, validates them against the declared relations, expands the authoring sugar, and writes fact files with `file:line` origins.
  - `layers.py`, `packages.py`, `conditions.py`: manifests, package qualification, the load-time checks, package search and the lockfile, and the infix condition syntax.
  - `engine.py`, `discovery.py`, `providers.py`, `souffle_cache.py`: the Soufflé runs, the discovery loop and its caches, and package providers.
  - `emit.py`: the Ninja translator. `builtins.py` is `buildlog -E`. `testrun.py` is `buildlog test`.
  - `provenance/`: `why`, `why-not` and `query` (see the protocol document).
  - `layers/core/`: the core Datalog layers (below).
- `plugins/buildlog-cpp/`: the C-family plugin. It holds the clang and gcc toolchain-family layers, the C and C++ language layer, probes of the machine (toolchain inventory, configure checks, capabilities), and three package providers (`pc`, `cps`, `cmake`) in `buildlog_cpp/importers/`.
- Case-study plugins and packages live under `case-studies/`.
- `tests/plugins/buildlog-testplugins/`: plugin fixtures for the tests.

The Soufflé engine is an implementation detail: users never invoke it. It must
be the patched build described in [the README](../README.md#prerequisites).

## The build directory

`buildlog configure -C B` writes, among other things:

- `B/souffle/build.ninja`, with the actions' outputs under `B/souffle/` (`bin/`, `lib/`, `obj/`, `gen/`), and `B/souffle/facts/`, the derived relations that the emitter, `why` and `buildlog test` read.
- `B/program/`: the composed, qualified Datalog program (`build.dl`, `io.dl`), the relation registry and `sources.json` (package directories, original file paths, probe declarations).
- `B/facts-in/round-N/`: the input facts of the final discovery round, with `source_map.json`.
- `B/probe-cache.json`, `B/probe-answers.json`, `B/rounds/`: the probe cache, the seed for the next configure and memoized rounds (see [the user guide](user-guide.md#caches-and-speed)).
- `B/buildlog-state.json`: the user files, `-D`/`-O` values and `--config` selection, so a reconfigure (including the regeneration edge) repeats them.
- `B/prepared.pkl` and `B/available-certificate.json`: reusable bounded preparation and its content, membership, absence and symlink observations.

## The discovery loop

Discovery is a fixpoint, because what to ask depends on what was found. The
toolchains are themselves discovered, and a probe such as "does this header
compile with this compiler for this platform" depends on them. A typical
configure takes inventory and capability/path-probe rounds,
then the final derivation.

1. The driver derives with the current facts. Rules derive *requests*: typed
   rows such as `req_cc_check(id, ...)`.
2. It checks each request against the probe kind's finite *domain*. A request
   outside the domain is an error.
3. It runs the probes for requests that have no answer yet. Probes see only the
   answers of earlier rounds, so the order in which a round's probes run cannot
   change their answers. Every request must get an answer row (a probe that
   finds nothing says so).
4. It writes the results as facts and repeats until no request is new.

The probe-result directory is updated incrementally: unchanged relations keep
their files, changed relations are rewritten, and relations no longer present
in the answers are removed. Provenance is updated independently of result rows.
The remembered file contents are local to a fixpoint and reset whenever its
result directory is cleared, including a rejected seed replay. This changes
only serialization work, not which answers a round sees or when probes run.

Result rows carry the request id, and rules join results back to requests, so
`why` crosses the discovery boundary. Requests and intermediate models need not
be monotone. Finite request universes and progress in answered identifiers
guarantee fresh termination; there is no round cap.

**Dependency preparation** precedes inference. Providers enumerate finite
layouts within declared prefixes, search directories and files, exposing
includes and rule pointers without probe answers. The strict executable union
is composed once, and metadata becomes immutable available facts. Invalid or
conflicting executable code is fatal even if unused; ordinary malformed metadata
can remain deferred until demanded. A certificate covers content, directory
membership, missing paths and symlink targets, and is confirmed before success.

**Binding** is per (name, platform), so tools and linked libraries can select
different installed instances. Available definitions do not depend on demand;
ambiguous availability establishes existence but demanding an ambiguous binding
fails. Source declarations and aliases take precedence. Module declarations
require binding; effects require use. Visibility alone does not run actions,
activate self-requests or lock modules. Late sysroot paths use guarded
`metadata_path` requests with ordered negative watches; they never add metadata
or executable code.

**Configuration selection.** The spec declares every configuration. The user
selects the ones built (`--config`, or `config_default`), and requests of an
inactive configuration cause no demand or probe. Metadata and code within its
declared roots can still be prepared and executable code must still be valid.

There is no package-driven outer loop. A file, root or rule module outside the
certified preparation is rejected, not activated by restarting inference.

With the patched Soufflé's `--serve`, each fixpoint uses one process: startup
parses, checks and translates the composed program, and each request evaluates
a round with fresh runtime state. Configure uses that startup for the composed
semantic check instead of checking the same program in a separate process.
The check result is cached by the binary, flags and program contents, so a
fully memoized warm configure need not start an evaluator. Per-layer and Python
loader checks still run as before. Standalone layer checks, and configure with
`BUILDLOG_SOUFFLE_SERVE=0` or without `--serve` support, retain the separate
composed check.

With `--serve-deferred-output` (the same session, one `--help` probe per binary
for both capabilities), a round is requested with `defer=` the relations the
loop reads: the request, domain and `discovered_rule_package` relations of the
loaded probe kinds. Soufflé writes those now and keeps the other outputs in the
retained engine. Configure owns this evaluator through the fixed-program
fixpoint and sends `DUMP` before any final-model reader (`config_active`,
state, diagnostics, emission). This completes the same round directory from
that exact evaluation, even if guarded cleanup of unused seeds subsequently
rewrites input facts. Intermediate rounds need not dump their retained outputs;
none can introduce another executable program. Round memos record whether they are
*complete* (every output of the program, validated) or *control*; a control
memo never satisfies a full request, a complete one satisfies both. A control
memo hit has no process to dump, so if its round turns out final the exact input
bytes it was keyed on (kept in memory when hashed) are evaluated again in full.
Without the feature (an older binary, no `--serve`, `BUILDLOG_SOUFFLE_DEFER=0`,
or a path containing a tab or newline) every round writes full output.

Within a fixpoint, unchanged probe provenance reuses its serialized source map
and origin keys; materialization also retains the merged source map when both
its static origins and probe-map text are unchanged. These caches are scoped
to the configure and preserve the source-map bytes. Ninja emission similarly
memoizes path spelling, lexical keys, prefixes and shell quoting for that
emission only. L0 diagnoses conflicting argv tokens by counting distinct
prefix/value pairs at non-set positions, rather than enumerating pairs of
tokens in every set.

## Layering

Layers are Datalog files grouped into packages. Each package has a rank; a
layer may read only its own relations and those of lower packages, and extends
lower layers only through their `open` relations
([rule-authoring.md](rule-authoring.md#layers-visibility-and-checks) states the
rules).

| layer | owns | language-specific? |
|---|---|---|
| **L0** actions and artifacts | Artifacts (rooted paths: `src:`, `out:`), actions (argv, explicit, implicit and order-only inputs, outputs, depfile), ordered argv segments, standard streams, working directory, restat, integrity checks (dangling inputs, duplicate producers, cycles). *Translated* to Ninja. | no |
| **L1** targets, platforms, configurations | Targets and deps, edge kinds (`dep` with visibility, `tool_dep` for "runs during the build"), platforms, configurations and their settings, the `needed(target, config)` closure, the exec transition, conditions, generic property propagation (`prop`, `iface`, `use`) with a visibility algebra. Imported artifacts are targets with given facts. Also configured files and options. | no |
| **F** toolchain families (in `buildlog-cpp`: `F_clang`, `F_gcc`) | How a family's instance is aimed at a platform, how settings are spelled per phase, and the linker's cycle policy. Used by both L3 (link) and L4 (compile). | family-specific |
| **L2** toolchains | The toolchain *entity* (discovered: family, version, tools), *capabilities* (`toolchain_runs_on`, `toolchain_can_target`), derived *instances* (`tc_instance(tc, exec, target)`) and *selection* `tc_select(lang, exec, target, tc)`, unique or pinned. A toolchain the build makes states the same facts from its author and names a target per role (`toolchain_tool_target`); targets choose it by name (`target_toolchain`), and lowering reads `tc_of(t, lang, c, tc)`. | no |
| **L3** native code, discovery, install | Native object, archive, shared, module and executable artifacts, the link closure and order, output names and versions, fat artifacts, imported artifacts; format-neutral package discovery (`L3_discovery.dl`); install and export (`L3_install.dl`). | object-format-specific, language-neutral |
| **V** vocabulary | The authoring vocabulary and generators: short relations that desugar to the lower ones. Part of core. | no |
| **L4** languages (`buildlog-cpp`) | C-family property keys (`cc.include_dir`, `cc.define`, `cxx.std`), lowering of sources to native objects, configure checks, install layout and export formats. | yes |
| **L5** generators, DSLs | Project or case-study rule packages, such as Halide's `halide-build` and Qt's moc rules. | yes |

**Toolchains versus their configurations.** A toolchain (a discovered entity,
for example Apple clang 21) serves one or more languages. Its *capabilities*
come from probes. An *instance* is derived, never declared: `tc_instance(tc,
exec, target)` holds exactly when the toolchain runs on `exec` and can target
`target`. One clang yields many instances. Selection is a function of
`(language, exec, target)` and must be unique or resolved by a pin fact, and
there are no priority lists.

**Configurations** are records `[platform, settings]`. A setting key is
declared by the layer that owns it (for example `native.opt`), has a finite
domain and a default, and has an exec policy: `inherit`, or a forced value for
the exec configuration. The configuration space must not depend on what is
needed, which keeps conditions stratified. Conditions are a boolean algebra
evaluated two-valued without negation: every attribute is a total function over
a finite domain.

**Multi-platform artifacts** (universal binaries) are a *split transition* at
L1, where a request in a multi-platform configuration becomes `needed` in every
slice, and a *merge action* at L3 (`lipo`, a tool of the universal platform).
Imported libraries are data at their simplest: a package ships facts and no rules.

**Exec configuration is inferred from use.** Nothing says that a target is a
host tool. A command names what it runs by target reference, and core derives
that the referenced target is needed in the exec configuration (see "Exec
configuration and tools" in [spec-reference.md](spec-reference.md#exec-configuration-and-tools)).

## Known limitations

- Layers are AST-checked one at a time, so macros from lower layers are not expanded in that view.
- There is no static finiteness check for probe domains; the domain is checked when requests are made.
- Probe results are not regeneration inputs: after a toolchain change, rerun `configure`.
- Errors are attributed per (target, configuration) and per action, not more finely.
- `why-not` explains one rule level at a time and depends on Soufflé's `explainnegation`.
- Dependency solving, incremental re-derivation, IDE project generators and remote execution are out of scope.
