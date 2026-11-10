<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: flatbuffers, a code generator the build runs

**Pinned upstream:** google/flatbuffers **v25.12.19**
(`7e163021e59cca4f8e1e35a7c828b5c6b7915953`, 2025-12-19). This is the latest versioned
stable release. GitHub's "latest" release, `v25.12.19-2026-02-06-03fffb2`, is an untitled
snapshot tag with no release notes, so this study does not use it. The Bazel study
(cs-bazel-flatc) uses the same tag.

flatbuffers' `flatc` compiles `.fbs` schemas into C++ headers during the build, and the runtime
library and the tests include those headers. In a cross build, `flatc` must run on the machine
doing the build (the exec platform), while the runtime, the generated code and the tests target
the configuration's platform.

Upstream CMake has no host/target split:
- `add_custom_command(COMMAND ${FLATBUFFERS_FLATC_EXECUTABLE} ... DEPENDS flatc)`
  (CMakeLists.txt:501-511) runs the flatc of the tree being built.
- The escape hatch is a path to a flatc built elsewhere: `FLATBUFFERS_FLATC_EXECUTABLE`
  (CMakeLists.txt:472-473, CMake/BuildFlatBuffers.cmake:57-65).
- google/flatbuffers#5525 ("Cross-compilation cmake and conan") asks for the split.

buildlog infers it. The project's code-generation rule names flatc by target reference
(`action_run`), and core builds the referenced target in the exec configuration. No
annotation says where flatc runs.

## Layout

```
case-studies/flatbuffers/
  README.md              this file (and the ledger)
  compare.py             buildlog build vs the CMake baseline: objects, flags, links, archive, symbols
  baseline/
    run-cmake.sh         the CMake variants below; writes the normalized files ($SRC/$BUILD)
    environment.txt      tool versions and the pin
    release/             native arm64, default options:
                         commands.txt, checks.txt, generated-{files,sha256,header-deps}.txt,
                         exported-symbols.txt, binaries.txt, program-output.txt, ctest.txt,
                         install-manifest.txt, the CMake export files, flatbuffers.pc
    x86_64-naive/        -DCMAKE_OSX_ARCHITECTURES=x86_64
    universal2-naive/    -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"
    x86_64-twotree/      the documented workaround (host tree + FLATBUFFERS_FLATC_EXECUTABLE)
    ios-naive/           CMAKE_SYSTEM_NAME=iOS: a target no emulator runs
  project/
    buildlog.toml        the spec list; [vars] FLATBUFFERS_SRC (the checkout)
    spec.toml            configurations, checks, profiles, ProjectConfig, source lists, flatbuffers, flatc
    tests.toml           flattests, generator templates and rows, the samples
    rules.dl             source lists
    presets/apple-clang.toml   the user file: toolchain pins, none of which mentions flatc
```

The raw CMake trees and logs are in `build/case-flatbuffers/raw/<variant>` (not committed).

## Pinned inputs

