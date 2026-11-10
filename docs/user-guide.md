<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# User guide

How to run buildlog on a project: the commands, the files that belong to the
project's author and the files that belong to you, and the state a build
directory keeps. For what goes in a spec see [spec-reference.md](spec-reference.md). For
writing packages and rules see [rule-authoring.md](rule-authoring.md). For how
the pieces fit see [architecture.md](architecture.md).

All examples run from the repository root and use `uv run --frozen` so they
use the locked environment. `BUILDLOG_SOUFFLE` must point at the patched
Soufflé (see [the README](../README.md#prerequisites)).

## The workflow

1. **Configure.** `buildlog configure` resolves packages, loads the project's
   facts, runs discovery (probes of the machine) until nothing new is asked,
   derives the build, and writes `build.ninja` into `<builddir>/souffle/`.
2. **Build.** Run `ninja -C <builddir>/souffle`. Ninja does not call buildlog,
   except that `build.ninja` carries a regeneration edge (see "Regeneration").
3. **Ask.** `buildlog why`, `why-not` and `query` answer questions about the
   configured build without re-running discovery.
4. **Test.** `buildlog test` builds and runs the tests the project declares.

The build directory defaults to `<project>/buildlog-build` for every command;
`-C` overrides it.

## A first run

`examples/hello` is a C++ program that links a small library and the system
zlib. It needs a pkg-config file for zlib, and where that lives is a decision
of the *user*, so the example ships a preset for Homebrew on Apple silicon:

```sh
uv run --frozen buildlog configure --user examples/hello/presets/homebrew-arm64.toml \
  -C build/hello examples/hello
ninja -C build/hello/souffle
build/hello/souffle/bin/arm64-macos.release.none/hello
```

The last command prints `hello 42 1.3.2` (the zlib version varies). On another
machine, copy the preset to a file of your own and change the toolchain pins and
the zlib `.pc` directory (for example `pkg-config --variable=pcfiledir zlib`).
Without a user file, configure fails with `dependency zlib of hello has no
definition`: the project says it needs zlib, and nothing says where to look.
User decisions never require editing the author's files.

The project is three small files:

```toml
# examples/hello/buildlog.toml
uses = ["buildlog-cpp"]
spec = ["spec.toml"]
```

```toml
# examples/hello/spec.toml
config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
package_want = ["zlib"]
request = [["hello", "release"]]

[target.greet]
native_kind = "library"   # static unless the user chooses shared
cxx_source = "greet.cpp"

[target.hello]
native_kind = "executable"
cxx_source = "main.cpp"
dep = { greet = "private", zlib = "private" }
```

```toml
# examples/hello/presets/homebrew-arm64.toml (a user file)
toolchain_pin = [
  ["c", "arm64-macos", "arm64-macos", "apple-clang"],
  ["cxx", "arm64-macos", "arm64-macos", "apple-clang"],
  ["native-link", "arm64-macos", "arm64-macos", "apple-clang"],
]
package_search_dir = [["pc", "/opt/homebrew/opt/zlib/lib/pkgconfig"]]
```

`examples/cross` is a larger, macOS-only example with a cross-compiled
generator host tool, toolchain presets and a universal binary. The provenance
examples below use it:

```sh
uv run --frozen buildlog configure --user examples/cross/presets/apple-clang.toml \
  -C build/cross examples/cross
ninja -C build/cross/souffle
```

The Halide test projects need a Halide install. Its location is a user
decision with no default, so pass it with `-D`:

```sh
uv run --frozen buildlog configure --package-path tests/fixtures/rule-packages \
  --user tests/projects/halide/presets/homebrew-arm64.toml \
  -D HALIDE_PREFIX=/path/to/halide-install \
  -C build/bilateral_grid tests/projects/halide/bilateral_grid
```

## Commands

- `buildlog configure [-C builddir] [--package-path PREFIX]... [--user FILE]... [--preset NAME]... [-D NAME=VALUE]... [-O NAME=VALUE]... [--update-lock] [--config NAME]... [--fresh] [--keep-going] [project]`: resolve packages, load facts, run iterated discovery, derive, write `buildlog.lock` and `build.ninja`.
  - `--config` (also on `check`) builds only the named declared configurations. The build directory remembers the choice like `--user` (a reconfigure that changes the active set says so: `configuring X (was: Y)`). Without it, the user's `config_default` applies, else every declared configuration.
  - `--preset NAME` loads `[presets.NAME]` from `buildlog.toml` as a user file.
  - `-D NAME=VALUE` overrides a `[vars]` value, and `-O NAME=VALUE` sets a project option. Both are remembered by the build directory.
  - `--fresh` bypasses the unchanged-configure skip, probe seeds and cached probe answers, executing reached requests and writing their new answers for later reuse. Use it after external/toolchain environment updates; see [Probe observations and environment updates](#probe-observations-and-environment-updates).
  - `--shuffle N` and `--shuffle-probes N` permute the input rows and the probe order with seed N. The output must not change; the tests use this as a determinism check.
- `buildlog check [the same options] [project]`: the same without emitting Ninja; prints derived error facts and exits 1 if there are any.
- `buildlog why [FACT|TARGET...] [-C builddir] [--flag TOKEN] [--output PATH] [--config NAME] [--full] [--demand PATTERN] [project]`: explains derived facts, or with `--demand`, why the probes behind them were asked. See "Provenance".
- `buildlog query '<rule body>' [-C builddir] [project]`: runs an ad-hoc Datalog query over the derived model.
- `buildlog why-not FACT [-C builddir] [project]`: explains why a fact is *not* derived.
- `buildlog test [-C builddir] [-R re] [-E re] [-L label-re] [-j N] [--timeout S] [--no-build] [--list] [--output-on-failure] [--json OUT] [project]`: builds the files the configured tests need, then runs them with ctest's semantics. See "Tests".
- `buildlog -E COMMAND ...`: portable file commands that generated build actions call (`copy`, `copy_if_different`, `create_symlink`, `make_directory`, `remove`, `touch`, `configure_file`, `export_package`, `install`), in the spirit of `cmake -E`. You rarely run these yourself.

`why`, `query` and `why-not` also take `--package-path` and `--format text|json`. They reuse the configured build directory. They configure only if it is missing, and never re-run discovery.

## Provenance

`why` answers "why is this flag on this compile line?" with a proof from Soufflé's provenance mode, shaped for reading. Name the tuple, or name a Ninja output and a command-line token and let buildlog find the tuples, using derived facts only:

```console
$ buildlog why -C build --flag -DHAVE_SIMD=1 --output obj/x86_64-macos.release.none/app/app/main.cpp.o examples/cross --leaves
action_arg("out:obj/x86_64-macos.release.none/app/app/main.cpp.o", 21, "-D", "HAVE_SIMD=1")
  condition("is_x86", $Eq("platform.cpu", "x86_64"))  ← examples/cross/spec.toml:2
  cxx_source("app", "app/main.cpp")  ← examples/cross/spec.toml:25
  dep_when("app", "simd", "private", "is_x86")  ← examples/cross/spec.toml:43
  needed("app", ‹x86_64-macos.release.none›)  [exists here because it is built in this configuration; --demand explains why]
  needed("simd", ‹x86_64-macos.release.none›)  [exists here because it is built in this configuration; --demand explains why]
  prop("simd", "cc.define", "HAVE_SIMD=1", "public")  ← examples/cross/spec.toml:66
  res_platform_constraint("inventory:default", "x86_64-macos", "cpu", "x86_64")  ← probe inventory request "inventory:default"
  target("simd")  ← examples/cross/spec.toml:93
```

Without `--leaves`, `why` prints an indented tree. Each derived node shows its rule id (`[buildlog-cpp:layers/L4_cxx.dl:32]`), and each leaf shows its origin: a TOML file and line, a package facts file, the probe and request behind a discovery result, or a layer fact. Configurations print by name (`‹x86_64-macos.release.none›`), and shared subproofs print once, then as `↑ #n4` on each other consumer's line. Leaves that one authored row states (a row and the infix condition desugared from it) share that row's line.

The tree is folded by default. Configuration construction (`config`, `config_setting`, `exec_config` below a root) is one line marked `folded`, a declared configuration (`decl_cfg`) lists only the spec lines it was built from, a configuration's name and platform lookups are dropped, the recursion of `exec_settings` over a settings list is one node, and builtin constraints (`1 > 0`), negations of engine relations and pruned probe requests (their results cite them) are dropped. `--full` shows all of it, with the legend of configuration values. A core manifest lists the relations (`fold`, `fold_origins`, `elide`, `flatten`; see the protocol). The common questions fit on a page:

```console
$ buildlog why -C build app --config cross-release examples/cross
needed("app", ‹x86_64-macos.release.none›)  [buildlog:L1_targets.dl:…]
  request("app", "cross-release")  ← examples/cross/spec.toml:81
  #n3 config_decl("cross-release", "x86_64-macos")  ← examples/cross/spec.toml:9  (via config_active)
  decl_cfg("cross-release", ‹x86_64-macos.release.none›)  [buildlog:L1_targets.dl:…; folded]
    config_decl_setting("cross-release", "native.opt", "release")  ← examples/cross/spec.toml:15
  config(‹x86_64-macos.release.none›)  [buildlog:L1_targets.dl:…; folded]
```

- A bare **target** asks why it is built (`needed`), in every configuration or those `--config` selects.
- `--config NAME` takes a configuration's printed name (`x86_64-macos.release.none`), a declared configuration (`cross-release`) or a prefix ending at a dot (`x86_64-macos`). In a fact, `@NAME` or `‹NAME›` stands for the configuration value: `buildlog why 'needed("app", @cross-release)'`; `query` and `why-not` accept them too.
- `--output` takes a Ninja output path, a target (its actions), a source file (the actions that compile it) or an output's basename, narrowed by `--config`. `why app --flag clang++` is `--output app`.
- `--flag` takes the token as spelled, or else its basename (`clang++` for the compiler's absolute path) or a prefix (`--spirv-core-grammar` for `--spirv-core-grammar=/abs/…`). With a target or source, a token every action has is explained once, on the target's final action: `buildlog why --flag -mavx2 --output arch/x86/adler32_avx2.c --config x86_64` in the zlib-ng case study.

- `--depth N` limits expansion, and `… k more` marks what was cut.
- Single-premise chains collapse by default and are listed as `(via …)`. `--no-collapse` keeps them, and `--raw` turns off all shaping.
- `--path-to PATTERN` keeps only the chains that reach matching leaves, e.g. `--path-to spec.toml:66`, `--path-to probe:inventory` or `--path-to package:halide`.
- A probe's request (`req_cc_check(…)`, `req_pkg_find(…)`, …) shows as one line with its content, marked `[asked: …]`: its subproof says why the question was asked, not why the answer holds, so it is pruned (and dropped where a result that cites it is shown). Likewise a target's demand, `needed(…)` beneath an action's attribute, is one line; `--with-demand` expands that (why the target is built in this configuration), and never a probe's. `--demand PATTERN` asks that question instead: it explains the requests under the proof that match `PATTERN` (a request's text, its relation or `probe:<kind>`; `*` for all), e.g. which targets and configurations made a check run. See "Answers and demand" in the protocol.
- `--format json` prints the versioned proof document described in [proof-protocol.md](proof-protocol.md). Nodes are shared by id, values are typed, and origins are structured.
- `--html out.html` writes a self-contained viewer with collapsible nodes and `file://` links to origins. Add `--print` to also print the proof.
- `--stats` prints sizes and timings to stderr.

A fact can use a relation as its package wrote it (`cxx_object`) or qualified (`buildlog_cpp__cxx_object`): `buildlog why 'request("app", "u2-release")' examples/cross`.

`query` takes a rule body. Negation, constraints and `_` are allowed, and names are qualified automatically. It prints a table of variable bindings, or `true.`/`false.` for a ground query:

```console
$ buildlog query -C build 'link_file(t, c, f), t = "simd"' examples/cross
t     c                            f
----  ---------------------------  ---------------------------------------------
simd  ‹x86_64-macos.release.none›  out:lib/x86_64-macos.release.none/libsimd.a
(1 row)

‹x86_64-macos.release.none› = ["x86_64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]]
```

`--format json` gives `{"format": "buildlog-query", "variables": [...], "rows": [...]}`, with values typed as in proofs.

`why-not` uses Soufflé's `explainnegation`. For every rule whose head can match, it binds the rule's other variables to a witness, the first answer of the longest satisfiable body prefix. It then marks each body literal as holding or not. The output has one block per rule that could match; this is the block that matters for the fact below (the others show rules whose first literal already fails):

```console
$ buildlog why-not -C build 'action_arg("out:obj/x86_64-macos.release.none/app/app/main.cpp.o", 21, "-D", "HAVE_SIMD=0")' examples/cross
why not action_arg("out:obj/x86_64-macos.release.none/app/app/main.cpp.o", 21, "-D", "HAVE_SIMD=0")?

…
rule buildlog-cpp:layers/L4_cxx.dl:32  (plugins/buildlog-cpp/src/buildlog_cpp/layers/L4_cxx.dl:32)
  action_arg(o, 20 + r, "-D", v) :- cxx_object(t, c, _, o), use(t, c, "cc.define", v, r).
  with _4455="app/main.cpp", c=‹x86_64-macos.release.none›, r=1, t="app"
    ✓ cxx_object("app", ‹x86_64-macos.release.none›, "app/main.cpp", "out:obj/x86_64-macos.release.none/app/app/main.cpp.o")
    ✗ use("app", ‹x86_64-macos.release.none›, "cc.define", "HAVE_SIMD=0", 1)   ← first failing literal
    next: buildlog why-not 'use("app", ["x86_64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]], "cc.define", "HAVE_SIMD=0", 1)'
…
101 other rule(s) for this relation cannot match the head
```

`--bind var=value` and `--rule N` override the witness and the rule choice (`N` is the engine's rule number). `-v` lists the rules that cannot match. `why-not` explains one level at a time, so follow the `next:` hint to go deeper.

## Project files

A project root must contain `buildlog.toml`, or a `pyproject.toml` with a `[tool.buildlog]` table holding the same keys (for a Python project with a native part, so that it has a single project file; `[tool.buildlog.vars]` is the vars table; both at once is an error). It is the only project file found by convention: a missing entry point is an error, and every authored spec input must be named explicitly:

```toml
uses = ["buildlog-cpp", "blurgen"]
source_root = "src"
spec = ["spec.toml", "facts/extra.facts", "rules.dl"]
include = ["../workspace-vars.toml"] # optional vars/source_root only; not facts

[vars]
SDK = "/opt/sdk"
```

`spec` paths are relative to `buildlog.toml`; the suffix decides the kind (`.toml` grouped facts, `.facts` TSV, `.dl` project rules). Shared files such as a workspace profile must be listed (usually in `spec` if they supply facts, or `include` if they only supply `vars`/`source_root`). `vars`, `source_root` and `uses` keep their previous meaning. Each executable package name must resolve to exactly one plugin or search-root package. The loader also follows package `requires` and resolves specification namespaces. Bounded preparation follows all metadata includes and rule pointers within declared roots before composing once. A metadata pin chooses an installed binding, not a conflicting executable implementation. Dormant modules cannot extend the project's authoring schema; their payloads become active only through binding and use.

## Author and user files

**Author and user facts.** Manifests distinguish `accepts` (facts the author or a package may ship) from `user` (invoker/environment choices). User facts are loaded only from files named with repeatable `--user FILE`; TOML uses the same grouped forms as spec files, `.facts` are TSV, and `.dl` user files may contain ground facts. Paths are relative to the current working directory. `-D NAME=VALUE` overrides `[vars]` for the build directory; user files do not introduce their own `[vars]`. An existing build directory records the resolved user files, `-D` values and `--config` selection in `buildlog-state.json`; a later configure/check with no `--user`/`-D`/`--config` reuses them, while passing the option replaces the recorded value. There is no auto-loaded user file; authors may ship opt-in presets such as `presets/homebrew-arm64.toml`.

If an author spec file (or ground fact in its project `.dl`) supplies a user relation, configure fails and names the file and relation. The explicit escape hatch is:

```toml
[author_decides]
toolchain_pin = "firmware: only arm-none-eabi-gcc 12 produces a bootable image"
```

The spelling says exactly what happened: the author, not the user, decided that relation. The reason string must be non-empty. The fact remains ordinary monotonic input, so a conflicting user fact still produces the usual error; where core can identify the conflict, the error quotes the author decision reason. User files are root-build state only: a downstream never inherits a dependency's user files. The lockfile remains about authored package/spec inputs; user files are per-build-dir state and are tracked by regeneration instead.

### Presets

A preset is a user file the author ships as an example of what a user might
decide: `examples/hello/presets/homebrew-arm64.toml` pins Apple clang and says
where Homebrew's zlib lives. Use it with `--user FILE`, copy and edit it, or
write your own. `buildlog.toml` can also hold `[presets.NAME]` tables, selected
with `--preset NAME`. Presets are user input, so the author/user boundary stays
visible: the author's spec never names a machine path.

### Locks

**Lockfile.** `configure` writes `buildlog.lock` (TOML) next to `buildlog.toml`: every loaded package's name, version, source (`core`, `plugin` with its entry point, or a project-relative `path`), a sha256 over its manifest, layer and facts files, and why it was loaded (`uses`, `requires:<pkg>`, `namespace:<rel>`, `discovered:<probe>:<request>`; an installed `buildlog` package is a discovered one, so its facts and rules are pinned like any package; the lock only pins and checks them, it never loads a package: every configure discovers them again). A missing lockfile is created silently. If a locked package's version, source or hash changes, configure fails until you rerun it with `--update-lock` (for example after editing a package's layers). Packages that are added or dropped update the lock silently.

### Regeneration

**Regeneration.** `build.ninja` has a `generator = 1` edge that reruns `buildlog configure` with the same package paths, user files, `-D` overrides, `BUILDLOG_PACKAGE_PATH` and `--keep-going` (never `--update-lock` or shuffle seeds) when `buildlog.toml`, a file listed by `spec`/`include`, a recorded user file, the lockfile, a loaded package's manifest/layer/facts file, or the buildlog/plugin distribution metadata changes. The dependency list is derived (`configure_input` driver facts → a `CONFIGURE` action in L0). Probe-only environment changes do not trigger this edge: configure explicitly, using `--fresh` after external/toolchain input updates as described below.

### Partial emission

**Partial emission.** Layers attribute errors to what they break: `error_at(kind, detail, t, c)` for a target in a configuration, or `action_error(a, kind, detail)` for an action. Core derives the affected actions in Datalog (dependents of a broken target, and consumers of a failed action's outputs), and `build.ninja` can turn each into an edge that prints the errors and fails. By default, configure exits 1 before writing or using `build.ninja` if any derived error exists. With `--keep-going`, configure emits the partial graph and exits 0 when all errors are attributed; an unattributed (global) error writes no `build.ninja`, removes a stale one, and always exits 1. `check` exits 1 on any error.

## Tests

`buildlog test` runs the tests a project declares as facts (`test`, `test_arg`,
`test_env`, ...; see [spec-reference.md](spec-reference.md#tests)). It builds the files the tests need
(`--no-build` skips that), runs each case in its own directory
(`test/<config>/<name>` under the build directory) and reports like ctest.
`--list` shows the selected tests without running them, `-R`, `-E` and `-L`
select by name, exclude by name and select by label, and `--json OUT` writes
the results. A cross configuration's tests are listed as "not run" with the
reason, unless you say how the machine runs them with `platform_runs_on` in a
user file (Rosetta, or a qemu-user launcher).

## Caches and speed

**Configure caching.** Two caches keep a warm `configure` short. Their agreement with
fresh discovery relies on the probe contracts, including the environment-stability
boundary below. (1) The loader's Soufflé analysis runs (the per-layer AST dump and the composed
program's type check) are memoized in a user cache dir (`BUILDLOG_CACHE_DIR`, default
`~/Library/Caches/buildlog` or `$XDG_CACHE_HOME/buildlog`; `off` disables it), keyed by the
Soufflé binary's identity, the arguments and the content of every file read, with the build
dir abstracted, so it is shared across build dirs; a hit replays the recorded errors and
warnings. (2) The discovery fixpoint is seeded with the previous configure's probe answers
(`probe-answers.json` in the build dir; `BUILDLOG_SEED_PROBES=0` disables it). With the same
program and inputs it replays in one round when every recorded request still holds.
Recorded withdrawn answers can be retained by certified replay; otherwise only guarded
answers seed the next run. A replay that deviates retries the same program with guarded seeds; a seeded run that fails a driver
check reruns unseeded. Otherwise only answers of probes that obey the
join discipline are seeded, confirmed round by round against their requests, and unused ones
are dropped before `why` sees the facts. The join discipline is a load-time check: every rule
that reads a probe's result relation must join it on the request id with the probe's request
relation (or a relation derived only from it). A probe whose answers cannot be read that way
declares `unguarded = "reason"` and is seeded only on exact replay. Shipped package
selection is fixed available data, not an unguarded find/import probe.
Configure timings are recorded in the build directory's metrics.

Round execution also reuses unchanged inputs. With the patched Soufflé's
`--serve`, one process per discovery fixpoint parses, checks and translates the program once
and evaluates each round on request (`BUILDLOG_SOUFFLE_SERVE=0` runs one process per round).
A round whose inputs (Soufflé's identity, the composed program, every input fact file) equal
an earlier round's in the build dir reuses that round's outputs (`rounds/round-N.key`;
`BUILDLOG_ROUND_MEMO=0` disables it), so a warm configure with nothing changed evaluates
nothing. With `--serve-deferred-output`, the rounds before the last write only the relations
discovery reads and the final pass's remaining outputs are dumped from the same evaluation
(`BUILDLOG_SOUFFLE_DEFER=0` makes every round write all outputs, for inspecting the early
rounds). A memo is complete or control; only a complete one is reused by a later configure
that needs all outputs. There is one prepared executable union throughout configuration.

**Certified preparation.** Before inference, providers enumerate metadata within
declared roots and follow includes and rule pointers. The complete executable union
is validated and composed once. `prepared.pkl` reuses that traversal when its
observations remain valid; `available-certificate.json` also covers decoded metadata
and negative path lookups. An unchanged warm configure can skip composition and
evaluation entirely. Adding a metadata file, changing contents or retargeting a
symlink invalidates reuse. A probe cannot introduce an uncertified root, file or rule.
`metrics.json` reports `composition_calls`, `compositions` (cache misses),
`evaluator_startups`, `evaluations` and `available_bundles`. Warm skips report zeros
for execution counters. The former prefetch/restart mechanism and its flag are removed.

### Probe observations and environment updates

An ordinary explicit configure validates probe keys and recorded filesystem watches,
including on the unchanged-configure fast path. Keys include the request arguments
and the probe's fingerprint. The shipped compiler checks fingerprint tool and SDK
identities, target information and selected environment values, including the value
of `CPATH`; they do not fingerprint every file under those paths.

Compiler checks also watch depfile-recorded inputs, compiler-reported header/framework
search roots and linker search directories, and selected library files. File identities
use resolved path, size, modification time, inode and device, not content hashes.
Directory watches omit size and are **nonrecursive**. A change to a recorded identity
invalidates its cached answer; changes must be visible through these identity fields.

This is not complete compiler-input discovery. For example, adding a header in an
existing nested directory of an earlier search root can change header resolution
without changing a watched root or the previously read header. Negative lookups,
`__has_include` inputs and dependencies of failed checks may be unrecorded.
Unobserved toolchain and external include/library inputs must remain stable while
cached answers are reused.

After installing, removing, replacing or restructuring those inputs, configure
explicitly with `--fresh`, using the intended user files, presets and other options:

```sh
buildlog configure --fresh -C build project
```

`--fresh` bypasses the unchanged-configure skip, seeds and cached probe answers.
It executes reached probe requests and writes their new answers for later reuse;
it does not purge every old cache entry. Use it for other affected configurations
or newly enabled checks as well. Setting `BUILDLOG_SEED_PROBES=0` alone is not
equivalent: individual probe-cache answers may still be reused.
Validated preparation and layer-analysis caches can still be reused.

## Importer locations (user side)

Binding sources (installed pkg-config, CPS and CMake package metadata) are
found in directories the *user* names. The author lists the names a project
wants; the user file says where to look, never which candidate wins:

```toml
package_prefix = [["/opt/homebrew"]]                    # lib|share/{pkgconfig,cps,cmake}
package_search_dir = [["pc", "/some/other/pkgconfig"]]  # one directory, one provider
package_pin = [["Greet::shout", "/opt/greet/lib/cmake/Greet/Greet-targets.cmake"]]
package_file = [["cmake", "/opt/x/lib/cmake/X/X-targets.cmake"]]  # import outright
package_want = [["PNG::PNG"]]                           # search a name only optional edges use
alias = [["PNG::PNG", "pc:libpng"]]                     # bind a name explicitly (core resolves it)
```

Two candidate files for one name are an error until you pin one. The importers and
how they bind names are described in [rule-authoring.md](rule-authoring.md#binding-source-importers).

### Installing a library as Datalog, and using it

Installing a library also writes `<prefix>/share/buildlog/<Pkg>/`: its facts as a Datalog file plus the rules the project ships (`export_rules` in the spec). A downstream project that wants the package exactly as buildlog states it, with its rules, names that directory:

```toml
package_search_dir = [["buildlog", "/opt/mylib/share/buildlog"]]
```

The CPS file the same install writes names this package as its sibling (`x_buildlog_manifest`), so a plain prefix search (`package_prefix`) sees one candidate and binds the `buildlog` route, facts and shipped rules, without ambiguity and without a format preference; the CPS file stays for other CPS consumers. A third-party CPS file without that marker next to a manifest is ambiguous until pinned, and a marker that names no manifest of the same package in the prefix is an error. The package is pinned in `buildlog.lock`; `buildlog why` on an imported fact names the installed facts file and line. Every configure with such a package takes one more pass than the CPS import (about 1.8 times the time for a small project: 3.2 s against 1.7 s); the lock pins the package and checks its hash but never loads it.

#### Integrating a resolver

A resolver (Spack, Homebrew, Debian, an ASP solver) decides which build of each package is used; buildlog does not. The intended integration is that the resolver's output is a user file naming the exact `manifest.toml` of each chosen package, with no search at all:

```toml
package_file = [["buildlog", "/opt/spack/zlib-1.3-abc/share/buildlog/zlib/manifest.toml"]]
```

`package_file` imports the named manifest outright (and with it the files its `imports` lists). Alternatively `package_pin = [["zlib:z", "<manifest>"]]` selects a manifest among those a search found, and binds every name the package defines; a pin of the CPS file chooses that route instead, even where its sibling manifest is found. A search directory (`package_search_dir`, `package_prefix`) is the convenience fallback for a hand-written setup. It never picks a first match: the `buildlog` provider enumerates the package under every directory and prefix, selection happens in the core, and two candidates for one name are an `ambiguous package` error until a pin names one. The resolver's job is to hand over a unique set, and buildlog checks that it did.
