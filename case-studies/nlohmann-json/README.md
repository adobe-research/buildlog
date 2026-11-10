<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: installing and exporting nlohmann/json with buildlog

nlohmann/json is header-only. Building it compiles nothing, so the study is
all about install and export. The questions are what the package says to
its consumers, where its files go, and whether a consumer sees the same
library whichever way it gets it. The library's options (`JSON_Diagnostics`
and the rest) are compile definitions on its interface. They change the
behaviour and the ABI of every consumer (upstream
[#3106](https://github.com/nlohmann/json/issues/3106)), so they must cross
the install boundary intact. The design is in `docs/architecture.md`.

## Layout

```
case-studies/nlohmann-json/
  README.md               this file (and the ledger)
  baseline/
    run-cmake.sh          CMake configure/install, and two consumers of the install; writes the files below
    environment.txt       tool versions and the pin
    default/, diagnostics/   one directory per variant (defaults; -DJSON_Diagnostics=ON):
      cache.txt           the JSON_* cache entries
      installed-tree.txt  every installed file, with its size and sha256
      nlohmann_json.pc, nlohmann_jsonConfig{,Version}.cmake, nlohmann_jsonTargets.cmake
                          the installed package files ($PREFIX for the prefix)
      consumer-cmake-{command,output}.txt        a find_package consumer's compile line and output
      consumer-pkg-config-{cflags,output}.txt    the same with pkg-config
  downstream/
    main.cpp              the consumer: prints the version and JSON_DIAGNOSTICS, and a diagnostics-sensitive error
    cmake/CMakeLists.txt  find_package(nlohmann_json 3.2 CONFIG REQUIRED)
    buildlog/             the same consumer as a buildlog project; its presets pick the route
  project/                the author's project: nlohmann/json as its CMakeLists.txt states it
    buildlog.toml         the inline spec, [vars] JSON_SRC, and shipped [presets.*]
    headers.toml          the installed headers (generated: one row per file)
    presets/apple-clang-arm64.toml   the user file for this Mac
    presets/install-prefix.toml      install_prefix = "${INSTALL_PREFIX}" (give -D INSTALL_PREFIX=...)
    presets/diagnostics.toml         option_value JSON_Diagnostics on
```

## Pinned inputs

- **nlohmann/json v3.12.0**, commit `55f93686c01528224f448c19128836e7df245f72`,
  the submodule `case-studies/nlohmann-json/upstream` (https://github.com/nlohmann/json.git) (`JSON_SRC` overrides it).
- **Tools:** CMake 4.4.3, Ninja 1.13.2, pkg-config 3.0.7, Apple clang 21.0.0,
  macOS 26.5.2 on arm64 (`baseline/environment.txt`).
- **CMake configuration:** `-G Ninja -DJSON_BuildTests=OFF
  -DCMAKE_INSTALL_PREFIX=<staging dir>`, and `-DJSON_Diagnostics=ON` for the
  second variant. The tests are out of scope.

## Running

```sh
case-studies/nlohmann-json/baseline/run-cmake.sh      # the CMake baseline
P=case-studies/nlohmann-json/project
uv run --frozen buildlog configure --user $P/presets/apple-clang-arm64.toml \
  --user $P/presets/install-prefix.toml -D INSTALL_PREFIX=$HOME/opt/nlohmann -C build/nl $P
ninja -C build/nl/souffle              # the export files (nothing to compile)
ninja -C build/nl/souffle install      # DESTDIR=... stages it
D=case-studies/nlohmann-json/downstream/buildlog
uv run --frozen buildlog configure --user $D/presets/apple-clang-arm64.toml \
  --user $D/presets/import-cps.toml -D NLOHMANN_PREFIX=$HOME/opt/nlohmann -C build/down $D
uv run --frozen pytest -q tests/test_nlohmann_install.py
```

Without `install_prefix` the build still writes the export files, but
`ninja install` fails and says how to set a prefix. There is no default.

## How nlohmann/json is stated

- **Options.** The seven `option()`s that change the interface are
  `[option.<name>]` entries with upstream's defaults. The user sets them with
  `option_value` (e.g. `presets/diagnostics.toml`) or `-O`. A boolean option
  is its own condition. `target.when` then adds the interface define. This is
  the same mechanism as
  any other conditional property, so the define is an ordinary `cc.define`
  usage requirement of `nlohmann_json`. When the project is built in-tree
  as a `source_project`, its options are namespaced by its name
  (`nlohmann_json.JSON_Diagnostics`) and only the user may set them
  (`downstream/buildlog/presets/nlohmann-diagnostics.toml`); a parent
  author's `option_value` for them is an error.
- **The library.** One `[target.nlohmann_json]` table with interface
  `cxx.std = 11`, the conditional defines, and `cc.include_dir src:include` (or
  `src:single_include` when `JSON_MultipleHeaders` is off). It has no
  `native_kind`, so it is an interface library, and a consumer's
  dependency on it only carries usage requirements.
- **Install.** `install_map` says that each include dir maps to
  `<includedir>`, the install-side counterpart of
  `$<BUILD_INTERFACE:...>`/`$<INSTALL_INTERFACE:include>`. `install_file_when`
  lists the headers under the header-layout condition (`headers.toml`,
  generated from the checkout; upstream installs the directory).
- **Export.** `[export.nlohmann_json]`, version 3.12.0 with
  `same_major` compatibility, and one component (the target). What the
  package says is derived: it is the target's own interface properties with
  their paths mapped through `install_map`. Which files carry it (CPS, .pc,
  CMake) and where they go are the buildlog-cpp plugin's conventions, and
  the user can override the locations. The package's names
  (`nlohmann_json` and `nlohmann_json:nlohmann_json`) are derived from the
  component, the same on the in-tree route and on every import route, so
  the downstream spec never names a route-specific target
  (`nlohmann_json::nlohmann_json`).

## Results

`tests/test_nlohmann_install.py` checks everything below (33 tests, some on a two-component fixture package, `tests/fixtures/export-components`, for `requires` and component names). `tests/test_install_configs.py` (6) covers several configurations installing into one prefix, and `tests/test_install_layout.py` (11) the per-platform layout.

**The installed tree.** buildlog installs 51 files; CMake installs 50. The
46 headers are byte-identical, and the same four package files are there:
`share/cmake/nlohmann_json/nlohmann_json{Config,ConfigVersion,Targets}.cmake`
and `share/pkgconfig/nlohmann_json.pc`. The extra file is
`share/cps/nlohmann_json.cps`, buildlog's primary export. Because the
package has no native artifact, both put the package files under `share/`
(#1697).

**Imported ≡ built.** A single downstream spec (`downstream/buildlog`, the
program `app` with a private dependency on the name `nlohmann_json`) is
configured once per route. The test compares the program's derived usage
facts: the `use` rows (include dirs, defines) and `use_join` rows (the
`cxx.std` lattice value) whose target is `app`. It replaces the package
root (the source tree or the install prefix) with `$ROOT`. The routes:

| route | how nlohmann_json is bound | facts vs in-tree |
|---|---|---|
| in-tree | `source_project(nlohmann_json, ../../../project)` | the reference |
| CPS | buildlog's install, `share/cps/nlohmann_json.cps` | equal |
| CMake export | buildlog's install, `nlohmann_jsonTargets.cmake` | equal |
| CMake export | **CMake's** install, `nlohmann_jsonTargets.cmake` | equal |
| pkg-config | buildlog's install | equal except `cxx.std` (L3) |
| pkg-config | CMake's install | equal except `cxx.std` (L3) and the defines (L4) |

Both variants are checked. The reference facts are
`{use cc.include_dir $ROOT/include, use_join cxx.std 11}`, plus
`use cc.define JSON_DIAGNOSTICS=1` with `JSON_Diagnostics` on. The in-tree
route and the imported routes compare mechanically as equal sets. Each
route's program also builds and runs, printing `JSON_DIAGNOSTICS=1` exactly
when the option is on. A prefix copied elsewhere imports the same facts,
rooted at the copy. The downstream spec is the same file on every route: the
user preset alone chooses the route (`in-tree.toml`, `import-cps.toml`,
`import-pc.toml`, `import-cmake.toml`), with no per-route alias.

**Several configurations, one prefix.** A release and a debug configuration
of nlohmann install into one prefix and give the same tree as one
configuration: every file is identical, so each destination is written
once. Configurations whose export files differ at one destination are a
configure-time error naming a differing fact, and two different built files
at one destination are an install-time error (content hashes) before
anything is copied.

**Formats and their losses.** `export_lossy` records, as a `why`-able fact,
that the `.pc` cannot state `cxx.std >= 11`, and configure warns about it.
The author can omit a format (`export_format_omit`, with a reason) and the
user can skip one (`export_format_skip`); either silences the warning by
dropping the format.

**Foreign consumers of buildlog's install.**
- `find_package(nlohmann_json 3.2 CONFIG REQUIRED)` finds it.
  `downstream/cmake` builds and runs, with `-DJSON_DIAGNOSTICS=1` when the
  package has it.
- SameMajorVersion holds: requests for 3, 3.11 and 3.12.0 are found, and
  requests for 3.13, 4 and 2 are refused.
- `pkg-config --cflags nlohmann_json` gives `-I<prefix>/include`, plus
  `-DJSON_DIAGNOSTICS=1` in the diagnostics variant, and a program compiled
  with it runs.

**Provenance.** `buildlog why --leaves` explains both an installed file and
an exported fact from their origin facts.
- The destination `<prefix>/include/nlohmann/json.hpp` of the `install`
  edge traces to:
  - `install_dir_default("includedir", "include")` in the buildlog-cpp plugin;
  - `install_prefix(...)` in the user's preset;
  - `install_map(... "src:include", "includedir" ...)` and `install_file_when(...)` in the author's spec.
- The CPS token `compile_features=c++11` traces to the author's
  `target_interface("nlohmann_json", "cxx.std", "11")` and the plugin's
  spelling.
- `definitions=JSON_DIAGNOSTICS=1` traces to the author's `option` and
  `target_when`, and to the user's `option_value`.

## UX pass port

The UX pass moves the project spec inline into `buildlog.toml`, keeps
`headers.toml` as the include-able source-list file (its files grouped by
condition under `[target.nlohmann_json.install_when]`), and uses `[option.*]`,
`[export.nlohmann_json]`, `[target.nlohmann_json]` property maps and inline
`[presets.*]`. The external preset files remain usable because the tests use
`--user` paths.

Counted by `historical source-line accounting` (source-list lines and inline presets not
counted):

| files | before (4a1ccb0) | after | Datalog before → after |
|---|---:|---:|---:|
| project | 109 | 37 | 0 → 0 |
| downstream/buildlog | 10 | 9 | 0 → 0 |

Before, headers.toml's 48 rows each carried their condition, so they counted;
now they are plain file lists. The project is 37 lines, below the PI's
about-60-line baseline. Graph identity is `IDENTICAL` for the project default
and diagnostics configurations, and for downstream's in-tree default and
diagnostics routes (after normalizing the downstream source-root path).
Loading a source project written in the vocabulary needed core support
(`facts.py` namespaces `option`/`target_*` rows and infix conditions).
Remaining gap: install lists headers one by one, because only named files are
Ninja inputs (L1 below).

## Ledger

Every difference from CMake's install, with its cause.

**L1. The file list instead of a directory.** Upstream uses
`install(DIRECTORY include/ ...)`. buildlog installs a file list
(`headers.toml`, 48 rows under two conditions). Only named files are Ninja
inputs, so a directory would hide the dependencies. The installed files are
the same 46. *Open:* a glob-like `install_tree` with a stamp (Ninja cannot
depend on a directory's contents).

**L2. CPS is added.** CMake 3.x writes no CPS (CMake 4.x can, experimentally,
through `install(PACKAGE_INFO)`). It is buildlog's primary export.

**L3. pkg-config has no C++ standard.** A `.pc` file cannot state a
language standard as a requirement. Upstream's `.pc` has no `-std` flag
either. Writing `-std=c++11` into `Cflags` would *lower* the standard of a
consumer that uses C++17. So buildlog writes nothing
(`export_unspelled("pc", "cxx.std", ...)`), and the pkg-config route loses
the `cxx.std >= 11` fact. The loss is stated, not silent: the plugin
declares the key unspelled with a reason, which yields the fact
`export_lossy(nlohmann_json, pc, nlohmann_json, cxx.std, ...)` and a
configure warning. nlohmann's spec keeps the `.pc` (upstream ships one); a
user who wants no lossy format writes `export_format_skip`.

**L4. Upstream's .pc drops the defines (upstream bug).** CMake's
`nlohmann_json.pc` is generated from a template with `Cflags:
-I${includedir}`. The `JSON_Diagnostics=ON` install's `.pc` is identical to
the default one, so a pkg-config consumer of it compiles with
`JSON_DIAGNOSTICS=0`. `baseline/diagnostics/consumer-pkg-config-output.txt`
shows this. That is an ODR/ABI mismatch with any library built against the
CMake target, which is the #3106 class of bug, on the pkg-config route.
buildlog's `.pc` carries the defines, because every format is derived from
the same `export_fact`s.

**L5. The .pc is relocatable.** Upstream writes `prefix=<absolute prefix>`;
buildlog writes `prefix=${pcfiledir}/../..`. pkg-config then prints a
non-normalized `-I<prefix>/share/pkgconfig/../../include`. buildlog's pc
importer does not normalize it, so the test compares real paths. No file
buildlog exports contains the prefix; a test checks this.

**L6. The CMake config file.**
- buildlog's `nlohmann_jsonConfig.cmake` includes the Targets file after
  `find_package_handle_standard_args`, like upstream's. It does *not* add
  upstream's legacy non-namespaced `nlohmann_json` target for requests
  below 3.2.0. That is a compatibility shim for pre-3.2 spellings, which
  the author would state as an alias if wanted.
- The Targets file lists one `INTERFACE_INCLUDE_DIRECTORIES` entry where
  upstream lists `${_IMPORT_PREFIX}/include` twice. Upstream's two install
  interfaces, of the multiple-header and single-header dirs, both map to
  `include`; buildlog's facts are a set.
- The defines are plain values for the chosen options, not upstream's
  `$<$<BOOL:ON>:...>` generator expressions. The options are resolved when
  the package is configured, and CMake folds the same expressions to the
  same values.
- `cmake_policy(VERSION ...)` and the preamble differ.

**L7. The version file.** buildlog's `nlohmann_jsonConfigVersion.cmake` is
semantically the same as upstream's hand-written one: SameMajorVersion
without the pointer-size check. buildlog *derives* the omission: a package
is architecture-independent when no exported component has a native
artifact (`export_arch_dependent`), and the version file is written only
then. For an architecture-dependent package, buildlog refuses to write a
version file for now. The check it needs (sizeof(void*), and ideally the
target) is an open question.

**L8. Options that are not options.**
- `JSON_Install` is not modelled. Whether to install is the user's choice:
  they run the `install` target or not. The rest of the graph does not
  depend on that choice, so the #2946 failure cannot happen: there,
  `-DJSON_Install=OFF` broke the tests. A project built in-tree as a
  `source_project` drops its install and export facts entirely. #1755 and
  #2482 ("could not find nlohmann_jsonConfig.cmake") are consumers
  searching where the files are not. The directories here follow the
  plugin's conventions, and the user overrides them with `install_dir`
  (e.g. `pkgconfigdir`) without editing the spec; a test checks this.
  `libdir` (unused by a header-only package) follows the target platform:
  `lib` on macOS, GNUInstallDirs' `lib64` or Debian's `lib/<multiarch>` on
  Linux, from platform facts.
- `JSON_SystemInclude` is not modelled either: buildlog always gives a
  dependency's include dirs as `-isystem` (buildlog-cpp's include-directory order).
- `JSON_CI`, `JSON_BuildTests` and the test-only options are out of scope.

**L9. -std on the consumer.** buildlog's consumer compiles with
`-std=c++11`, which comes from `use_join cxx.std 11`. CMake's consumer
compiles with no `-std` flag, because Apple clang's default (gnu++17)
already satisfies `cxx_std_11`. This is buildlog's existing policy for the
`cxx.std` lattice, not something install introduced. All buildlog routes
agree on it (except pkg-config, L3).

**L10. What is not exported (not supported yet).** buildlog refuses the
following with an error, rather than exporting them wrongly:
- exported components with native artifacts (static or shared libraries);
- configurations whose export files differ at one destination (CMake
  writes per-configuration `Targets-<cfg>.cmake` files instead).
nlohmann needs none of these. zlib, next, needs the first one.
Requirements on imported packages are supported
(`tests/test_export_requires.py`); nlohmann has none.

**L11. Names each format can answer.** Both routes define `nlohmann_json`
and `nlohmann_json:nlohmann_json`. pkg-config has no components, so a `.pc`
answers only the bare package name; a CMake export file answers `P` and
`P:comp` (from `P::P` and `P::comp`) but not `P:*`. When a prefix holds
both CPS and CMake metadata, a file that *defines* a name wins over one
that answers it by alias (CPS defines `P:comp`; CMake only aliases it).
Two files that only alias the name still need a `package_pin`.

## Linux (GCC 13 and Clang 18)

`linux/run.sh cases gcc|clang nlohmann` builds the header-only library, installs it to a
staging prefix and builds and tests the downstream against the install (the CPS import).
`linux/run.sh baselines` runs `baseline/run-cmake.sh` (the default and the diagnostics
builds) and writes `baseline/linux/` and `baseline/linux-clang/`. `tests/test_nlohmann_install.py`
compares the install tree and the exported files with CMake's install by host
(`host.baseline_dir`): 33 tests pass with both families. No spec line changes.