- **flatbuffers v25.12.19** is the submodule `case-studies/flatbuffers/upstream` (https://github.com/google/flatbuffers.git, pinned to that commit).
  `BUILDLOG_FLATBUFFERS_SRC` or `-D FLATBUFFERS_SRC=...` overrides the path.
- **Tools:** CMake 4.4.3, Ninja 1.13.2, Apple clang 21.0.0 and macOS 26.5.2 on arm64, with
  Rosetta 2 installed (`baseline/environment.txt`).
- **CMake:** `-G Ninja -DCMAKE_BUILD_TYPE=Release`, other options at upstream's defaults
  (static library, flatc, tests and samples on).

## Running

```sh
case-studies/flatbuffers/baseline/run-cmake.sh [release|x86_64-naive|universal2-naive|x86_64-twotree|ios-naive]
uv run --frozen buildlog configure --user case-studies/flatbuffers/project/presets/apple-clang.toml \
  -C BUILD --config x86_64 case-studies/flatbuffers/project      # or release, universal2
ninja -C BUILD/souffle
uv run --frozen buildlog test -C BUILD
python3 case-studies/flatbuffers/compare.py BUILD [x86_64-macos.release.none]
uv run --frozen pytest -q tests/test_flatbuffers_case.py
```

## Scope

All of scope items 1-4 are covered, and item 5 only partly (see "Install and export"):

- **`flatc`**: 37 objects, recompiling the library sources as upstream does.
- **`flatbuffers`**: `libflatbuffers.a`.
- **Generated code**:
  - all 10 of CMake's `compile_schema_for_test` / `_fbsh` calls for **`flattests`** (27
    objects, 11 headers including `--bfbs-gen-embed`'s extra `test_64bit_bfbs_generated.h`);
  - the sample schema `samples/monster.fbs`.
- **Samples**: `flatsamplebinary`, `flatsampletext` and `flatsamplebfbs`.
- **Test**: the one ctest test (`flattests`).

Out of scope, as they are off by default upstream: the shared library, `flathash`, the
benchmarks, `grpctest` and the other languages' generators.

## Results

### Native (release, arm64)

| | CMake | buildlog |
|---|---|---|
| compile objects | 85 | 85, the same (target, source) set |
| link inputs (5 programs) | – | identical per program (`compare.py`) |
| `libflatbuffers.a` members | 6 | the same 6 |
| defined globals in `libflatbuffers.a` | 666 | 671: the 666 plus 5 weak `std::bad_function_call` symbols (ledger 4) |
| flatc runs | 10 | 11 (+ `monster_generated.h`, ledger 7) |
| generated headers | 11 | 11, **byte-identical** (sha256 against `baseline/release/generated-sha256.txt`) |
| `samples/monster_generated.h` | checked-in copy | generated, byte-identical to the checked-in copy |
| flattests | passed | passed (`buildlog test`) |
| sample output | `baseline/release/program-output.txt` | identical |
| warnings | 0 | 0 |

### The headline: cross and multi-arch

| Variant | What it takes | flatc that runs | Outcome |
|---|---|---|---|
| CMake `x86_64-naive` | 1 flag | **x86_64**, 10 runs under Rosetta 2 | passes, but only because Rosetta translates flatc: this is not a cross build |
| CMake `universal2-naive` | 1 flag | fat (x86_64 + arm64), the arm64 slice runs | passes; flatc is built twice for one use |
| CMake `ios-naive` | 4 flags (2 only to get past configure) | iOS arm64 (platform IOS, minos 15.0) | **fails**: all 10 flatc runs are `Killed: 9`, build exit 137 |
| CMake `x86_64-twotree` | 2 trees, 4 commands, 2 required flags + 3 host-trimming flags | the host tree's arm64 flatc | passes; still builds an unused x86_64 flatc; stale-tool hazard (below) |
| buildlog `x86_64` | `--config x86_64`, **0 annotations** | `bin/arm64-macos.release.none/flatc`, 11 runs | 140 commands; generated headers byte-identical to CMake's; x86_64 flattests passes under Rosetta (run by hand) |
| buildlog `universal2` | `--config universal2`, 0 annotations | `bin/arm64-macos.release.none/flatc` (the arm64 slice's instance), 22 runs | 210 commands; fat flatc, flattests and lib; both slices' flattests pass (`arch`) |

**CMake's manual steps for a real cross build (the two-tree dance):**
1. Configure a host tree (`-DFLATBUFFERS_BUILD_TESTS=OFF -DFLATBUFFERS_BUILD_FLATLIB=OFF
   -DFLATBUFFERS_INSTALL=OFF`, optional, so that only flatc is built).
2. `cmake --build host --target flatc`.
3. Configure the target tree with the target (`-DCMAKE_OSX_ARCHITECTURES=x86_64`, or a
   toolchain file) **and** `-DFLATBUFFERS_FLATC_EXECUTABLE=<host>/flatc`.
4. Build the target tree.

That is 4 commands, 2 build trees and 2 required cache entries, and the flatc path is spelled
by hand. The dance has three further costs:
- The target tree still builds an x86_64 flatc that it never runs.
  `FLATBUFFERS_BUILD_FLATC=OFF` would avoid that, but it also turns the tests off.
- The generation steps keep `DEPENDS flatc`, meaning the target tree's own flatc, not the file
  they run (CMakeLists.txt:511). After an edit to flatc's sources, the target tree rebuilds its
  unused x86_64 flatc and reruns generation with the **stale** host flatc; nothing rebuilds the
  host tree.
- For iOS (or any target Rosetta cannot run), only the two-tree dance works at all.

buildlog needs one command (`configure --config x86_64`) and 0 annotations. The one graph has
both flatc builds and the edges between them: an edit to `src/idl_parser.cpp` rebuilds the
arm64 exec flatc and reruns every generation step. It also rebuilds the x86_64 product flatc,
because `request_all` asks for it (ledger 9). This was checked with `touch` + `ninja -n`:
11 FLATC edges rerun.

### `buildlog why`: flatc's configuration comes from its use

The question: why is the arm64 flatc needed in the x86_64 build? Output trimmed; `‹…›` is the
configuration legend. The command is:
`buildlog why -C bl/x86_64 'needed("flatc", ["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], nil]]])' --depth 3`.

```
needed("flatc", ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:312]
  tool_dep("flattests", ‹x86_64-macos.release.none›, "flatc")  [buildlog:L1_targets.dl:565]
    tool_ref("flattests", ‹x86_64-macos.release.none›, "out:gen/x86_64-macos.release.none/flattests/tests/64bit/test_64bit_generated.h", "flatc", "run")  [buildlog:L1_targets.dl:551]
      action_run("out:gen/…/test_64bit_generated.h", 0, "", "flatc", "run")  [buildlog:V_generators.dl:85]  … 2 more
      target_action("flattests", ‹x86_64-macos.release.none›, "out:gen/…/test_64bit_generated.h")  [buildlog:V_generators.dl:84]  … 2 more
    tool_resolves("flattests", "flatc", "flatc")  [buildlog:L1_targets.dl:558]
      project_name("flatc")  [buildlog:L1_targets.dl:555]  … 2 more
  exec_config(‹x86_64-macos.release.none›, ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:133]
    config(‹x86_64-macos.release.none›)  [buildlog:L1_targets.dl:89]
    host_platform("arm64-macos")  [buildlog-cpp:layers/L2_toolchains.dl:23]
      res_host_platform("inventory:default", "arm64-macos")  ← probe inventory request "inventory:default"
    exec_settings(... native.opt release ...)   !exec_overridden("native.opt")
```

(Regenerated after the UX pass ported flatc to core's generator vocabulary; one level deeper,
`action_run`'s leaves are the `flatc_cpp` template's `run` line and the instance's `generate` row,
both in tests.toml.) The leaves are one generator row (tests.toml), the `flatc` target (spec.toml), the
x86_64 configuration (spec.toml) and the probed build machine. No user or author fact mentions
exec, host or arm64 for flatc. The toolchain follows in the same way:
`why 'tc_for("cxx", ‹arm64…›, "apple-clang")'` derives it from
`tc_select("cxx", "arm64-macos", "arm64-macos", "apple-clang")` and the preset's first pin.
`why --flag clang++ --output bin/arm64-macos.release.none/flatc --with-demand` gives the whole
chain from the link line.

The configure-check path is also explained:
`why --flag -DFLATBUFFERS_LOCALE_INDEPENDENT=0 --output obj/.../flatbuffers/src/util.cpp.o`
leads through the `dir_props` profile's inline condition to the
`FLATBUFFERS_HAS_STRTO*_L` check results.

## Ledger

Each difference from the baseline, with its reason. `compare.py` prints them.

1. **`-std=c++11` vs `-std=c++14`.** Upstream requests `cxx_std_11`
   (`target_compile_features`). CMake emits `-std=c++14`: Apple clang's default level is
   gnu++14, CMake keeps the default when it satisfies the request, and then spells it because
   `CMAKE_CXX_EXTENSIONS` is OFF. buildlog emits what the author asked for. Everything builds
   cleanly as C++11, and flattests passes. CMake's behavior is an implicit, compiler-dependent
   choice.
2. **`FLATBUFFERS_TEST_PATH_PREFIX` on flattests only.** CMake's `add_definitions()` sits
   inside the tests block but applies to the whole directory, so the define also reaches the
   library, flatc and the samples (58 objects). Only the tests read it. buildlog states it on
   flattests (tests.toml), and the value is the absolute source path in both builds.
3. **Include dirs.**
   - CMake adds `$BUILD/samples` (`include_directories`) to every target, but nothing is
     generated there, because `compile_schema_for_samples` is never called.
   - flattests' `$BUILD` and `$BUILD/tests` become `gen/<cfg>/flattests[/tests]`.
   - The samples get the generated header's directory as `-isystem`, since a dependency's
     include dirs are system dirs to its consumers (buildlog-cpp policy).
4. **Target spelling:** `--target=arm64-apple-macos13 -isysroot <SDK>` vs `-arch arm64`.
   - The deployment target is buildlog's inventory's (macOS 13). CMake has none, so the
     compiler's default (26.0) applies.
   - The deployment target is observable in the output: with macOS 13, libc++ emits weak
     `std::bad_function_call` vtable/typeinfo/destructors in `reflection.cpp.o` and
     `util.cpp.o` (5 extra globals in `libflatbuffers.a`). With 26.0, the dylib provides them.
   - This was checked by compiling `util.cpp` both ways: 6 vs 1 matching symbols.
5. **Mechanics.**
   - `ar rcs` vs `cmake -E rm` + `ar qc` + `ranlib`.
   - Flags come in lexical order rather than CMake's.
   - `-MD -MF` without `-MT`.
   - The link lacks `-Wl,-search_paths_first -Wl,-headerpad_max_install_names`, as in the zlib
     study.
6. **Generated-header shadowing (both builds).** The depfiles show that only 3 of the 11
   generated headers are ever read (`alignment_test`, `arrays_test`,
   `default_vectors_strings_test.fbs.h`). The other 8 are shadowed by identical checked-in
   copies next to the including source, because `#include "..."` searches the includer's
   directory first. So both builds generate 8 headers the compiler never reads, and would
   silently use the checked-in copy if the two diverged. buildlog reproduces this faithfully:
   the same sources, the same include semantics.
7. **`monster_generated.h` is generated** (+1 flatc run). CMake's
   `compile_schema_for_samples` is dead code, and the samples use the checked-in header.
   buildlog generates it with `scripts/generate_code.py`'s flags as the samples' interface
   target `flatsample`. The output is byte-identical to the checked-in copy, which still
   shadows it (6).
8. **Source lists and directory scope are project rules** (rules.dl, 78 lines in all):
   - `srclist*` for CMake's `FlatBuffers_*_SRCS` variables, including splices;
   - `directory_prop(_when)` for `include_directories` / `add_definitions`;
   - (`cxx.family`, for `$<CXX_COMPILER_ID>`, was a rule here; it is now buildlog-cpp's attribute);
   - `flatc_schema` for `compile_schema`.
9. **The x86_64 configuration also builds an x86_64 flatc.** flatc is a requested product
   (`request_all`, like CMake's `all` and install), so the target gets one. It is never run.
   Upstream #5525 asks for the build machine's flatc as the installed tool, which is a
   packaging question.
10. **universal2 runs flatc 22 times.** Each slice is a configuration with its own generated
    headers, so all 11 run per slice. The generated C++ does not depend on the target, so 11
    runs are redundant (CMake runs 10).
11. **Tests on a foreign platform are "not run".** `buildlog test` reports x86_64 flattests as
    "built for x86_64-macos; the build machine is arm64-macos". Rosetta runs it (passed by
    hand; `arch -arm64` gives "Bad CPU type"). universal2 tests each slice separately: arm64
    passes, x86_64 is not run.
    *Step 4:* with the user file `presets/rosetta.toml` (`platform_runs_on`), both run and pass.
12. **ProjectConfig's compiler branches.**
    - The clang and GCC branches are stated as `prop_when` rows on `cxx.family` (spec.toml),
      and the GCC branch is exercised on Linux (see "Linux").
    - The GCC branch includes `-Werror=unused-parameter`, an author-imposed `-Werror` on a
      user-chosen compiler, which buildlog's policy (per-role `cc_werror`) refuses. buildlog.toml
      therefore states `[author_decides] cc_werror` with upstream as the reason.
    - The version guards (clang > 3.8, and GCC > 4.4/4.7/7.0/8.0, CMakeLists.txt:394-419) are
      `cxx.version > 3.8` and the like in the same `when` conditions. Apple clang 21 passes the
      clang guard, so its flags are the ones CMake passes.

### Install and export (scope item 5): friction, not done

The experiment was run in a scratch copy, which is not committed.
- **Headers install.** `install_map ["src:include", "includedir", ""]` plus 32 `install_file`
  rows, and a user file with `install_prefix`. `ninja install` installed the 32 headers, as
  `install(DIRECTORY include/flatbuffers ...)` does.
- **Friction:** a directory install has no counterpart, so the 32 files are listed by hand.
- **`libflatbuffers.a` and `flatc` cannot be installed.** Their paths depend on the
  configuration (`out:lib/<cfg>/...`), but `install_file` / `install_map` are author input
  facts with no configuration. Deriving them in rules.dl is rejected ("clause head
  install_file is owned by buildlog; not declared in extends"): they are not in core's `open`
  list.
- **Export is refused as designed.** `export_component` for either target gives "exported
  component with a native artifact (not supported yet)" (L3_install.dl; linkage-as-parameter is not implemented).
- **Question for the installable-flatc case:** in the x86_64 configuration two flatc
  binaries exist. CMake installs the target's. #5525 wants the tool usable on the build OS,
  and a CPS package would need to say which platform `flatc` runs on (a `flatc` component
  with its own platform, which is not the library's).
- CMake installs: headers, `libflatbuffers.a`, `flatc`, 7 CMake package files (including
  `BuildFlatBuffers.cmake`) and `flatbuffers.pc` (`baseline/release/install-manifest.txt`).
- The shared library (off by default) has SOVERSION = VERSION = 25.12.19, and on macOS
  compatibility and current version 25.12.19. buildlog does not model these; see Design
  input 10.

## Design input (ranked)

1. **Exec inference generalizes (confirmed on a second project).**
   - flatbuffers needed nothing beyond Halide's mechanism: an action that names a target in
     `action_run` makes that target a tool, built for `exec_config`.
   - The preset's toolchain pins decide which compiler builds it, and none of them mentions
     flatc.
   - This fixes #5525's problem, and the stale-tool hazard of CMake's workaround, by
     construction.
2. **Configuration-independent actions.** flatc's output depends on the schema and the
   options, not on the target. universal2 runs it twice per schema, and a multi-config build
   would run it once per configuration. A way to say an action is target-independent (keyed
   by the exec config only) would share the outputs. Halide's generated headers are the same
   case.
3. **Emulators / can-run.**
   - Tests (and, in principle, tools) for x86_64 are "not run" on an arm64 Mac that runs them
     through Rosetta.
   - CMake has `CMAKE_CROSSCOMPILING_EMULATOR`.
   - A `runs_on(platform, via)` inventory fact (Rosetta; qemu-user elsewhere) would let
     `buildlog test` run them and keep "not run" for iOS.
   - *Step 4:* core's user relation `platform_runs_on(p, host, via)`; see "Step 4 changes".
4. **Install/export of native artifacts** (above):
   - configuration-dependent `install_file`;
   - directory installs;
   - native components in CPS;
   - the tool component's platform.
5. **Compiler identity and version in conditions (done).** CMake's `$<CXX_COMPILER_ID:GNU>`
   and `VERSION_GREATER` guards are `cxx.family` and `cxx.version > 4.4` (buildlog-cpp's
   attributes; the comparison is core's, `docs/spec-reference.md`).
6. **Directory-scope properties and source lists** are common CMake vocabulary. Together they
   took about 25 lines of rules, but every CMake port will rewrite them; they are candidates for
   an importer-facing library.
7. **Quote-include shadowing is invisible.** A generated header shadowed by a checked-in file
   of the same name (ledger 6) could be diagnosed from depfiles: an action output that no
   consumer reads, while a same-named source file is read.
8. **Implicit standard level.** `cxx_std_11` becoming `-std=c++14` in CMake is worth a note
   in porting guides. buildlog's literal reading is the honest one, but it compiles different
   code than the baseline did.
9. **`--config` is not persisted.** Rerunning `configure` without `--config` re-activates
   every configuration: the build dir's command count went from 102 to 210. Regeneration
   keeps user files and `-D`, but not the configuration choice.

10. **Shared-library versioning (recorded, not modeled).** buildlog does not model
    SOVERSION, install_name or compatibility_version yet. upstream's default is static, and
    the shared library (`FLATBUFFERS_BUILD_SHAREDLIB=ON`, target `flatbuffers_shared`,
    `OUTPUT_NAME flatbuffers`) is out of scope. I built it once with CMake to record the
    values (`build/case-flatbuffers/raw/sharedlib`, not committed).
    - **The values.** VERSION = SOVERSION = `PROJECT_VERSION` = 25.12.19 (CMakeLists.txt:488-495).
      Upstream's comment: "calendar-based versioning and do not provide any ABI stability
      guarantees. Therefore, always use the full version as SOVERSION."
    - **What CMake produces on macOS:**
      - `libflatbuffers.25.12.19.dylib`, and `libflatbuffers.dylib` symlinked to it;
      - install name `@rpath/libflatbuffers.25.12.19.dylib`;
      - compatibility version 25.12.19 and current version 25.12.19. Every release is a new
        ABI, and dyld refuses an older library.
      - On ELF the result would be SONAME `libflatbuffers.so.25.12.19`.
    - **Where the version comes from.** `CMake/Version.cmake` hard-codes 25.12.19 but
      overrides it from `git describe` when that is available, so the ABI identity depends on
      how the source was obtained (a tarball vs a checkout of a non-tag commit).
    - **Upstream contradicts itself.** Its package-version file
      (`CMake/flatbuffers-config-version.cmake.in`) accepts any newer version: `find_package
      (flatbuffers 25.1)` accepts 25.12.19 and any later release. The SONAME says no two
      releases are ABI-compatible, and so do the generated headers, which
      `static_assert(FLATBUFFERS_VERSION_MAJOR == 25 && ..._MINOR == 12 && ..._REVISION == 19)`
      against the runtime headers. `flatbuffers.pc` gives `Version: 25.12.19`, with no
      compatibility statement.
    - **The bearing on export.** buildlog's export has `export_compat` = same_major | same_minor
      | exact | any_newer, and CPS `compat_version`. For flatbuffers:
      - the honest setting is `exact`, i.e. CPS `compat_version` = `version` = 25.12.19,
        regardless of linkage;
      - the static library needs the same, because generated code checks the exact runtime
        version at compile time;
      - with it, a downstream `requires: flatbuffers >= 25.1` resolves only to 25.12.19;
      - upstream's any-newer version file is the case where a package's declared compat is
        looser than its ABI (and its API: generated headers from another flatc do not
        compile).
    - **What this asks of the design:**
      1. Derive a shared library's SOVERSION/compatibility_version from the package's
         `export_compat`, rather than stating it separately. For `exact`: the full version;
         for `same_major`: the major.
      2. Reject, or at least warn about, an `export_compat` looser than the SOVERSION implies.
      3. Record the tool/runtime version coupling: code generated by flatc X requires runtime
         X exactly. A `requires` from generated code to the runtime at `exact` is the
         dependency CMake leaves implicit.

## UX input

These are the authoring and usability frictions I hit. For each: the snippet I wrote, what I
wished I could write instead, and the line count.

**Size.**
- **buildlog spec:** 237 non-comment, non-blank lines outside source-list entries. That is
  about 220 without the `[srclist.*]` scaffolding (headers, splices, brackets).

  | File | Lines |
  |---|---|
  | `buildlog.toml` | 5 |
  | `spec.toml` | 87 |
  | `tests.toml` | 93 |
  | `presets/apple-clang.toml` | 6 |
  | `rules.dl` | 46 |

- **Upstream `CMakeLists.txt`:** 481 lines by the same measure (616 non-comment lines, minus
  135 source-file lines). That count also covers what is out of scope here: options, the
  shared library, install/export, packaging and the other generators' tests. The in-scope
  subset is roughly comparable to buildlog's 237.
- **The Datalog share is the smell:** 46 of the 237 lines are rules.dl, and all of it is
  vocabulary a CMake port needs (items 1-4 below).

**1. Code generation had to be written in Datalog.** This is 30 lines of rules.dl (`flatc_gen`,
`flatc_out`, `action`/`action_run`/`action_arg` ranks 10+r and 90-94, `action_out`,
`prop_in` ×4, 2 errors), plus 5 `.decl`s.

```
action_run(o, 0, "", "flatc", "run") :- flatc_out(_, _, o, _, _, _, _, _).
action_arg(o, 10 + r, "", f) :- flatc_out(_, _, o, _, _, _, _, x), flatc_opts(x, r, f).
action_arg(o, 90, "", "--filename-suffix"), action_arg(o, 91, "", s),
action_arg(o, 92, "", "-o"), action_arg(o, 93, "", od), ...
prop_in(t, c, "cc.generated_header", o, "public") :- flatc_out(t, c, o, _, _, _, _, _).
```

Wished for a TOML action template (about 6 lines), with outputs becoming generated headers of
the target:

```toml
[generator.flatc_cpp]
tool = "flatc"                       # a target name: exec config inferred as today
args = ["{opts...}", "--filename-suffix", "{suffix}", "-o", "{outdir}", "{in}"]
outputs = ["{name}{suffix}.h"]
[target.flattests]
generate = [{ generator = "flatc_cpp", in = "tests/alignment_test.fbs", suffix = "_generated", opts = "FLATC_OPT_COMP" }, ...]
```

Manual argv ranks (10+r, 90..94) and a required `public` visibility on `cc.generated_header`
were the error-prone parts. With `private`, the interface target `flatsample`'s header was
silently not built, and the samples fell back to the checked-in copy.

**2. The compiler identity had to be written as an attribute in Datalog** (since moved into
buildlog-cpp as `cc.family` and `cxx.family`). That was 6 lines, plus
5 comment lines explaining the workarounds:

```
cxx_family_known("clang"). cxx_family_known("gcc").
cxx_family_known(f) :- toolchain(_, f, _).
attr_discovered("cxx.family") :- cxx_family_known(_).
attr_value("cxx.family", f) :- cxx_family_known(f).
config_attr(c, "cxx.family", f) :- tc_for("cxx", c, tc), toolchain(tc, f, _).
```

Wished for a built-in `cxx.compiler_id` (and `cxx.compiler_version` with a `$Ge`) in
buildlog-cpp, used as `["gcc", "$Eq(cxx.compiler_id, gcc)"]`: 0 lines. Three errors in a row,
each fixed blind:
- "attribute undefined" (in the inactive configurations);
- "outside attribute domain" (the inventory only knows clang);
- "no .input declaration for relation attr_discovered", for a ground fact in project rules.

That last one meant writing `cxx_family_known` as a helper. The message names Soufflé's IO,
not the loader's rule ("project rules may not state ground facts of this relation; derive
it").

**3. Source lists in Datalog.** 7 lines, for CMake's `set(X_SRCS ...)` with `${Y_SRCS}` splices:

```
srclist_all(l, s) :- srclist_source(l, s).
srclist_all(l, s) :- srclist_splice(l, o), srclist_all(o, s).
cxx_source(t, s) :- srclist_use(t, l), srclist_all(l, s).
```

Wished for `sources = ["@FlatBuffers_Library_SRCS", "src/flatc.cpp", ...]` or a built-in
`[source_set.X] include = [...]`: 0 rules.

**4. Directory-scope properties in Datalog.** 4 lines (`prop(t, k, v, "private") :-
directory_prop(k, v), native_kind(t, _).`). The same default is repeated as `dep = {
ProjectConfig = "private" }` on all 6 native targets. Wished for a `[defaults]` table:
`[defaults] prop = [...]`, `dep = { ProjectConfig = "private" }`, applying to every target in
the file.

**5. Repetitive property rows.** ProjectConfig has 21 rows, each repeating
`"cc.compile_flag"` and `"interface"`, and 13 of them also repeat a condition name:

```toml
prop_when = [
  ["cc.compile_flag", "-Wnewline-eof", "interface", "clang"],
  ["cc.compile_flag", "-Wno-unknown-warning-option", "interface", "clang"], ... (13 rows)
```

Wished for the same in about 5 lines:

```toml
compile_flags.interface = ["-Wall", "-pedantic", ...]
compile_flags.interface.when.clang = ["-Wnewline-eof", ...]
```

**6. The same condition stated twice, negated.** 8 lines (`locale_independent` and
`locale_dependent` differ by one `$Not(...)`), because `prop_when` takes a condition name, not
an expression. Wished for `["cc.define", "FLATBUFFERS_LOCALE_INDEPENDENT=0",
"!locale_independent"]` or an `else` value: `when = "locale_independent", then = "...=1",
else = "...=0"`.

**7. Option lists duplicated.** 3 of the 4 `flatc_opt_list` tables repeat `FLATC_OPT_COMP`'s 5
flags to add one flag each (20 lines). Upstream writes `"${FLATC_OPT_COMP};--scoped-enums"`.
Wished for `extends = "FLATC_OPT_COMP"` (the splice that `srclist` has): 6 lines.

**8. Symbol-exists checks as C programs.** Three `cc_check` tables with a hand-written program:

```toml
subject = "int main(int argc, char** argv) { (void)argv; return ((int*)(&strtof_l))[argc]; }"
```

That is 4 lines each, 12 in all. Wished for `kind = "symbol_exists", symbol = "strtof_l",
headers = ["stdlib.h"]`: 3 lines each, with the program the plugin's.

**9. `[R.x]` TOML subject form.** `[flatc_opt_list.FLATC_OPT_COMP]` works only because I made
`flatc_opt_list` unary. My first shape (subject inside a binary relation) was rejected. The
rule "the table name must be a unary relation" had to be learned from the error.

**10. Installing needs a hand-written file list** (32 `install_file` rows for
`install(DIRECTORY include/flatbuffers ...)`), and built artifacts cannot be installed at all
(Design input 4). The error for deriving `install_file` in rules.dl, "clause head install_file
is owned by buildlog; not declared in extends", reads as a package-system rule. What an author
needs to hear is "install_file is author input; built artifacts are not installable yet".
Wished for `install_dir = [["src:include/flatbuffers", "includedir", "flatbuffers"]]` and
`install = ["flatbuffers", "flatc"]` for targets.

**11. `why` query syntax.**
- The legend prints `‹arm64-macos.release.none›`, but a query cannot use it:
  `'needed("flatc", ‹arm64-macos.release.none›)'` fails with "expected a value at offset 22".
  The query needs the spelled-out value:
  `'needed("flatc", ["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], nil]]])'`.
  Wished for the config name (`needed("flatc", @arm64-macos.release.none)`) or a `--config`
  option.
- `--flag` needs the compiler's full path to match the argv token
  (`--flag /Applications/Xcode.app/.../clang++`). Wished for a basename match.

**12. `why` output length.**
- Without `--depth`, explaining the exec flatc printed about 90 lines. Most of it was
  configuration-suffix plumbing (`decl_suffix`, `settings_name`, `mconfig(‹universal2...›)`
  via `settings_suffix`, and `1 > 0`, `0 = 0` arithmetic leaves), repeated for each
  configuration.
- The meaningful chain (`action_run` → `tool_ref` → `tool_dep` → `needed(flatc, exec)` ←
  `exec_config`) is 6 lines of it.
- `--depth 3` helped, but it cuts uniformly. Wished for configuration-construction facts
  folded by default (as `needed` already is: "exists here because it is built in this
  configuration"), and for comparisons to be hidden.

**13. CLI.**
- `--config` is not persisted: a plain re-`configure` re-activated all 3 configurations (102
  → 210 commands).
- The Ninja file is at `BUILD/souffle/build.ninja`, so every command is `ninja -C
  BUILD/souffle` (the directory name is an implementation detail).
- There is no `buildlog build` that forwards a job limit.
- The first configure needs `--user presets/apple-clang.toml`. Later ones remember it, which
  is inconsistent with `--config`.

**14. Foreign-platform tests.** `buildlog test` says "not run (built for x86_64-macos; the build
machine is arm64-macos)", and there is no switch to try anyway (for example `--run-foreign`)
when Rosetta can run it. I ran the x86_64 `flattests` by hand.
(Step 4: the user states it once, `platform_runs_on` in presets/rosetta.toml.)

**15. File sprawl.** 5 files: `buildlog.toml` (5 lines, mostly the spec list), a 6-line preset
and spec.toml / tests.toml / rules.dl. The preset is necessary (it is the user's), but a
single-toolchain Mac needs 4 pin rows for "Apple clang for everything":

```toml
toolchain_pin = [["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ... ×4]
```

Wished for `toolchain_default = "apple-clang"`: 1 line.

**16. Variable naming.** `[vars] FLATBUFFERS_SRC` is overridden by the environment variable
`BUILDLOG_FLATBUFFERS_SRC` or by `-D FLATBUFFERS_SRC=...`. Two names for one knob is
workable, but the prefix convention has to be learned from another study.

## Upstream issues

- **Solved by construction:** google/flatbuffers#5525 (cross-compilation needs a host flatc).
  The same applies to every `FLATBUFFERS_FLATC_EXECUTABLE` two-tree recipe, and to its
  stale-tool hazard (CMakeLists.txt:511 `DEPENDS flatc`).
- **Not solved:** installing the build-machine flatc from a cross configuration as a package
  component (install/export, above).

## Tests

`tests/test_flatbuffers_case.py` (integration; it skips without the checkout, Soufflé, Ninja,
or an arm64 Mac) checks the following:
- **release:** 11 flatc runs, all by the arm64 flatc; the generated headers byte-equal to
  CMake's (sha256); the 85 objects equal to CMake's; flattests and flatsamplebinary build and
  pass.
- **x86_64:** 11 runs by `bin/arm64-macos.release.none/flatc`; the exec flatc linked for arm64
  and the products for x86_64 (`--target`, `lipo -archs`); no exec/host/tool_dep fact in the
  project.

## Unverified

- The iOS cross build with buildlog: the inventory has no iOS platform here. The CMake
  failure is recorded; buildlog's success is expected, but not shown.
- The GCC branch of ProjectConfig (no GCC toolchain in the inventory).
- The x86_64 flattests under Rosetta was run by hand, not by `buildlog test`.

## Step 4 changes

| Change | Files | Before | After |
|---|---|---|---|
| §6: `platform_runs_on`, a user fact: this Mac runs x86_64-macos programs (Rosetta, directly) | presets/rosetta.toml (user file, not spec) | – | 1 |
| One library kind: `flatbuffers` was `static_library`; it is `library` with `linkage = { require = "static", why = ... }` (upstream's `add_library(flatbuffers STATIC ...)`); graph identical | spec.toml | 67 | 68 |
| bazel-flatc's `flatbuffers` is `library` with no requirement (Bazel's `cc_library` follows the consumer); graph identical | bazel-flatc spec.toml | 43 | 43 |

- `--user presets/apple-clang.toml --user presets/rosetta.toml --config x86_64 --config universal2`:
  `buildlog test` runs flattests in arm64-macos.release.none and x86_64-macos.release.none
  (universal2's slices, and the x86_64 configuration's). Both pass: "2 passed, 0 skipped,
  0 failed, 0 not run". Without the user file, the x86_64 case is "not run", as before.
- The fact is never inferred, and it only reaches `buildlog test`. A build action that runs an
  x86_64 program is still an error (core's exec check). The graphs are unchanged.
- `test_x86_64_tests_run_where_the_user_says_the_machine_runs_them` covers it, and skips
  without Rosetta.


## UX pass port

The port moved directory-scope properties into the `dir_props` profile, target declarations to
`[target.*]` maps (`kind`, `deps`, `private`/`interface` and `when`), and kept the project-owned
source-list abstraction until core source groups land. The `flatc` generator was already on
`[generator.*]`; its long six-column instance rows were replaced by two shorter templates so
`tombi` keeps the rows compact. buildlog-cpp still has no `cxx.family` configuration attribute,
so `rules.dl` keeps the small project rule. Source groups would replace the srclist block and save
about 28 rules/spec lines.

| file | before (4a1ccb0) | after |
|---|---:|---:|
| buildlog.toml | 5 | 5 |
| rules.dl | 46 | 16 |
| spec.toml | 87 | 76 |
| tests.toml | 96 | 128 |
| total | 234 | 225 |
| Datalog | 46 | 16 |

Graph identity: `release` and `x86_64` compare `IDENTICAL` with `ninja_norm.py`.

T2: the three symbol checks are `cc_check_symbol` rows (`cc_check_lang = "cxx"`), whose
program is the one the tables spelled out; 225 → 216 lines. Graphs `IDENTICAL`.

## Linux (GCC 13 and Clang 18)

`linux/run.sh cases gcc|clang flatbuffers` configures, builds `flatc`, `flatbuffers`,
`flatsamplebinary` and `flattests` for the `release` configuration and runs the test
(1/1). `linux/run.sh baselines` runs `baseline/run-cmake.sh` for `release` only and writes
`baseline/linux/` and `baseline/linux-clang/`; the three tests that are not macOS-only pass
with both families (generated files byte-identical with upstream's; every object and link
command compared with CMake's; `flattests` passes). The x86_64, Rosetta and universal2
tests are macOS-only and skip.

- **GCC branch.** upstream's `ProjectConfig` GCC flags (`-Werror=unused-parameter` and
  friends) are `cxx.family` rows; `-Werror=unused-parameter` is an author `-Werror`, so
  `buildlog.toml` has `[author_decides] cc_werror` (item 12 above).
- **Presets.** `presets/gcc-arm64.toml` and `clang-arm64.toml`, ported from the Apple one.
- **cxx.family** moved from this project's `rules.dl` into buildlog-cpp
  (`cc.family`, `cxx.family`), because ggml and SPIRV-Tools need it too.
