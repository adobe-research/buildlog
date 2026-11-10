<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Rule authoring

How to extend buildlog: packages, plugins, Datalog layers, probes and package
providers, and how provenance (`why`) sees your rules. Read
[architecture.md](architecture.md) first for the layering and the discovery
loop. The facts a spec can state are in [spec-reference.md](spec-reference.md).

## Packages and plugins

`buildlog` itself is only the core: L0 (actions), L1 (targets, platforms, configurations, conditions, property propagation), L2 (toolchain selection), L3 (native code: objects, archives, shared, module and object libraries, executables, output names (`native_output_name`, CMake's OUTPUT_NAME) and shared-library versions (`native_version`; Mach-O compatibility version from the soversion, current version from the version), link order and build-tree rpaths, whole archives, fat artifacts (executables, static and shared libraries; a merged shared library gets its links), imported artifacts) and format-neutral package discovery (`L3_discovery.dl`: which names are wanted, where to look, selection, the relay of provider results) in `src/buildlog/layers/core`, plus the loader, discovery, emitter and CLI. It knows nothing about C or any package metadata format. Everything else is a package:

- **Tier 1, plugins**: Python distributions registered in the `buildlog.plugins` entry point group; the entry resolves to the plugin's manifest directory. `plugins/buildlog-cpp` (the clang toolchain inventory, clang family spellings, L4 C++ and C (a link whose closure is C only uses the C driver, via core's open `link_role`), macOS/clang probes, and the `pc`/`cps`/`cmake` package providers) is one. Core (L0–L3: actions, targets, toolchain selection, native code, package discovery) and plugins may declare probes and (plugins) `[[provider]]`s; rule packages may not.
- **Tier 2, rule packages**: a manifest, `.dl` layers and facts, no Python. Found at `<prefix>/share/buildlog/<name>/manifest.toml` for each prefix in `BUILDLOG_PACKAGE_PATH` (colon-separated) and `--package-path`, and in the project's `packages/<name>/`. A package found under a prefix may write `${PACKAGE_PREFIX}` in its facts files for that prefix (CPS's `@prefix@`), so a library can ship rules and usage facts that stay valid wherever it is installed (`case-studies/qt-moc`: Qt's moc/rcc rules for its consumers).

Worked examples in the repository:

- `plugins/buildlog-cpp/`: a tier-1 plugin (probes, providers, F/L2/L4 layers).
- `case-studies/qt-moc/share/buildlog/qt/`: a tier-2 rule package that Qt would
  ship next to its libraries; it adds a rule so that a target compiling against
  QtCore has its headers run through moc.
- `examples/cross/packages/blurgen/`: a project-local rule package (Datalog
  rules and a manifest, no Python).
- `tests/fixtures/rule-packages/` and `tests/plugins/buildlog-testplugins/`:
  small packages and plugins that the tests exercise, including the loader's
  error cases.

## Manifest schema

**Manifest schema** (`manifest.toml`):

| key | meaning |
|---|---|
| `name`, `version`, `rank` | package name (`[a-z][a-z0-9]*` with single `-`/`_` separators), PEP 440 version, layer rank (0 is the core) |
| `namespace` | optional; every relation the package declares, accepts, opens, exports or outputs must start with `<namespace>_` (extended relations are exempt) |
| `layers`, `facts` | `.dl` files in order; facts files (`*.toml` relation tables or `*.facts`) the package brings |
| `requires` | `{ buildlog-cpp = ">=0.1" }`: lower packages it reads or extends, with version specifiers |
| `open` | own relations higher layers may read and extend |
| `exports` | own relations or types higher layers may read but not extend (exporting an ADT type exports its constructors) |
| `extends` | relations of lower packages' `open` lists this package adds clauses to |
| `accepts` | own relations that take author spec/package facts |
| `user` | own relations that take user files only (invoker/environment choices) |
| `outputs` | own derived relations written for inspection |
| `driver` | own relations only the driver writes (provenance, regeneration inputs); spec and package facts may not supply them (core only today) |
| `target_namespace` | qualifier of the package's target ids, `<q>::name`; defaults to the package name (Halide declares `Halide`); unique among loaded packages, ignoring case |
| `[[probe]]` | tier 1 only: `kind`, `request`, `domain`, `result`, `entry`, optional `fingerprint`, `fingerprint_params`, `unguarded` (a reason; see configure caching) |
| `structural`, `fold`, `fold_origins`, `elide`, `flatten`, `demand`, `legend` | how `why` shapes proofs (see [proof-protocol.md](proof-protocol.md)); core-only in practice |
| `registered_includes` | `#include` paths a layer file may use (any other include is a load error) |
| `[[stratum]]` | tier 1 only: `name`, `program` (a `.dl` file), `entry` (`module:function`). A **build-time stratum**: a Datalog program a Ninja action runs (`buildlog -E collate <package>:<name>`, through the patched Soufflé) over files the build produced, so Ninja can learn edges the configure model cannot know. Its relations are private to it and its `.dl` is part of the lock hash. **One-way rule:** the loader rejects any layer whose text names a relation the stratum declares, so nothing at configure time reads build-time results. buildlog-cpp's `modules` stratum is the C++ modules collator |
| `[[provider]]` | tier 1 only: `name`, `entry` (`module:object`), optional `rules` (a rule-package directory included by bounded preparation, before inference) |

Probes and the discovery loop are described below.

## Layers, visibility and checks

Everything a package declares that is not open or exported is private. The loader qualifies private names in every package, the core included (`buildlog__nkeys`, `buildlog_cpp__tc_candidate`), and rejects reads of other packages' private names, clause heads outside owned or extended relations, `extends` outside lower layers' `open`, and reads of packages outside the transitive `requires`. Layer `.dl` files contain no `.input`/`.output`/`.printsize`/`.functor`: the loader generates all IO from manifests (inputs for `accepts` and probe results; outputs for probe requests/domains, the relations the driver reads, and `outputs`). The checks run on Soufflé's `--show=initial-ast` before any probe; the composed program and its registry are written to `<builddir>/program/`.

The governing rules, which the loader's checks enforce:

1. **Code dependencies point downward only.** A layer's rules may name its own
   relations and the relations of lower layers. They must never name a
   relation from a higher layer.
2. **Upper layers extend lower layers through declared extension points.** A
   higher layer adds clauses to a lower layer's *open* relations. For example,
   `buildlog-cpp` adds `tool_dep`, `iface` and `link_item` rows. *Logical*
   recursion may still cross layers (`needed` depends on a higher layer's
   `tool_dep`, which depends on `needed`). What is forbidden is lower-layer
   *code* knowing about higher layers.
3. **Module layers are not evaluation strata.** Layers are units of
   modularity and distribution. Negation and aggregation strata are a
   separate, solver-level concern and cut across layers.
4. **Nothing is chosen by order.** Wherever several candidates exist
   (toolchains, execution platforms, package files), the result must be
   unique. Otherwise the user supplies a *pin fact*, and remaining ambiguity
   is an error.

Two more conventions follow from them. **Property keys declare their
algebra**: the layer that owns a key declares it as `set_key` (union) or
`lattice_rank` (join), and an undeclared key, or a key with two algebras, is an
error. **Open vocabularies are symbols; closed algebras are ADTs**: property
keys, setting keys, languages, toolchain families and probe kinds are symbols,
so any layer can add them, while conditions are an ADT (`$Eq`, `$Not`, `$And`,
`$Or`, ...) because their evaluation rules must be total. Configurations are
records, and records and ADTs never reach L0: a naming relation turns each value
into a string.

**Project rules.** `.dl` files listed in `spec` form an unqualified synthetic
`project` layer. Their readable exports and writable open relations come from
the explicit roots' dependency closure, not every dormant executable module
found during preparation. Discovery cannot expand the author's permissions.
Any relation the project declares is an accepted input relation, so
TOML/`.facts` can supply it. Ground project-rule facts are copied to input
facts with `file:line` source-map origins. A project declaration that collides
with a package public relation is a loader error.

## Probes

A probe is Python code, in a tier-1 plugin, that asks the machine a question:
which compilers exist, whether a header compiles, where a library is. Probes
never decide anything. A layer declares a probe kind in its manifest with a
*request* relation, a finite *domain* relation and a *result* relation, for
example `req_cc_check(id, ...)`, `domain_cc_check(id, ...)` and
`res_cc_check(id, ..., answer)`. Rules derive requests, the driver runs the new
ones, and the results are plain facts that rules join back to the request by id.

A probe's `entry` is called once per new request as `fn(ctx, req_id, args, results)` and returns rows per result relation. The contract, which the driver enforces, has two parts:
- **Every request gets an answer.** The first `result` relation (the primary one) must have a row for `req_id`. A probe that finds nothing says so in a row, for example with a status column. An empty answer is an error naming the probe and the request, and is not cached.
- **Answers come from earlier rounds.** `results` is a read-only snapshot of the answers of earlier rounds, the same for every probe of the round, so the order in which a round's probes run cannot change their answers.

- Requests may withdraw as answers arrive. Probe kinds must satisfy the
  guardedness and answer-functionality contracts described in the formal
  account; finite domains alone do not establish order independence.
- Each probe kind declares a finite request domain. The driver checks at run
  time that every request is inside it (`request outside finite domain`).
- Probe results are cached by kind, request arguments and the probe's `fingerprint`: a function
  `module:function(args, results, **fingerprint_params)` that returns a string
  identifying its declared inputs (files by identity, environment variables,
  earlier answers). Together, the key and recorded watches must determine the
  answer within the plugin's documented environment-stability contract. Inputs
  outside those observations must remain stable while an answer is reused;
  document when users must explicitly refresh them. A probe kind with no
  fingerprint is never cached or seeded and runs on every configure.
  Sufficient observations and a clear stability contract are the plugin author's
  responsibility; the driver does not prove complete input discovery.
- A fingerprint is computed before the probe runs, so it cannot name what the
  probe turns out to read. A probe may instead return a *watch set* under the
  reserved result key `__watch__` (`discovery.WATCH_KEY`): a list of
  `discovery.watch_identity(path, directory=False)` rows, for observed files
  (identified by realpath, size, mtime, inode, device) and the directories
  observed during lookup (identified by realpath, mtime, inode and device,
  without size). Directory watches are nonrecursive: entries added or removed
  inside an existing child directory need not change its parent's identity.
  A missing path is watched too, so its appearance counts. These are stat
  identities, not content hashes; changes must be visible through those fields.
  The driver stores the set with the cached
  answer and re-checks it whenever the answer would be reused or seeded; any
  difference makes the entry a miss and the probe runs again. buildlog-cpp's
  `cc_check` watches the files named by the compile's depfile (`-MD`), every
  header and framework search directory and linker `-L` directory the driver
  reports (`-v`), and the library files that `-l` arguments resolve to.
- Shipped compiler checks do not promise complete negative or transitive lookup
  observation. Adding an earlier nested candidate or changing an unrecorded
  `__has_include` input can change an answer without changing its key or watches;
  failed checks may also have incomplete dependency records. Their unobserved
  toolchain and external include/library inputs must remain stable until an
  explicit refresh. After installing, removing, replacing or restructuring such
  inputs, use `buildlog configure --fresh -C build project` with the intended
  user files, presets and configuration selections. This bypasses warm skipping,
  seeds and cached probe answers; it refreshes reached requests, not every old
  cache entry. Other affected configurations or newly enabled checks also need
  a fresh configure. Disabling seeding alone does not bypass probe-cache reuse.
  See [the user guide](user-guide.md#probe-observations-and-environment-updates).

## A toolchain described by facts

An installed compiler is probed (buildlog-cpp's `inventory`), which is a
backwards-compatibility affordance for tools that do not describe themselves.
A toolchain the build makes states the facts the probe would have found,
the way a package file states a library's usage facts. A pure rule package
that ships with the compiler's sources derives `toolchain(tc, family,
version)`, `toolchain_runs_on`, `toolchain_can_target` and, for each role,
`toolchain_tool_target(tc, role, target)` in place of a path. It may also
derive `toolchain_tool_arg`, `toolchain_runtime`, the family's
`family_setting_arg` spellings and `tc_platform_arg`; all of these are open
core relations, so the manifest lists them in `extends`. Targets choose the
toolchain by name (`[target.t] toolchain = "tc"`). Core L2 does the binding:
the compiler target becomes a tool dependency built in the exec
configuration, and its artifact becomes argv[0] and an implicit input of each
compile, archive and link. A layer that lowers a target's actions reads
`tc_of(t, lang, c, tc)`, not `tc_for`, and spells a tool's argv[0] with
`action_tc_tool(a, tc, role)`, which handles installed and built toolchains
the same way. `case-studies/tcc/project/packages/tcc` is an example: TinyCC
as a toolchain the project builds.

## Binding-source importers

Package discovery is core and format-neutral; formats are *providers* that plugins register with `[[provider]]` in their manifest. Preparation catalogs finite layouts in declared roots and records finder offers independently of demand. The core selects candidates per name and platform (unique, or `package_pin`, otherwise an error on demand) and relays neutral rows with `file:line` origins. All reachable executable rules are validated and composed once, including dormant code. `buildlog-cpp` registers four providers, `pc`, `cps`, `cmake` and `buildlog` (a library installed as Datalog, [below](#a-library-installed-as-datalog-the-buildlog-format)), which decode pkg-config `.pc`, CPS `.cps`, CMake generated exports and buildlog metadata. Format names include `pc:libpng`, `PNG::PNG`, `Pkg:comp` and `Pkg:*`. Bare-name offers retain each provider's naming/layout rules rather than offering every alias in every file. Source declarations and aliases take precedence. The `_on` forms, e.g. `package_prefix_on = [["x86_64-macos", "/usr/local"]]`, scope a location or pin to one platform.

Providers implement `enumerate_files(kind, root, observations)`,
`metadata_links(file, observations)` and `decode(result, vocabulary, namespace)`.
The first two expose a bounded include/rule closure; the decoder accepts a static
platform vocabulary, never probe answers. Reads, directory membership, symlinks
and negative lookups must pass through the observation certificate. Late sysroot
paths remain typed expressions resolved by guarded environmental probes.
An ordinary metadata error may be deferred, but invalid referenced code is fatal.
Module declarations are bound-level and effect payloads are use-guarded; declaration
helpers cannot depend on demand or condition truth. Dormant code does not grant
the project new input relations. One executable implementation per package name
is supported; metadata-only instances do not compete as executable versions.

For example:

```toml
package_prefix = [["/opt/homebrew"]]                    # lib|share/{pkgconfig,cps,cmake}
package_search_dir = [["pc", "/some/other/pkgconfig"]]  # one directory, one provider
package_pin = [["Greet::shout", "/opt/greet/lib/cmake/Greet/Greet-targets.cmake"]]
package_file = [["cmake", "/opt/x/lib/cmake/X/X-targets.cmake"]]  # import outright
package_want = [["PNG::PNG"]]                           # request a binding explicitly
alias = [["PNG::PNG", "pc:libpng"]]                     # bind a name explicitly (core resolves it)
```

Two candidate files for a demanded name (including a `.pc` and a `.cps` for
one bare name) are an error until pinned. Ambiguous availability still counts
as defined; binding is the operation that requires uniqueness. Importers never
equate names across formats; besides bare-name bindings, the metadata-stated
alias is pkg-config `Provides`.

CMake's `$<TARGET_NAME_IF_EXISTS:T>` and `$<TARGET_EXISTS:T>` become core's
`$Defined(T)`: definitions within declared roots are available independently of
demand, without a `package_want` opt-in. Existence alone does not activate a
module's effects. Neither pins nor late probes broaden preparation's roots.
Package configurations map `Debug`/`Release` to `native.package_config`, which
is `native.opt` unless the spec maps it
(`package_config_map = [["debug", "release"]]`, like
`CMAKE_MAP_IMPORTED_CONFIG_DEBUG`); other configurations never match.
A `.pc` `buildlog_rules=` variable or CPS `x_buildlog_rules` pointer is followed
during preparation, before the one executable union is composed.

A CMake FRAMEWORK target links the binary inside its bundle; an include dir
naming a `.framework` becomes `-iframework <parent>`. `why` follows imported
facts to their metadata file and line. Core's `file_archs` probe reads the
platforms of a needed import from Mach-O, ELF, COFF or `ar` headers, in-process
and cached by stat. Providers also report versions (`package_version`),
attributes (`package_attr`), private build properties, follow-on imports and
per-linkage dependencies. The invoker selects linkage with
`package_linkage = [["LLVM", "shared"]]`; two unpinned offers are an error.
Fixtures are in `tests/fixtures/importers` (`regenerate.sh` regenerates exports).
`BUILDLOG_LLVM_BIN` offers an upstream clang toolchain (`llvm`) beside Apple clang.

## A library installed as Datalog: the `buildlog` format

An imported artifact is facts about its location and usage requirements, and a built artifact entails the same facts. The `buildlog` export format states them as Datalog, so nothing is translated into a spelling a consumer must read back: installing a library writes a rule package into the prefix, and a downstream project loads it like any package.

```
<prefix>/share/buildlog/<Pkg>/
  manifest.toml        # name, version, imports = [{ format = "buildlog", file = "<Pkg>.facts.dl" }], layers = [the shipped rules]
  <Pkg>.facts.dl       # the library's facts
  L5_<pkg>.dl, ...     # the project's own rules, if it ships any
```

**Facts.** The facts file is ground Soufflé: every line is a fact of the `pkg_*` vocabulary (`buildlog.dl_package.VOCABULARY`; `souffle_header()` prints the `.decl`s, and `tests/test_export_buildlog.py` runs Soufflé on header plus file and compares the tuples). `L3_install.dl` derives them from the same `export_*` relations the CPS, pkg-config and CMake files are written from, so the formats state one thing and there is no spelling table and nothing unspelled.

| relation | meaning |
| --- | --- |
| `pkg_package(pkg)`, `pkg_version(pkg, v)`, `pkg_prefix(rel)` | the package; the way from the facts file's directory to the prefix (`../../..`) |
| `pkg_component(comp, kind)`, `pkg_default(comp)` | a component (`interface`, `archive`, `dylib` or `module`, a file a loader dlopens: never a default component); one in the package's default set |
| `pkg_location(comp, rel)`, `pkg_link_name(comp, n)`, `pkg_soname(comp, n)` | a native component's artifact, relative to the prefix, and its link and install names |
| `pkg_requires(comp, other)`, `pkg_depends(comp, pkg, other)` | a requirement on a sibling, or on a component of another package (`other` empty: the package's defaults) |
| `pkg_runtime_dep(comp, module, place)` | `comp` loads the `module` component at run time (`beside_loader` or `beside_executables`): consumers that link `comp` get it placed like a built one (core's `runtime_dep_when`) |
| `pkg_prop(comp, key, v)`, `pkg_prop_path(comp, key, rel)`, `pkg_prop_abs(comp, key, path)` | an exported usage requirement, by buildlog's own property key: a value, a prefix-relative path, an absolute path outside the package |
| `pkg_compat`, `pkg_description`, `pkg_requires_version` | metadata, kept in the file and not imported (the CPS importer skips the same fields) |

Because paths are prefix-relative and the prefix is found from the file's own location, a copy or a move of the install imports as the same facts with the new prefix. An absolute path (`pkg_prop_abs`) states a place outside the package and is not relocated. The format has no conditions: a package installed from several configurations must state the same facts in each.

**Rules.** A project that wants its rules to travel names the rule package it owns:

```toml
export_rules = [["minidsl", "src:packages/minidsl"]]   # package, a project-local rule package
```

The directory is an ordinary rule package (`manifest.toml` and layers, whose `name` must be the exported package's). Install copies its manifest, layers and facts into `share/buildlog/<Pkg>/` beside the facts file, and lists the facts file in the manifest's `imports`. This is how a DSL library ships its generator rules next to its library facts (`tests/fixtures/export-rules`: `minidsl` ships a rule that turns `.dsl` sources into C++ for targets that use `minidsl:rt`). 

**Consumption.** During preparation, the `buildlog` provider finds
`<prefix>/share/buildlog/<Pkg>/manifest.toml`, the package's single entry point,
and follows its `imports`. Its facts use CPS-style ids `<Pkg>:<comp>` and
`<Pkg>:*`; its pure tier-2 rules enter the executable union before inference.
Binding and use do not load another program. The final lock pins finally used
modules by content hash, including their imported facts
(`reason = discovered:...`). Three things follow:

- *Where it enters.* Preparation follows manifest imports and `buildlog_rules=` pointers before inference. Imported files are immutable available metadata, not probe results. Binding enables declarations; use enables actions and other effects. The lock never activates a module: final locks contain explicit packages and finally used modules only. Uncertified generated imports fail instead of starting another pass.
- *W5 and the other checks.* Shipped rules are checked like any package's. Decision and declaration dependencies must respect the lower selection boundary; demand-dependent declarations are rejected before evaluator startup. A private name of another package is a visibility error, and a clause head the package does not extend is a layering error (`tests/test_export_buildlog.py`, `test_loader_checks_apply_to_shipped_rules`). A tier-2 package declares no probes or providers; preparation rejects such declarations before importing its Python code.
- *Which route binds.* Identity, not precedence. The generated CPS file names
  its sibling with `"x_buildlog_manifest": "share/buildlog/<Pkg>/manifest.toml"`.
  The provider verifies this prefix-relative claim and records `Result.same`,
  materialized as `av_pkg_same`; core treats the two files as one candidate,
  without format precedence. A third-party CPS file without the marker remains
  a competing candidate until pinned. The marker must name an existing manifest
  inside the prefix with the expected directory and package name. A dangling,
  escaping or mismatched marker is invalid metadata when demanded, not permission
  to fall back silently. Pinning the CPS file selects that route. `uses` can name
  an explicit rule package; `--package-path` declares a preparation prefix.

**Manifest `imports`: nearby export files of other formats.**
`imports = [{ format = "cmake", file = "../../../lib/cmake/X/XTargets.cmake", namespace = "X" }]`
names an upstream export beside a hand-written manifest and rules
(`tests/fixtures/export-imports`). The file is relative to the manifest at
`<prefix>/share/buildlog/<Pkg>/` and must remain inside the prefix; `namespace`
has the provider's usual CMake meaning. Preparation follows these imports for
explicit as well as discovered package roots, using each format's bounded
metadata hooks. The generated facts file is another import:
`{ format = "buildlog", file = "<Pkg>.facts.dl" }`.
The lock hash covers the manifest, layers, facts and imported files.
Targets keep their format's ids: `X::y` is also `X:y`; downstream rules and
dependencies name those ids. A manifest without a facts file may offer its
directory's `X:` or `X::` spelling, but a wrong name is unresolved, never an
implicit selection.

**A package that ships its upstream's exports and a few facts** (`case-studies/halide/llvm-install`, `case-studies/qt-moc/share/buildlog`). One manifest per upstream package (LLVM, Clang, LLD; Qt6, Qt6Core, ...), as upstream has one `*Config.cmake` each; no manifest answers for several names. A manifest `imports` its own export file and ships a layer of facts and rules about it. Package names may be upper-case (`LLVM`, `Qt6Core`): the manifest directory is the name, and the `buildlog` provider looks for `<text before ::>` as the directory, so a name that is not that text (`Qt6::Core` is in `Qt6Core`) is bound with `package_file`, one row per manifest. Two levels of facts: *package-level* facts (the CMake variables: LLVM's version, `LLVM_ENABLE_RTTI`, `LLVM_DEFINITIONS`, `LLVM_TARGETS_TO_BUILD`) are the package's own relations (`llvm_version`, `llvm_define`, ... in its facts file) and are published to consumers through the open `package_version` and `package_attr`, which a consumer's rules read (`halide-build` drops `_GLIBCXX_ASSERTIONS` from `package_attr("LLVM", p, "llvm.define", d)`); *target-level* properties are derived by the package's rules from them for the targets it imports (`prop(t, "cxx.rtti", v, "private") :- import_ns_target("LLVM", t), ...`), so buildlog-cpp's generic C++ ABI relation sees them with no LLVM-specific rule in buildlog-cpp or core. `import_ns_target(ns, t)` holds for an imported target `t` of an import with namespace `ns`.

*Ownership.* The loader refuses a package whose rules or facts state a target-keyed relation (`target`, `imported`, `imported_kind`, `import_artifact`, `prop`, `prop_when`, `dep`, `dep_when`, `package_want`) about an id outside the namespaces its own manifest owns: its name, its `target_namespace`, and the `namespace` of each of its `imports` (a literal id, a `cat("NS:", ...)`, or a variable the body binds by `import_ns_target(<owned>, v)`), and a package-keyed relation (`package_version`, `package_attr`, `import_linkage_dep`) about a package other than its own: "asserts about a target outside the namespaces its imports own". Action relations (a rule that generates a source for a consumer's target) are not covered. The `namespace` option also accepts an export whose targets are already `<ns>::...` (Qt's `Qt6::Core` under `namespace = "Qt6"`), so that the manifest owns the namespace it states.

*Conflicts are errors.* If the imported export and the package's rules state a single-valued property (`cxx.rtti`, `cxx.exceptions`) differently, the C++ ABI relation reports `conflicting C++ ABI settings`; there is no precedence between sources. A package that `requires` another at an exact version (`Clang` requires `LLVM = "==22.1.8"`) is a load error when the versions differ.

**Relation to CPS `x_buildlog_rules`.** A hand-written or third-party CPS file
may point at a rule package with `x_buildlog_rules`; preparation follows it
before inference. Buildlog's own install instead writes `x_buildlog_manifest`,
so facts and rules belong to the same manifest. The CPS route translates CPS's
vocabulary with `export_spelling` and unspelled keys. The `buildlog` route keeps
facts in buildlog's own vocabulary as Datalog.

**Provenance.** `buildlog why` on an imported fact ends at the line of the installed `<Pkg>.facts.dl` that states it. It does not continue into the upstream project's derivation: the exporter runs at install over the derived relations, without the proofs.

## Provenance and rules

`why` explains a derived fact with the proof Soufflé's provenance mode builds,
shaped for reading ([proof-protocol.md](proof-protocol.md) specifies the
document, the shaping and the Soufflé features it relies on). What a rule author
should know:

- A leaf's origin is a TOML file and line, a package facts file, a probe and
  its request, or a layer fact. Sugar in the loader (infix conditions, compact
  tables, `[defaults]`, `[profile]`) desugars to existing relations and keeps
  the row's origin, so `why` cites the row you wrote, not a generated one.
- A manifest's `structural`, `demand`, `fold`, `fold_origins`, `elide`,
  `flatten` and `legend` lists tell `why` what to hide, fold or print as a
  legend. They are currently set by the core manifest.
- Probe requests are pruned from proofs (their subproof says why a question was
  asked, not why the answer holds). `why --demand PATTERN` asks that other
  question.
- Build a request id from its arguments injectively: two different argument
  tuples must never share an id. Do not join fields with a bare separator
  (`cat(a, "|", b)` collides on `a|b`, `c` versus `a`, `b|c`). Length-prefix
  every field but the last, as the shipped kinds do:
  `cat("kind:", to_string(strlen(a)), ":", a, ":", b)`. Keep a tag (`import:`,
  `import-ns:`) distinct for ids of different arity. A single-argument id is
  already injective.
- Rules that read a probe's results should join them on the request id. This is
  also what makes probe answers safe to seed from the previous configure (see
  the cache description in [the user guide](user-guide.md#caches-and-speed));
  `unguarded = "reason"` declares an exception.
- An auxiliary request guard must have no fact or accepted/user/driver input bypass.
  Only top-level positive atoms certify guards; aggregate-local or parenthesized
  atoms do not. This conservative check may reject a semantically guarded rule.
- Probe requests, domains and `discovered_rule_package` must not depend on
  completion-gated rule heads or `discovery_complete`, even indirectly through
  negation or aggregation. The loader reports the offending dependency chain (W4).

## Design notes

**Sugar desugars to the existing relations.** Short authoring forms exist so
that authors state intent once (a target's name is not repeated in every fact),
but each one expands to the same relations a longer spelling would state, so
provenance stays intact and a spec may always use the underlying relations.

**Include directories are a class-ordered set, not a walk.** Build systems
mostly order include directories by a depth-first walk of the dependency graph,
first occurrence wins (CMake, Meson, build2, GN; Bazel and Buck2 effectively),
and none of them detects header shadowing. Order only matters when headers
collide, or with `#include_next` and wrapper headers. buildlog-cpp instead
partitions directories into classes (the target's own `-I`, dependencies'
`-isystem`, then the system class) and treats each class as a lexically ordered
set, which is monotone and deterministic. Where a project has a real collision
it states `cc_include_before(x, y)`, and a probe-detected `-Wshadow-header`
warns about the rest. See "Include directories and warnings" in
[spec-reference.md](spec-reference.md#targets-and-native-code).
