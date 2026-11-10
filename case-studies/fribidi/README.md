<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: building fribidi with buildlog (the Meson baseline)

GNU FriBidi 1.0.17 is small and builds with Meson. It is the first case study
whose baseline is not CMake. Two things in its build matter here. First, host
programs generate tables during the build: `gen.tab/` builds seven programs
marked `native: true`, and runs them over the Unicode data files to produce the
`*.tab.i` headers and `fribidi-unicode-version.h` that the library compiles.
Second, it has configure checks (`cc.has_header`, `cc.has_function`,
`cc.sizeof`) that feed a template-less `config.h` and a templated
`fribidi-config.h`.

This study builds fribidi with buildlog and compares the result with Meson's
build on this Mac. The comparison covers the table programs (built for the exec
platform because they are used as generators; nothing marks them), the tables
(byte-identical), both configured headers, the shared library, the CLI and the
other programs, and the nine tests. It also covers an x86_64 cross build
against Meson's cross file. Install, `fribidi.pc` and the man pages are out of
scope.

## Layout

```
case-studies/fribidi/
  README.md                 this file (ledger and design input)
  baseline/
    run-meson.sh            Meson setup/compile/test for debug (Meson's default), release and
                            cross-x86_64; writes the normalized files below
    normalize_meson.py      a Meson build dir -> the case studies' normalized JSONL records
    x86_64-darwin.ini       the Meson cross file of the cross-x86_64 build
    environment.txt         tool versions and the fribidi pin
    meson-<variant>.jsonl   the normalized command graphs (debug, release, cross-x86_64)
    commands-<variant>.txt  `ninja -t commands`, with $SRC/$BUILD
    checks-<variant>.txt    the configure checks as `meson setup` reports them
    options-<variant>.txt   the project options and the graph-relevant built-in options
    meson-test-<variant>.txt meson test's results
    release/                the release build's introspection (targets, tests without env),
                            compile_commands.json, config.h (+ the debug build's diff),
                            fribidi-config.h, fribidi-unicode-version.h, tables.md5,
                            exported-symbols.txt, dylib-metadata.txt
    tests/                  the normalizer's tests and a miniature Meson build dir fixture
    raw/                    gitignored: the build trees and logs (meson-logs/testlog.txt holds
                            the environment: never commit it)
  project/                  the author's project: fribidi as its meson.build files state it
    buildlog.toml           the spec list; [vars] FRIBIDI_SRC (the checkout)
    spec.toml               configuration, option, profiles, version, checks, incs, libfribidi
    gen.toml                gen.tab: generator templates, native_args, gen-unicode-version, tables
    configured.toml         config.h and fribidi-config.h (owner `config`)
    programs.toml           bin/ programs, test programs, test-runner.py, the nine tests
    rules.dl                remaining project abstractions (configured variables, scripts, tests)
    presets/apple-clang-arm64.toml  the user file for this Mac: toolchain pins
    presets/cross-x86_64.toml       a user file adding an x86_64-macos release configuration
tests/test_fribidi_case.py  the acceptance tests against the baseline
```

## Pinned inputs

- **fribidi v1.0.17**, commit `b93119f5` (`b93119f5fdc7ea47672cc304c1455ffa6dfe7536`), a
  submodule at `case-studies/fribidi/upstream` (https://github.com/fribidi/fribidi.git). `FRIBIDI_SRC` overrides the path
  (`-D FRIBIDI_SRC=...` or `BUILDLOG_FRIBIDI_SRC`, which the tests read too, skipping
  cleanly without a checkout). `run-meson.sh` checks the commit and a clean tree first.
- **Tools:** Meson 1.12.1, Ninja 1.13.2, Apple clang 21.0.0, ccache (Meson uses it on its
  own), macOS 26.5.2 on arm64 (`baseline/environment.txt`).
- **Meson configuration:** `-Ddocs=false`, everything else default, as `meson-debug`
  (Meson's default buildtype, `debug`) and `meson-release` (`--buildtype=release`). The
  comparison uses the release build, since buildlog's configuration is `native.opt=release`.
  Meson's `default_library` is `shared` (`options-release.txt`), so the library is
  shared here too.

## Running

```sh
case-studies/fribidi/baseline/run-meson.sh           # the Meson baselines
uv run --frozen buildlog configure --user case-studies/fribidi/project/presets/apple-clang-arm64.toml \
  -C build/fribidi case-studies/fribidi/project
ninja -C build/fribidi/souffle -j2
uv run --frozen buildlog test -C build/fribidi -j2
uv run --frozen pytest -q -n0 tests/test_fribidi_case.py case-studies/fribidi/baseline/tests
# the cross build: the x86_64 configuration only
uv run --frozen buildlog configure --user case-studies/fribidi/project/presets/cross-x86_64.toml \
  --config x86_64-release -C build/fribidi-x86_64 case-studies/fribidi/project
```

## The Meson normalizer

`baseline/normalize_meson.py` writes the same records as the CMake baselines
(`case-studies/halide/baseline/normalize.py`, whose parser, decomposition,
classification and path canonicalization it reuses): one JSON line per Ninja edge,
machine paths as `$SRC`, `$BUILD`, `$HOMEBREW`, ..., commands decomposed into steps,
kinds derived from commands (`compile`, `link` with `link_type`, `custom`, `phony`,
`meta`), and a target per edge. Only the Meson-specific parts are new:

- **Target attribution** comes from `meson introspect --targets`, not CMake's File API. An
  edge whose output is a target's `filename` belongs to it. So does an edge whose output
  lies in the target's private directory `<filename>.p/` (objects, `.symbols`). Records
  carry `target`, `target_id`, `target_type`, `machine` (the target's compilers' machine,
  `build` for `native: true` in a cross build) and `dir` (from `defined_in`). Meson lets
  targets share a name: fribidi's library and CLI are both `fribidi`, and each gen.tab
  program shares its name with the custom target running it. Colliding names get their
  id's type suffix (`fribidi@sha`, `fribidi@exe`, `gen-bidi-type-tab@cus`).
- **Wrappers.** `meson --internal exe --capture F -- cmd` becomes a step running `cmd`
  with stdout `F` (and `--feed`/`--chdir` likewise). The ccache launcher becomes the
  step's `wrapper`. Both additions are generic, in the shared `decompose`, and CMake
  graphs contain neither, so the Halide baselines do not change. Meson's other steps
  (`symbolextractor`, `regenerate`, test, install, dist, ...) classify as `meta` and
  are excluded as identity-only records.
- Meson writes include directories relative to the build directory (`-Ilib`, `-I.`). They
  become `-I$BUILD/lib` and `-I$BUILD`.

`baseline/tests/test_meson_normalizer.py` covers each point on a miniature Meson build
directory, and re-derives `meson-release.jsonl` from `raw/` when that exists.

## How the project states fribidi

Before the UX pass it had 529 lines (without presets) against Meson's 390 (the meson.build files
and meson_options.txt, without doc/). At that point `rules.dl` had six small abstractions, each
named for the Meson idiom it replaced:

1. **Property sets** (`propset`, `uses_propset`). These are Meson's variables holding
   arguments: `incs` (`include_directories('.', 'lib', 'gen.tab')`) and gen.tab's
   `native_args`, whose `HAVE_*` defines depend on checks.
2. **Generated files** (`generated_file(owner, file, tool)`, `generated_arg`,
   `generated_input`). A custom target with `capture: true` whose command[0] is a built
   executable. The action is `action_run` of the tool by target reference plus
   `action_stdout`. The file belongs to an owner, as a configured file does: it is
   generated once per configuration under `gen/<cfg>/<owner>/`, and it is every member's
   public generated header. Owners: `unicode_version` (members: the library and the six
   table programs) and `tables` (the library).
3. **Unicode tables** (`unicode_table`, `unicode_table_input`, `unicode_table_compression`).
   The `foreach tab : tabs` loop: a program `gen-<n>-tab` from `gen-<n>-tab.c` and
   `packtab.c`, and a generated `<n>.tab.i`.
4. **Owner variables** (`owner_define`, `owner_check_define`, `owner_check_size`, ...).
   Meson's `configuration_data`: one set of values for a generated file, which core
   needs as properties of every owner member.
5. **Script programs** (`script`). `find_program('test-runner.py')` is a source file run
   by its `#!` line. buildlog runs only built artifacts, so the script is copied (mode
   kept) to `bin/<cfg>/`, and the copy is the run artifact.
6. **Reference tests** (`reference_test(charset, suffix)`). The rows of
   test/meson.build's `tests` table.

The version is stated once (`project_version = "1.0.17"` and
`library_interface = ["libfribidi", "0", "4", "0"]`). The rules derive the library's
version and `fribidi-config.h`'s major, minor, micro and interface version from it.

## Ledger

Acceptance: `tests/test_fribidi_case.py` (9 tests) and
`case-studies/fribidi/baseline/tests/` (7 tests).

### The table programs and the tables

**Matches the baseline.**
- The six `*.tab.i` and `fribidi-unicode-version.h` are **byte-identical** to Meson's
  (`release/tables.md5`, also identical between Meson's debug and release builds). They
  are also identical in buildlog's x86_64 cross build.
- Each program runs as Meson runs it: `gen-<n>-tab 2 <inputs...> gen-<n>-tab`, with stdout
  to the table, the inputs as explicit inputs and the program as an implicit one.
- The programs compile with Meson's `native_args`, and the three `HAVE_*` headers come from
  checks. packtab.c is compiled once per program in both builds (42 objects in all).
- **Exec platform, derived.** No row says that a gen.tab program is a host tool.
  `buildlog why --output bin/<cfg>/gen-bidi-type-tab` derives the link from
  `needed(gen-bidi-type-tab, c)` ← `tool_dep("tables", c, "gen-bidi-type-tab")` ←
  `tool_ref(..., "out:gen/<cfg>/tables/bidi-type.tab.i", "gen-bidi-type-tab", "run")` ←
  `action_run` from `[generator.table1]` and `target_action(tables, ...)`, with
  `exec_config(c, c)` for this native build. In the cross build the programs are built
  once, for arm64, whichever configuration's library runs them. Meson needs the author
  to write `native: true` on all seven `executable()`s.
- **Their checks are asked of the exec toolchain.** `why --flag -DHAVE_STRINGS_H` on a
  gen.tab object in the cross build leads to `cc_check_in("HAVE_STRINGS_H",
  arm64-macos.release.none, "apple-clang", "arm64-macos", "arm64-macos")`. The condition
  is evaluated in the program's own (exec) configuration, so the answer is the arm64
  toolchain's.

**Differs, and why.**
- **An upstream bug that buildlog cannot reproduce.** gen.tab/meson.build:3 says
  `native_cc = meson.get_compiler('c')` but means the build machine's compiler, which
  needs `native: true`. So its "native" `has_header` checks ask the host (cross) compiler.
  In the cross build Meson answers them `(cached)` from the x86_64 checks
  (`checks-cross-x86_64.txt`). The answers agree here only because both targets use one
  SDK. In buildlog the check follows the configuration of the target that uses it, so
  there is nothing to get wrong.
- **Where the tables go.** Meson writes to `gen.tab/` in the build tree. buildlog writes
  to `gen/<cfg>/tables/` and `gen/<cfg>/unicode_version/`. The directories reach the
  compiles as generated include dirs: `-I` in the library, `-isystem` in its users.
- **Generated once per consumer configuration.** A generated file's owner is keyed by its
  members' configuration, as configured files are. In the cross build,
  `fribidi-unicode-version.h` is therefore generated twice: once for the arm64 programs
  that include it, once for the x86_64 library. With two configurations of the library,
  all seven files are generated twice by the same arm64 programs, with identical
  results. Meson has one custom target for both machines. See design input 5.
- **Meson's generator runs go through `meson --internal exe --capture`.** buildlog's go
  through Ninja's command with `> out` (`action_stdout`).

### Configured headers and configure checks

**Matches the baseline.**
- All nine checks agree with `meson setup` (`checks-release.txt`): `memmove`, `memset`,
  `strdup`, five headers and `sizeof(int) = 4`. They are buildlog-cpp `cc_check`s, whose
  answers are probe results.
- **`config.h` defines what Meson's does.** Every `#define` has the same name and value,
  and `FRIBIDI_NO_DEPRECATED` is undefined in both (option `deprecated` on).
  `DEBUG` is defined only in debug builds (`release/config.h.debug-diff`). Here it is a
  `$Eq(native.opt, debug)` condition, so it is undefined in the release configuration.
- **`fribidi-config.h` is identical except its first line** ("generated by buildlog" vs
  "by Meson", a `configure_input` value). Its version values come from the stated version
  (rules.dl). The MSVC placeholder comes from the C toolchain's family, which is Meson's
  `cc.get_id() == 'msvc'`.

**Differs, and why.**
- **`config.h` spelling.** Meson writes `configuration_data` without a template: a
  header comment, `#pragma once`, sorted keys each followed by a blank line, `#undef X`
  for false. buildlog has no template-less form, so configured.toml's text rows
  reproduce Meson's layout with `#cmakedefine`/`@VAR@`. A false value then reads
  `/* #undef X */`, and the comment says buildlog. The test compares the macro sets.
- **One owner for both headers.** Configure variables are properties of the owner's
  members, and the members must agree. Meson has one `configuration_data` per file:
  `cdata` for config.h, `fribidi_config` for fribidi-config.h. Two owners would give
  the library both variable sets, merged, so both headers share one owner `config`
  and one variable set. It works because the names do not collide. See design input 4.

### The library and the programs

**Matches the baseline.**
- The same 42 objects, each from the same source with the same defines (except
  `-DNDEBUG`, below), searching the same source directories (except Meson's implicit
  ones, below). `-fvisibility=hidden` and `-DHAVE_CONFIG_H` appear where Meson has them.
  `test-reorder-nsm` has no `HAVE_CONFIG_H` in either build.
- **The same 36 exported symbols** (`nm -gU`, `release/exported-symbols.txt`).
- Both libraries have the install name `@rpath/libfribidi.0.dylib`, and the programs
  link it with an `@loader_path` rpath.

**Differs, and why.**
- **The file name and current_version.** Meson names the file on Darwin by the soversion
  alone, `libfribidi.0.dylib`, with compatibility and current version `0` (Meson's
  `darwin_versions` default). Its `libfribidi.dylib` symlink is made at setup and is not
  an edge. buildlog's `native_version("0.4.0", "0")` gives `libfribidi.0.4.0.dylib`,
  symlink edges `libfribidi.0.dylib` and `libfribidi.dylib`, compatibility version
  0.0.0 and current version 0.4.0.
- **`-DNDEBUG`.** buildlog's release configuration defines it. Meson's `b_ndebug`
  defaults to `false`, so its release keeps assertions. No file the build compiles
  includes `<assert.h>` (only the fuzzer, not built), so the output is the same.
- **Warnings and colour.** Meson's default `warning_level=1` adds `-Wall -Winvalid-pch`,
  and it adds `-fdiagnostics-color=always`. buildlog adds no warning flags by default.
- **ccache.** Meson finds ccache and prefixes every compile with it, with no option set.
  buildlog does not (a user decision, if anything).
- **PIC.** buildlog puts `-fPIC` on the shared library's objects. Meson omits it on
  Darwin, where it is the default.
- **Target spelling.** buildlog writes `--target=arm64-apple-macos13 -isysroot <sdk>` and
  the full clang path. Meson writes `cc`.
- **Implicit include directories.** Meson adds each target's private dir
  (`<target>.p`), its build dir and its source dir (`implicit_include_directories:
  true`), so a program in bin/ gets `-I$SRC/bin`. buildlog adds only what the target
  states (`incs`) plus the generated dirs. Quote includes of same-directory headers
  (`getopt.h`) resolve without it.
- **Link flags.** Meson links everything with `-Wl,-dead_strip_dylibs
  -Wl,-headerpad_max_install_names`, the library with `-shared`. buildlog links with
  `-dynamiclib` and neither flag.
- **Relink avoidance.** Meson extracts the library's exported symbols
  (`symbolextractor`, a `restat` edge), and the programs depend on the `.symbols` file,
  so an implementation-only change to the library does not relink them. buildlog has no
  equivalent. See design input 7.
- **Layout.** Meson mirrors the source tree (`bin/fribidi`,
  `test/unicode-conformance/BidiTest`). buildlog uses `bin/<cfg>/<name>`, so the rpath is
  `@loader_path/../../lib/<cfg>` instead of `@loader_path/../lib`.
- **Symbol visibility is a raw flag.** Meson's `gnu_symbol_visibility: 'hidden'` is a
  typed target property. The spec states `-fvisibility=hidden` as a compile flag.

### Tests

**Matches the baseline.** `buildlog test` runs meson test's nine tests by the same names:
the six `test-runner.py` reference tests, `reorder-nsm`, `BidiTest` and
`BidiCharacterTest`. All nine pass, in both builds.

**Differs, and why.**
- **The runner is a copied script.** Meson runs `test/test-runner.py` from the source tree
  through its `#!`. buildlog runs `bin/<cfg>/test-runner.py`, a copy (rules.dl 5), and
  passes the fribidi program by reference (`test_arg_ref`).
- **A test whose program nothing requests silently does not exist.** Before
  `test-runner.py` was in `request_all`, `buildlog test` ran three tests and reported
  nothing about the other six. See design input 6. *Fixed in step 4 (bug B1):* a test
  requests its program in core, and `request_all` no longer lists the four test programs.
- **Environment.** Meson sets `MALLOC_PERTURB_` (random), the sanitizer `*_OPTIONS`
  variables and `DYLD_LIBRARY_PATH`, with a 30 s timeout. buildlog sets none of these.
  The rpaths make the library path unnecessary.

### Cross build (x86_64 on this arm64 Mac)

**Matches the baseline.** `presets/cross-x86_64.toml` is a user file: toolchain pins for
x86_64-macos and a configuration `x86_64-release`. Nothing in the project changes.
Configured alone (`--config x86_64-release`), buildlog builds the seven gen.tab
programs once, for arm64, and the library and programs for x86_64 (`lipo -archs`). That
is 42 objects, split between the machines as in Meson's cross build, where
`meson-cross-x86_64.jsonl` labels the gen.tab targets `machine: build`. The tables are
byte-identical. In buildlog, `native: true` has no counterpart and needs none. In Meson
it has two parts: the author's annotation, plus the user's cross file
(`x86_64-darwin.ini`), which defines the host machine and leaves the native machine as
the default `cc`.

**Differs, and why.**
- **Tests.** buildlog runs none of the nine x86_64 tests (`not run`: built for
  x86_64-macos, and the build machine is arm64-macos). Meson has no `exe_wrapper`, so it
  skips the three that run an x86_64 program directly (exit 77). It runs the six
  `test-runner.py` tests, because the script is not a cross program. Those pass only
  because Rosetta runs the x86_64 `fribidi` the script starts. Meson's answer is right
  here only by accident, and buildlog's is conservative.
- **A native build's introspection says `host` for `native: true` targets.** Meson
  collapses the build and host machines in a native build, so `machine: build` appears
  only in the cross graph.

### Meson behaviour outside the graph

- **Default setup fails from a git checkout.** `docs` defaults to `true` and needs
  `c2man` and `help2man`, because only release tarballs ship pregenerated man pages. The
  baselines use `-Ddocs=false`, and buildlog does not state the doc targets.
- `bin` and `tests` (options, default true) gate subdirectories. buildlog has no
  conditional request (as in zlib), so the programs and tests are unconditional, which
  matches the defaults. `fuzzer_ldflags` (a string option) and the fuzzer are not stated.
- The Windows-only define `FRIBIDI_BUILD` is not stated. `FRIBIDI_LIB_STATIC` is stated
  since step 4, as a usage requirement of the static flavour (see "Step 4 changes").

## Design input

Ranked by how much they bear on the design.

1. **Linkage as a parameter: Meson's `default_library` is the user-level default, and
   the author must not read it.** Meson has three author forms. `shared_library()` and
   `static_library()` are author-fixed, like buildlog's `native_kind`. `library()`
   leaves the linkage to the user's built-in option `default_library=shared|static|both`,
   which is per build and overridable per subproject (`-Dfribidi:default_library=static`).
   `both_libraries()` is the author asking for both. With `both`, or `both_libraries`
   and `b_staticpic=true`, Meson compiles one object set and links it both ways. fribidi
   writes `library()`, so its linkage is the user's.
   The weak spot is that fribidi's meson.build also *reads* the option:
   `if get_option('default_library') == 'static'` adds `-DFRIBIDI_LIB_STATIC` to the
   library, to `libfribidi_dep`'s compile args and to fribidi.pc. With `both`, the static
   variant silently lacks it. The author needed a usage requirement conditioned on the
   *instance's* linkage, but Meson only offers a build-wide string. The buildlog
   direction (notes, "Compatibility with a parameterised library") avoids this: one
   library target, linkage a parameter instantiated on demand, with a user default. The
   Meson analogue of that default is `default_library`, per project. A downstream request
   can pin a linkage, as Meson's `dependency('fribidi', static: true)` does for imports.
   The author would then state `prop_when(libfribidi, "cc.define", "FRIBIDI_LIB_STATIC",
   "public", is_static)` against the instance, and `both` becomes two instances whose
   conditions differ. Two points to decide: (a) whether objects are shared between
   instances when their compile properties agree (Meson's `both_libraries` object reuse);
   (b) the configured-file caveat already in the notes, since `fribidi-config.h` does not
   depend on linkage and should not be generated per linkage.
2. **Meson's options split into user and author as buildlog's do, except for types and
   namespaces.**
   - *Built-in options* are the user's, set at `meson setup -D...`. An author may only
     give defaults, in `project(default_options: ...)`, and the user overrides them.
     They map as follows:
     - `buildtype`, `optimization`, `debug`: configuration settings (`native.opt`,
       `config_decl_setting`);
     - `b_sanitize`: `native.sanitize`;
     - `werror`: the user's `cc_werror`, which buildlog already rejects from authors;
     - `prefix`/`libdir`/...: the install-dir user overrides;
     - `c_args`/`c_link_args`: user flags;
     - `default_library`: item 1;
     - `b_ndebug`: not a separate knob in buildlog. `-DNDEBUG` follows `native.opt`;
     - `warning_level`: no counterpart. Warnings are flags an author states, but the
       *level* is a user choice (like `-Werror`), and a portable `native.warnings`
       setting spelled by the toolchain family would map it.
   - *Project options* (`meson_options.txt`: `option('deprecated', type: 'boolean', value:
     true)`) are `option_decl` (author, with default) and `option_value` (user), read as
     `get_option()`, as buildlog's `option.<n>` attributes are. Meson's types
     (boolean, combo, integer, string, array, feature) have only a boolean counterpart
     here. fribidi needs a string (`fuzzer_ldflags`). Meson's `feature` type
     (enabled/disabled/auto, where auto means use it if a check finds it) is the natural
     type for "optional dependency" options and fits buildlog's probes-as-discovery.
   - *Namespaces:* Meson namespaces a subproject's options by project,
     `-Dfribidi:docs=false`. The parent's *author* may set them in `subproject('fribidi',
     default_options: [...])`, as defaults only, and the user overrides. An option
     declared `yield: true` takes the parent's value of the same name, and some built-ins
     (`default_library`, `werror`, `warning_level`) are per subproject. This answers the
     notes' open question for nlohmann-style namespaced options:
     `<project>.<option>`; a parent author gives defaults, never final values; the user
     decides last; an explicit `yield` shares one name.
3. **`native: true` is derivable, and deriving it removes a bug class.** Meson makes the
   author state the machine twice. `native: true` goes on each tool `executable()`, and
   `meson.get_compiler('c', native: true)` must be used for the tool's checks. fribidi
   gets the second wrong (ledger), which is invisible in native builds. buildlog derives
   the exec configuration from use (`action_run` → `tool_dep`), and a check's answer
   comes from the configuration of the target that uses it, so both annotations
   disappear. This is the paper's claim, checked against a Meson baseline, with `why` as
   evidence. One gap: a program used both as a tool and as a product would be built for
   both configurations, as needed. Meson cannot express that (a target has one machine),
   and fribidi does not need it.
4. **Configure-file variables are per target; Meson's are per file.** Meson's
   `configuration_data` object is a value that is passed to `configure_file`. Two headers
   with two variable sets are two objects. In buildlog the variables are properties of
   the owner's members, so one target cannot be a member of two owners with different
   sets without merging them. fribidi works around this with one owner for both headers
   (rules.dl 4 then states the sets once per owner). A `configure_file`-level variable
   relation (keyed by owner and file) would express Meson's model directly and remove the
   members-must-agree check for variables no member uses. Related: Meson's template-less
   `configure_file(configuration: cdata)` is a common form (config.h), and buildlog
   needs a hand-written template for it.
5. **Generated files have no core vocabulary, and their placement is per consumer
   configuration.** rules.dl 2 is about 25 lines of project rules over core
   `action_run`/`action_stdout`/`target_action`/`prop_in`. Halide wrote its own
   equivalent. A plugin-level "custom command" (tool by reference, inputs, captured
   stdout, owner members get the dir) is due, since three case studies want it. Its key
   is open. fribidi's tables depend only on the tool and inputs, not on the consumer's
   configuration, so keying them on the exec configuration would generate them once per
   exec platform (Meson generates once). That would need a way to say "configuration-
   independent output".
6. **Tests of unrequested programs vanish silently.** A `test` row whose program is not
   needed in any configuration is dropped, and `buildlog test` does not mention it.
   Either a test should make its program needed (Meson's tests build their
   dependencies), or its absence should be a warning. Script programs (rules.dl 5) are
   a second gap: a test program that is a source file needs a copy action today.
7. **Things Meson does that buildlog lacks.**
   - relink avoidance through a symbol file (`symbolextractor` + `restat`);
   - an automatic compiler launcher (ccache);
   - `MALLOC_PERTURB_` and sanitizer options in the test environment;
   - implicit per-target include dirs, which buildlog should *not* copy (they hide
     missing `include_directories`);
   - typed symbol visibility (`gnu_symbol_visibility`);
   - `darwin_versions`.
   The first and third are worth having. The second is a user decision
   (a `cc_launcher` user relation).
8. **Library versioning, recorded here and not implemented.** Upstream (meson.build)
   gives three numbers:
   - project `version: '1.0.17'`, the package version;
   - `soversion = 0`, the ABI major. It sets the install name and SONAME:
     `@rpath/libfribidi.0.dylib`, or `libfribidi.so.0` on ELF;
   - `libversion = '0.4.0'`, formatted from `soversion` and `interface_version = 4`,
     with interface_age 0.

   The comment at the top of meson.build states the policy. A bug-fix release bumps
   `interface_age`. Added interfaces bump `interface_version` and reset the age. An
   ABI break bumps `soversion`.

   On macOS, Meson's defaults (no `darwin_versions`) give the dylib compatibility
   version 0.0.0 and current version 0.0.0 (`release/dylib-metadata.txt`).
   buildlog's `native_version("0.4.0", "0")`, derived in rules.dl from
   `library_interface`, gives compatibility 0.0.0, current 0.4.0 and the file
   `libfribidi.0.4.0.dylib`. That is closer to libtool's reading, where current
   version encodes the interface.

   These numbers bear on an exported version constraint (CPS `compat_version`,
   buildlog's `export_compat`). What a downstream `requires fribidi >= X` means has
   two readings:
   - the package version, 1.0.17 (for fribidi.pc and CPS `version`);
   - the ABI: any 1.x with `soversion` 0 is link-compatible. So `export_compat =
     "same_major"` (compat_version 1.0.0) is right for 1.0.x, provided upstream keeps
     `soversion` tied to the package major. fribidi does, with the soversion at 0
     since 1.0.

   The two readings can come apart, because the package version and the soversion
   are independent numbers. A modelled version would state `soversion`,
   `interface_version`/age and the package version as separate facts. It would
   derive install_name, SONAME, compatibility/current version and the CPS
   `compat_version` from them, and check that `export_compat` does not promise more
   than the soversion does (for example, `any_newer` across a soversion bump).
   Downstream, `requires` would constrain the package version, and the soversion
   would be recorded in the lock or import facts, so that a relink is needed exactly
   when it changes. Meson's `darwin_versions` is the macOS knob for this;
   fribidi does not set it.
9. **Include dir and ordering are one statement.** A generated header's dir and its order
   edge come together (`cc.generated_header`). Meson separates them: a generated header
   is a *source* of the target, which orders the build, and include dirs are stated
   apart. fribidi lists the headers as sources of `library()` and in
   `declare_dependency(sources:)`. buildlog's coupling is simpler and was never wrong here.

## Step 4 changes

Spec lines are measured with `historical source-line accounting` (non-blank, non-comment, without
single-path source-list lines; it counts configured.toml 4 lines lower than the table
below, so its totals are 345 before step 4).

| Change | Files | Before | After |
|---|---|---|---|
| B1: a test requests its program (the four test programs leave `request_all`) | programs.toml | 70 | 66 |
| §1(d): `libfribidi` is a `library` whose linkage is the user's (`default_linkage = "shared"`, Meson's `default_library` default); `FRIBIDI_LIB_STATIC` becomes `linkage_prop` on the static flavour | spec.toml | 69 | 71 |

The default graph is byte-identical before and after the linkage change (native and
cross). `-Dfribidi:default_library=static` corresponds to the user file line
`library_linkage = [["libfribidi", "static"]]`, or a configuration setting
`native.linkage = "static"` for the whole build (Meson's global `default_library`). With it,
the library is `libfribidi.a`, its objects are not PIC, and `-DFRIBIDI_LIB_STATIC` reaches
the library's objects and its users' (`fribidi`, the table tests). All 9 tests pass
(`test_static_linkage_is_the_users_and_carries_fribidi_lib_static`). The flaw in design
input 1 goes away: a build with both flavours has two configurations, and only the static
one carries the define. The author states the define unconditionally, so it is not gated on
Windows. fribidi-common.h reads it only under `_WIN32`, so elsewhere it has no effect.
Not validated: fribidi.pc, since the project states no pkg-config file (Meson's
`extra_cflags: fribidi_static_cargs`). Point (a) of design input 1 also stays open: the two
flavours share no objects, because they are two configurations.

## UX input

Frictions hit while authoring and running this study. Each item gives what I
wrote, what I wished I could write, and the line count. Nothing here is
implemented.

**Spec size** (non-blank, non-comment lines, without single-path source-list lines):

| | lines |
|---|---|
| buildlog project: buildlog.toml 5, spec.toml 69, gen.toml 39, configured.toml 85, programs.toml 70, rules.dl 81 | **349** |
| of which Datalog (rules.dl) | 81 |
| of which config.h text rows (item 3) | ~40 |
| presets (user): apple-clang-arm64 4, cross-x86_64 8 | 12 |
| Meson: meson.build 55, meson_options.txt 10, bin/ 60, gen.tab/ 55, lib/ 40, test/ 31, test/unicode-conformance/ 12 (doc/ excluded) | **263** |

Without the config.h text rows and rules.dl, the TOML alone is about 230 lines,
close to Meson's size. The excess is items 1–3.

1. **Generic abstractions had to be written in Datalog** (rules.dl, 81 lines). Property
   sets, generated files (a tool's captured stdout), script programs, and version
   splitting are all generic. Version splitting is especially awkward: splitting
   "1.0.17" on dots with `substr`/`range` takes 5 lines of Datalog:
   ```
   version_parts(v, substr(v, 0, i), substr(v, i + 1, j - i - 1), substr(v, j + 1, strlen(v) - j - 1)) :-
     project_version(v), i = range(0, strlen(v)), substr(v, i, 1) = ".", j = range(i + 1, strlen(v)),
     substr(v, j, 1) = ".".
   ```
   What I wished I could write instead: `project_version = "1.0.17"` with core deriving
   `version.major/minor/micro` as configure variables. The generated-file rule (about 25
   lines) should be a TOML table:
   ```toml
   [generated.bidi-type.tab.i]
   tool = "gen-bidi-type-tab"
   args = ["2", "src:gen.tab/unidata/UnicodeData.txt", "gen-bidi-type-tab"]
   stdout = true
   members = ["libfribidi"]
   ```
   A property set (6 lines of Datalog) should be `[target.x] uses = ["incs"]` over a
   core `[propset.incs]`.
2. **The configure-check → condition → define chain is spelled three times.** For
   one header check I wrote the check (4 lines), two conditions
   (`["has_strings_h", "$Eq(cc.check.HAVE_STRINGS_H, yes)"]`,
   `["no_strings_h", "$Eq(cc.check.HAVE_STRINGS_H, no)"]`) and the define/undefine rows.
   rules.dl 4 (`owner_check_define`, `owner_check_10`, about 15 lines) automates it for
   configured files, but `native_args` still needs the hand-written conditions.
   What I wished I could write: `prop_when = [["cc.define", "HAVE_STRINGS_H",
   "cc.check.HAVE_STRINGS_H"]]`, with a check name as a condition, plus
   `cdata.set10`-style kinds on configure variables. Likewise, every boolean
   condition needs its negation as a second row (`debug`/`not_debug`,
   `deprecated`/`no_deprecated`, 4 extra lines). A `!name` in condition references
   would remove them.
3. **Template-less config.h.** Meson's `configure_file(output: 'config.h',
   configuration: cdata)` is one line. Here it took 33 `configure_file_text` rows, 41
   lines after tombi wraps the long ones. Each row repeats `"config", "config.h"`.
   What I wished I could write: `[configure_file.config."config.h"] generate =
   "defines"`, with the file written from the variables, sorted.
4. **Owner name repeated on every row.** `owner_check_define`, `owner_check_10`,
   `owner_define`, `owner_define_when`, `owner_undefine_when` and `owner_member` repeat
   `"config"`; configured.toml spells `"config"` 66 times (33 of them in the config.h text rows), and `configure_file`/`configure_file_at_only`/`cc_configured_header`
   repeat `["config", "<file>"]` three times each. What I wished I could write: one
   `[owner.config]` table with `files = [...]`, `members = [...]`, `define = {...}`,
   `define_when = [...]`.
5. **Near-identical program tables.** The seven programs in programs.toml share
   `native_kind`, `uses_propset = "incs"`, `HAVE_CONFIG_H` and `dep = { libfribidi =
   "private" }`, so there are 5 repeated lines × 7 (about 35 lines). Meson has the same
   problem but hides it in a `foreach`. What I wished I could write: a defaults table,
   `[target_defaults.bin] native_kind = "executable" ...`, or `extends = "program"`.
6. **Membership stated twice.** A program that uses `config.h` is a member of the
   `config` owner (`owner_member`, 7 rows in configured.toml) *and* gets
   `-DHAVE_CONFIG_H` in its own table. What I wished I could write: membership as a
   field of the target (`configured = ["config"]`), or inferred from the define.
7. **TOML placement rules bit three times.**
   - Keys after a `[table]` header belong to that table, so top-level rows
     (`reference_test`, `test`, `test_arg`) must precede the first `[target.*]`. I
     reordered programs.toml by hand.
   - Positioned rows (`generated_input` with an index) cannot live in a subject table.
     The error was `gen.toml:42: generated_input does not have values for all
     non-number columns`, which gave no hint to move the rows to the top level and
     restate the owner (2 extra lines).
   - A subject table's symbol column cannot take a list:
     `reference_test.suffix must be scalar`. `[reference_test.CapRTL] suffix =
     ["explicit", "implicit", "isolate"]` had to become 6 top-level rows.
8. **A confusing visibility error.** `project: body relation cc_check is private to
   buildlog-cpp (not exported or open)`. The rule only wanted a check's language and
   subject by name. The workaround restates them: `owner_check_size = [["config",
   "SIZEOF_INT", "c", "int"]]`, which duplicates the `[cc_check.SIZEOF_INT]` table. What I
   wished for: `cc_check` exported read-only, or the error naming an exported
   alternative (e.g. `cc_check_type_size`).
9. **A test silently vanishes when its program is not requested.** My script target was
   named `test-runner`, and the tests referenced `test-runner.py`. Even after I fixed the
   name, the six tests did not exist until I added `["test-runner.py"]` to
   `request_all`. There was no error and no warning; `buildlog test` just reported
   "3 passed". What I wished for: tests make their program needed, or an "unknown
   program"/"not built" warning (1 line to fix, about 15 minutes to find).
10. **A script program costs a copy rule.** `find_program('test-runner.py')` needed
    rules.dl 5 (8 lines), a `[script."test-runner.py"]` table and a `request_all` row.
    What I wished I could write: `test = [["CapRTL_explicit", "src:test/test-runner.py"]]`,
    with a `src:` path accepted as a test program.
11. **`why` output.** It was readable once found, but:
    - `--output` needs the exact Ninja path with the configuration directory
      (`bin/arm64-macos.release.none/gen-bidi-type-tab`), where a target name was the
      natural question ("why is gen-bidi-type-tab built for this config?");
    - the default depth prints 60+ lines with `… 4 more` elisions, and `--depth 4`
      was needed;
    - the ‹arm64-macos.release.none› legend at the bottom must be read first;
    - the answer I wanted, "tool_dep because bidi-type.tab.i runs it", is 3 lines deep,
      under `executable` and `needed`.

    What I wished I could write: `buildlog why gen-bidi-type-tab --config release`,
    answering with the demand chain first.
12. **CLI friction.**
    - `pytest -p no:xdist` fails, because pyproject's `addopts = "-n auto"` needs
      xdist. The only light option is `-n0`.
    - For a cross-only build, `configure` needs both `--user presets/cross-x86_64.toml`
      and `--config x86_64-release`. Without `--config`, the arm64 configuration is
      also built.
    - A reconfigure without `--user` silently reused the previous user file. That is
      convenient, but surprising when switching presets in one build dir.
13. **File sprawl.** There are six project files plus two presets of 4 and 8 lines. The
    split (spec/gen/configured/programs) mirrors Meson's subdirs, and all of them are
    listed in buildlog.toml's `spec = [...]`. The presets are tiny because the only
    per-machine decision is the toolchain pin, which repeats one tuple per language:
    `["c", …]` and `["native-link", …]` with identical platforms and toolchain (2
    lines per platform). What I wished I could write: `toolchain_pin = [["*",
    "arm64-macos", "arm64-macos", "apple-clang"]]`, or a `languages = ["c"]` shorthand.
14. **Hand-made normalization in the baseline scripts.** The baseline scripts carry
    hand-made `sed` normalization, for example Meson's build-relative `../../…` source
    paths. A `buildlog`-provided "normalize a foreign build dir" command, shared with
    Halide/zlib, would remove the 5 sed pipelines in run-meson.sh (and their counterparts in the other studies). This is
    infrastructure, not authoring, but every case study repeats it.

## Status

- `tests/test_fribidi_case.py`: 9 passed (build, objects, tables, exec derivation via
  `why`, config.h, fribidi-config.h, symbols, `buildlog test` 9/9, cross build).
- `case-studies/fribidi/baseline/tests/`: 7 passed.
- **Not covered:** install, `fribidi.pc`, the man pages and docs, the fuzzer, Windows
  (`FRIBIDI_BUILD`/`FRIBIDI_LIB_STATIC`, MSVC), a buildlog debug configuration to compare
  with `meson-debug.jsonl` (recorded, not compared), static and `both` linkage.


## UX pass port

The port moved options to `[option.deprecated]`, option/check conditions to infix syntax, Meson
argument variables to profiles (`incs`, `native`), generated files to `[generator.*]`, and native
targets/programs to `[target.*]` tables with `kind`, `sources`, `deps`, maps and `gen_user`. The
six table programs share a `table-tool` profile, and the table generators derive the program's
name from the table's (`gen-{name}-tab`), so a `generate` row names only the table and its inputs.
`configured.toml` keeps the project-owned owner-variable abstraction because core has configured
file templates but not Meson's `configuration_data` rows; that project template is billed.
The configure checks are compact tables (`[cc_check_include]`, `[cc_check_function]`,
`[cc_check_type]`; T2), one line per check: 341 → 317.

| file | before (4a1ccb0) | after |
|---|---:|---:|
| buildlog.toml | 5 | 5 |
| configured.toml | 85 | 85 |
| gen.toml | 39 | 81 |
| programs.toml | 69 | 66 |
| rules.dl | 81 | 47 |
| spec.toml | 69 | 57 |
| total | 348 | 341 |
| Datalog | 81 | 47 |

Graph identity: release/shared, static-linkage and cross-x86_64 configurations compare
`IDENTICAL` with `ninja_norm.py`. Vocabulary gaps: compact Meson-style `configuration_data`
would save about 35 lines in `configured.toml`/`rules.dl`. gen.toml grows (39 → 81) because the
table programs and generators were derived by 25 lines of project Datalog, which are now a
profile, six three-line target tables and two generator templates.

## Linux (GCC 13 and Clang 18)

`linux/run.sh cases gcc|clang fribidi` builds and tests the release configuration (9
Meson tests). `linux/run.sh baselines` runs `baseline/run-meson.sh` (debug and release; no
cross build) with **Meson 1.12.1, the macOS baseline's version** (pip in the image; Ubuntu's
apt Meson is 1.3.2) and writes `baseline/linux/` and `baseline/linux-clang/`. The tests
that are not cross builds pass with both families: the same 42 objects with the same
defines, source include dirs and flags, byte-identical tables, `config.h` and
`fribidi-config.h`, the library's 36 exported symbols and `soname libfribidi.so.0`, the
static linkage and the 9 tests.

- **Meson's large-file define.** Meson adds `-D_FILE_OFFSET_BITS=64` to every compile on
  Linux (not in any `meson.build`), so `spec.toml` states it under
  `platform.os == linux`.
- **`-fPIC`.** Meson spells it on the shared library's objects on Linux; the test compares
  it there (on macOS it is a ledger item).
- **Names.** `libfribidi.so.0.4.0`, soname `libfribidi.so.0`; `elf-metadata.txt` replaces
  `dylib-metadata.txt`.
