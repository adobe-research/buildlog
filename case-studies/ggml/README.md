<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: ggml's CPU backend as per-ISA modules

ggml builds its CPU backend in one of two ways:

- **Linked (the default).** One `libggml-cpu`, compiled for the build machine
  (`GGML_NATIVE`), and linked into `libggml`.
- **As dlopen'd modules.** With `-DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON`,
  one MODULE library per ISA feature level of the target architecture
  (`libggml-cpu-haswell.so`, `libggml-cpu-apple_m4.so`, ...).
  - `ggml_add_cpu_backend_variant_impl` (src/ggml-cpu/CMakeLists.txt:20) makes each module a copy of the same target.
  - Each gets the same sources and its level's `-march`/`-m` flags, applied per target, not per file (the comment at line 5).
  - At run time `libggml` scans a directory for `libggml-cpu-*.so`, dlopens each one, calls its `ggml_backend_score()`, and keeps the best.

This study models the backend as **one logical backend instantiated per ISA
level**, and the ISA/platform compatibility as facts with clear errors. It
spells out the loader→module relation: needed at run time, never linked.
The comparison is with ggml's own CMake build on this Mac, native arm64 plus
x86_64 through the x86_64 target. The zlib-ng study (per-TU ISA flags) ran in
parallel; the relation names are chosen so the two can be unified (see
Design input).

**Repository used.** ggml-org/ggml, standalone. The variant machinery lives and is maintained in `ggml/src/ggml-cpu/CMakeLists.txt` there, and llama.cpp's `ggml/` is a synced copy.

## Layout

```
case-studies/ggml/
  README.md               this file (the ledger and design input)
  compare.py              compares a buildlog build with a baseline dir: compiles, links, symbols, load commands
  baseline/
    run-cmake.sh          the CMake baselines (five configurations, below); writes the normalized files
    environment.txt       tool versions and the pin
    <config>/             cpu, default, variants-arm64, variants-x86_64, variants-native:
      cmake-options.txt, configure.log, cache.txt   the configure
      commands.txt, compile-commands.jsonl          ninja -t commands; compile_commands.json ($SRC/$BUILD)
      artifacts.txt, exported-symbols.txt           the file set; nm -gU of each library/module
      smoke.txt, ctest.txt                          what test-backend-ops/simple-backend print; ctest
      ggml-version.h                                the configured header
  comparison/             compare.py's output for each pair below, and the install demo
  project/
    buildlog.toml         spec list; [vars] GGML_SRC (the checkout), GGML_BUILD_COMMIT
    spec.toml             configurations (release = arm64-macos, x86_64 = x86_64-macos), options, ggml-base, ggml
    cpu.toml              the CPU backend (sources, props, feats object), the ISA levels, the GGML_NATIVE checks
    tests.toml            test-backend-ops, simple-backend and two tests
    install.toml          install layout (libs to libdir, modules to bindir), derived from the active configuration (`install_target`)
    rules.dl              ISA levels as buildlog-cpp ISA sets, backend instances, variant set, loader, score prediction
    presets/apple-clang.toml  user: toolchain pins (arm64 host → arm64, x86_64), config_default release
    presets/variants.toml     user: GGML_BACKEND_DL=on, GGML_CPU_ALL_VARIANTS=on, GGML_NATIVE=off
    errors/*.toml         user files that each trigger one error (the error demos)
```

## Pinned inputs

- **ggml v0.25.3** (latest release tag), commit `353b63b4`, the submodule
  `case-studies/ggml/upstream` (https://github.com/ggml-org/ggml.git). Override the path with `-D GGML_SRC=...`, or with
  `BUILDLOG_GGML_SRC` for the tests.
- **Tools:** CMake 4.4.3, Ninja 1.13.2, Apple clang 21.0.0, macOS 26.5.2, Apple
  M3 Max (`baseline/environment.txt`).
- **CMake configurations** (all use `-G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CCACHE=OFF`):

  | dir | options | built / run |
  |---|---|---|
  | `default` | upstream defaults (Metal and BLAS on) | all; ctest 20/21 (test-backend-ops fails on **Metal**: TOPK_MOE; out of scope) |
  | `cpu` | `-DGGML_METAL=OFF -DGGML_BLAS=OFF` | all; ctest 21/21. **The linked baseline.** |
  | `variants-arm64` | + `-DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF` | 3 modules; loads `apple_m2_m3` |
  | `variants-x86_64` | same + `-DCMAKE_OSX_ARCHITECTURES=x86_64` | 14 modules; under Rosetta loads `sse42` |
  | `variants-native` | DL + ALL_VARIANTS with `GGML_NATIVE` left ON | 3 identical native modules; **loads none** (below) |

## Running

```sh
case-studies/ggml/baseline/run-cmake.sh          # the CMake baselines (raw trees in build/case-ggml/baseline-raw)
P=case-studies/ggml/project
# linked, native
uv run --frozen buildlog configure -C B/default --user $P/presets/apple-clang.toml -D GGML_BUILD_COMMIT=353b63b4 $P
ninja -C B/default/souffle && uv run --frozen buildlog test -C B/default
# the variants, both architectures
uv run --frozen buildlog configure -C B/variants --config release --config x86_64 \
  --user $P/presets/apple-clang.toml --user $P/presets/variants.toml -D GGML_BUILD_COMMIT=353b63b4 $P
ninja -C B/variants/souffle && uv run --frozen buildlog test -C B/variants
case-studies/ggml/compare.py case-studies/ggml/baseline/variants-arm64 B/variants arm64-macos.release.none
uv run --frozen pytest -q tests/test_ggml_case.py
```

## How it is modelled

- **ISA levels** (cpu.toml tables, rules.dl "ISA levels"):
  - `isa_level(level)`, `isa_level_arch(level, arch)`, `isa_level_parent(level, parent)`, `isa_level_feature(level, feat)` and `isa_level_excludes(level, feat)`: a level states only the features it adds to its parent's, and takes its parent's architecture (a project-local rule that stands in for generic CPU microarchitecture knowledge, which belongs in a layer below buildlog-cpp);
  - `ggml_feature_ext(feat, ext)`: ggml's feature names as buildlog-cpp's ISA extensions (AVX512 is five).
  - Since step 4 (design input §2) each level is a buildlog-cpp ISA set: it requires its architecture and its features' extensions (`isa_requires`) and excludes its excluded ones (`isa_excludes`); the variant compiles with it (`isa_target`). buildlog-cpp derives the flags from its knowledge:
    - arm64: one `-march=<base>+tag+…`, the base the latest version the extensions need, the tags in the order upstream uses (`+nosve` included);
    - x86: one `-m<ext>` per extension.
  - `ggml_cpu_variant(os, arch, level)`: which levels upstream builds for a platform.
  - `ggml_isa_define` / `ggml_isa_score`: the `GGML_<FEAT>` defines and score weights of the feats object.
  - Well-formedness errors: a feature without extensions, a level belonging to two architectures, or undeclared (the project's); an extension the level's architecture lacks (it cannot coexist with the architecture) or one both needed and excluded (buildlog-cpp's).
- **One backend, instantiated per level** (cpu.toml's `[target_template.*]`, rules.dl "The backend's instances").
  - Until step 4, `[cpu_backend.ggml-cpu]` stated the sources, props, deps and feats sources once, and `cpu_backend_instance(b, t, level)` re-attached each to its instances with a one-line rule per relation.
  - Since step 4 these are core target templates (§1): `ggml-cpu-backend` (sources under `arch/{arch_dir}/`, props, dep ggml-base), `ggml-cpu-variant` (module_library, `{isa_define}` defines, dep `{name}-feats`) and `ggml-cpu-feats` (the feature-detection object). One rule makes each level an instance (`instance_of`, `instance_param level`):
    - `ggml-cpu-<level>` for each variant: a `module_library` with that level's flags;
    - `ggml-cpu` itself, declared in cpu.toml as an instance of the backend with `arch_dir = "arm"`: a `library` (shared by default, `default_linkage`, as upstream's `BUILD_SHARED_LIBS` default) with `-mcpu=native+…`.
  - Each variant also gets `<t>-feats`, an object library compiled with the level's defines but **not** its flags, as upstream does, so that probing never executes an instruction the machine lacks.
  - The ISA flags come from one rule, `isa_target(t, l) :- instance_of(t, "ggml-cpu-variant"), instance_param(t, "level", l)`: a template parameter may not depend on the needed closure, and the ARM flag's ordered join does.
- **Which instances are built.**
  - Linked: `ggml` has `dep_when ggml-cpu public linked_backends` (spec.toml:94).
  - Variants: `ggml` has a `runtime_dep_optional_when` on each module, placed `beside_executables`, under the condition `ggml.variants.<os>.<arch>`, i.e. ALL_VARIANTS on and the configuration's os/cpu (rules.dl, "Which instances a build makes"). Until step 4 this was a phantom object library, `ggml-cpu-variants`, always requested, with `dep_when` edges to the modules.
  - So `--config release` builds the 3 Apple levels and `--config x86_64` the 14 x86 levels, from one spec.
- **The loader relation** is core's `runtime_dep` (step 4), here its optional form `runtime_dep_optional`: ggml loads whichever variants it finds, at least one of them, so none is required of it, and upstream installs them to `bindir` (rules.dl's `install_target`), as the loader's own install does not. Its consequences, which rules.dl stated by hand until then (`ggml_loads` and 14 rule lines):
  1. Building `libggml` builds the modules: an order-only input, CMake's `add_dependencies`.
  2. The modules sit where the loader looks: a symlink `bin/<cfg>/libggml-cpu-X.so → ../../lib/<cfg>/…` for every executable that links ggml, an order-only input of those executables. Nothing is linked and no usage requirement propagates.
  3. The **predicted choice**: `ggml_score` mirrors `ggml_backend_score()`, from the same `-mcpu=native` checks GGML_NATIVE uses. It becomes a `test_regex` pass condition, so `buildlog test` fails if the loader picks another module.
- **GGML_NATIVE** (rules.dl:170–205).
  - Four `cc_check`s compile `int main(){return __ARM_FEATURE_X - 1;}` with `-mcpu=native`, replacing upstream's `check_cxx_source_runs`. A user can state the answers with `cc_check_known`.
  - The flag is joined in upstream's order, e.g. `-mcpu=native+dotprod+i8mm+nosve+nosme`, plus `-U__ARM_FEATURE_SVE` and `-U__ARM_FEATURE_SME`.

## Ledger

### Part 1: linked, native (`comparison/cpu.txt`, baseline `cpu`)

- **Equal:**
  - the file set: `libggml-base`, `libggml-cpu` and `libggml`, each 0.25.3 with its links, plus the two programs;
  - every `-D`, `-I` and `-m`/`-U` flag, including the `-mcpu=native+dotprod+i8mm+nosve+nosme` computed from the checks;
  - the global symbols: base 695, cpu 422, ggml 19;
  - the load commands and the link order (ggml → cpu → base);
  - `ggml-version.h`, byte for byte;
  - `buildlog test`: 2/2 pass (simple-backend's mul_mat output equals CMake's `smoke.txt`).
- **Differences, each with its cause:**
  1. `-D<target>_EXPORTS` is CMake-only: CMake's DEFINE_SYMBOL default, unused by ggml.
  2. `-F<SDK>/System/Library/Frameworks` is CMake-only: CMake's `find_library(Accelerate)` adds the framework dir; the SDK default already contains it.
  3. `-std=gnu++17` becomes `-std=c++17`: `cxx.std 17` has no extensions spelling (CMake's `CXX_EXTENSIONS` default ON).
  4. `-Werror=implicit-int -Werror=implicit-function-declaration` are left out on C compiles.
     - buildlog rejects author `-Werror` (L4_cxx.dl:303–325).
     - Apple clang ≥ 16 already makes both errors in C99+.
     - The other C-only warnings and `-std=gnu11` are present, via a rules.dl workaround (design input 4).
  5. Link:
     - CMake adds `-Wl,-headerpad_max_install_names` (for its install-time rpath rewrite) and `-Wl,-search_paths_first`; buildlog adds neither.
     - The rpath differs: CMake uses an absolute `$BUILD/src`, buildlog `@loader_path/../../lib/<cfg>`.
     - `-framework Accelerate` is spelled `-Wl,-framework,Accelerate` (design input 3).
  6. The dependents' include class differs: buildlog uses `-isystem <SRC>/include`, CMake `-I`.

### Part 2: variants (`comparison/variants-arm64.txt`, `comparison/variants-x86_64.txt`)

- **Equal:**
  - the module set: 3 on arm64 (`apple_m1`, `apple_m2_m3`, `apple_m4`) and 14 on x86_64 (`x64` … `sapphirerapids`);
  - every level's `-march`/`-m` flags and `GGML_<FEAT>` defines, on the module objects and on the `-feats` objects, which are flag-free, as in CMake;
  - `GGML_BACKEND_DL` on the modules;
  - no `GGML_USE_CPU` anywhere;
  - global symbols: base 695/701, every module 424 (x86: 442, sapphirerapids 449), ggml 19;
  - the load commands: a module links base and Accelerate, never ggml, and ggml does not link any module.
- **Differences:** the same six as part 1, plus:
  7. **Module location.**
     - CMake writes modules into `bin/`, next to the executables (CMAKE_RUNTIME_OUTPUT_DIRECTORY).
     - buildlog writes them to `lib/<cfg>/` (L3_native.dl:97–102, not configurable) and symlinks them into `bin/<cfg>/`.
     - The loader then reports `…/bin/<cfg>/libggml-cpu-X.so` in both builds.
  8. **simple-backend is built only by buildlog.** Under GGML_BACKEND_DL, `examples/CMakeLists.txt` skips `examples/simple`, although `simple-backend` calls `ggml_backend_load_all()` and works. It is the second smoke program.
  9. Modules are unversioned in both builds, since CMake cannot version a MODULE on macOS (CMake issue 20782). Libraries get `-compatibility_version 0.0.0` explicitly from buildlog; CMake leaves it implicit.
- **Runtime selection on this machine** (the smoke test).
  - `buildlog test` passes simple-backend and test-backend-ops-add on arm64. The pass condition is the predicted module: `loaded CPU backend from .*/libggml-cpu-apple_m2_m3[.]so`.
  - M3 has dotprod and i8mm but not SME, so apple_m4 scores 0. CMake's build loads the same module.
  - The x86_64 cases are "not run" (a cross configuration). Run by hand under Rosetta (`arch -x86_64`), buildlog's x86 build loads `libggml-cpu-sse42.so`, as CMake's does.
  - The x86 prediction is not made: the build machine's checks say nothing about x86_64.

### Part 3: ISA/platform compatibility errors (`project/errors/`, tests)

| user file | message |
|---|---|
| `errors/haswell-on-arm64.toml` (`request ggml-cpu-haswell` in release) | buildlog-cpp's `ISA scope incompatible with the configuration's cpu: ggml-cpu-haswell (haswell) needs avx2, which arm64 (arm64-macos.release.none) does not have`, one per extension (until step 4 §2, the project's `ggml CPU variant is for another architecture`, also for the feats object) |
| `errors/variants-without-dl.toml` | `GGML_CPU_ALL_VARIANTS requires GGML_BACKEND_DL: set GGML_BACKEND_DL=on` (upstream: FATAL_ERROR) |
| `errors/native-variants.toml` (NATIVE left on) | `GGML_NATIVE is incompatible with GGML_CPU_ALL_VARIANTS: set GGML_NATIVE=off` |
| none, `--config x86_64` without variants | `GGML_NATIVE for a configuration the build machine does not run: ggml-cpu in x86_64-macos.release.none (x86_64-macos; the build machine is arm64-macos)` |
| DL without ALL_VARIANTS | `GGML_BACKEND_DL without GGML_CPU_ALL_VARIANTS is not modelled` (design input 1) |

**NATIVE + ALL_VARIANTS on ARM is an upstream bug that buildlog turns into an error.**
- Upstream rejects the combination on x86 only.
- On ARM it builds three identical `-mcpu=native` modules with no feats object, so they have no `ggml_backend_score`.
- The loader then selects **none**: `test-backend-ops` prints "Testing 0 devices" (`baseline/variants-native/smoke.txt`).

### Part 4: install layout (`comparison/install.txt`)

- `install.toml` installs:
  - `libggml-base` and `libggml` (with their links) to `libdir`;
  - the modules to `bindir`, which is upstream's `install(TARGETS … LIBRARY DESTINATION ${CMAKE_INSTALL_BINDIR})` when GGML_BACKEND_DIR is unset.
- `ninja install` produces upstream's layout.
- An installed program finds its modules: run from `/`, the loader picks `$PREFIX/bin/libggml-cpu-apple_m2_m3.so`.
- **But the installed files keep the build-tree rpath** (`@loader_path/../../lib/<cfg>`), so running needs `DYLD_LIBRARY_PATH=$PREFIX/lib`. CMake rewrites the rpath at install.
- The facts are per file and name the configuration (design input 5).
- Package export of native components is not supported yet (L3_install.dl, "exported component with a native artifact").

### Versioning (upstream's, recorded; not modelled beyond what buildlog spells today)

| artifact | upstream (CMake) | on macOS (`otool -l`, CMake = buildlog) |
|---|---|---|
| `libggml-base` | `VERSION 0.25.3`, `SOVERSION 0` (`GGML_VERSION_MAJOR`), src/CMakeLists.txt:211–214 | install name `@rpath/libggml-base.0.dylib`, current 0.25.3, compatibility 0.0.0 |
| `libggml` | same, src/CMakeLists.txt:363–366 | `@rpath/libggml.0.dylib`, 0.25.3, 0.0.0 |
| `libggml-cpu` (linked) | same, via `ggml_add_backend_library` (src/CMakeLists.txt:407–414) | `@rpath/libggml-cpu.0.dylib`, 0.25.3, 0.0.0 |
| `libggml-cpu-<level>.so` (modules) | unversioned: skipped `if (APPLE AND GGML_BACKEND_DL)` (CMake issue 20782); versioned on other platforms | a bundle, no `LC_ID_DYLIB` |
| package | `ggml-config-version.cmake`: `0.25.3`, `COMPATIBILITY SameMajorVersion`; `ggml.pc`: `Version: 0.25.3` | not exported by buildlog (install.toml copies files only) |

- The project states `native_version(t, "0.25.3", "0")` in rules.dl and spec.toml. It gives the file names, the install name and the current version. The compatibility version is always 0.0.0, which is also what CMake writes, because CMake sets no `MACHO_COMPATIBILITY_VERSION`.
- A module's only version check is at run time: the loader rejects a module whose `reg->api_version` is not `GGML_BACKEND_API_VERSION` (2; ggml-backend-reg.cpp:246).
- There is no `export_version`/`export_compat` for ggml, since native components cannot be exported yet.

### Upstream pain points: which buildlog solves

- **llama.cpp #20415: an unconditional `project()` resets the toolchain and arch when ggml is a subdirectory.**
  - Solved by construction: buildlog has no per-directory toolchain state.
  - The platform is a configuration's, and every instance is compiled for it.
  - A project included as a package cannot reset it.
- **The `HAVE_*` `check_cxx_source_compiles` results are cached by variable name across variants** (src/ggml-cpu/CMakeLists.txt, ARM branch).
  - They are computed once, with the first variant's (apple_m1's) flags, and the result is also unused.
  - In buildlog a check is keyed by its subject and configuration, so a per-level check would be distinct by construction.
  - The model does not need them.
- **NATIVE + ALL_VARIANTS on ARM** silently yields an unloadable set (above). Solved: it is an error.
- **`-mfp16-format=ieee` is not accepted by Apple clang.** Upstream guards it by compiler; here it is simply not stated for Apple.
- **MODULE libraries are awkward in CMake.**
  - They cannot be linked, cannot be versioned on macOS, and need `add_dependencies` so the loader builds them.
  - Their output location is the runtime dir by convention.
  - In buildlog the kind is fine, but the loader relation has no home (design input 2).

## Design input

Ranked by importance for step 4. Each item gives the rule wanted, the rule written, and where the fix belongs.

1. **Options cannot decide what is built, or a target's kind.**
   - *Wanted:*
     ```
     request(t, d) :- config_decl(d, _), ggml_all_variants(), cpu_backend_instance(_, t, l), l != "native".
     native_kind("ggml-cpu", "module_library") :- ggml_dl().
     ```
   - *Got:* `request depends on needed, creating a negation/stratification cycle`.
     - Options reach rules only as `config_attr option.*`, which shares a stratum with discovered attributes (`cc.check.*`), and those depend on `needed`.
     - A negation on `ggml_dl()` in `native_kind` cycles the same way.
   - *Workaround:* the phantom `ggml-cpu-variants` object library, `request_all`'d, with `dep_when` edges under an `$And` condition (rules.dl:136–160). Since step 4 the edges are the loader's `runtime_dep_when`, and the phantom is gone.
     - The native instance is always a shared library.
     - GGML_BACKEND_DL without variants (a native *module*) is an explicit "not modelled" error.
   - *Principled fix:* split `option_value` (user facts, known before `needed`) from discovered attributes, in L1_options.dl. Rules could then read options in the stratum where `request`, `dep` and `native_kind` are derived.
   - This is also the linkage-as-a-parameter question: "which kind is this instance" is a parameter that options pick.

2. **No run-time dependency relation (needed at run time, not linked).**
   - *Wanted:* `runtime_dep(loader, module, "<dir relative to consumer's executables>")` in core, with these consequences:
     - building or running the loader brings the modules;
     - the modules are co-located with the loader's consumers, in the build tree (bin/<cfg>/) and at install;
     - `buildlog test` and `run_artifact` consumers see them;
     - nothing propagates to links or usage requirements.
   - *Got:* a `dep` on a module would link it and propagate its public props into the loader, so the three consequences are hand-written (rules.dl:225–250):
     - order-only `action_in`;
     - SYMLINK actions through the builtin `create_symlink`, since `action_program_core` is not open to projects, so the core's `ln -sf` spelling cannot be reused;
     - order-only inputs on every executable whose `link_closure` holds ggml.
   - *Fix location:* L1_targets.dl (the relation), and L3_native.dl (co-location, like `shared_link`) plus L3_install.dl (install next to the loader). The relation is Bazel's `data`/runfiles, CMake's `add_dependencies` plus manual output dirs.
   - The same relation would serve plugins, dlopen'd codecs and Python extension modules.
   - *Step 4:* implemented as wanted in the build tree (`runtime_dep(t, m, place)` and `runtime_dep_when`, L1_targets.dl; co-location in L3_native.dl), with place `beside_executables` or `beside_loader`. A required module is installed with its loader (`runtime_dep`), an optional one only when listed: the ggml variants are optional, and rules.dl's `install_target(t, "bindir")` lists them as upstream's install does, so the build tree, the Ninja output and the install are unchanged.

3. **`native.link_flag` does not reach the target's own link.**
   - It is a link *item* placed for consumers: `-lm` landed on ggml-cpu's and the programs' links, not on ggml-base's own.
   - The target's own link needs `native.link_option`. That is one argv word, so `-framework Accelerate` must be written `-Wl,-framework,Accelerate`.
   - Fix: document the distinction, and let `native.link_option` take a multi-word option (or add a framework key, as zlib-ng's and Halide's Apple links would use too).

4. **No C-only flags and no C standard.**
   - buildlog-cpp has `cxx.compile_flag` and `cxx.std`, but no `c.compile_flag` or `c.std`.
   - *Workaround:* rules.dl:211–217 adds the C-only warnings and `-std=gnu11` to every action labelled `CC` of the directory's targets, at argv slot 14.
   - Related gaps:
     - `cxx.std 17` cannot spell `gnu++17` (CMake's `CXX_EXTENSIONS`);
     - `cc.compile_flag` is a set, so author order is lost; it was harmless here.
   - Fix: `c.compile_flag`, `c.std` and a `cc.extensions` setting in L4_cxx.dl, next to the C++ ones (lines 123–160).

5. **A project cannot derive install facts from artifacts.**
   - *Wanted:*
     ```
     install_file(t, f) :- module_library(t, _, f), cpu_backend_instance(_, t, _).
     ```
   - *Got:* `module_library` and `shared_link` are private to buildlog, while `shared_library` and `run_artifact` are exported, and `install_map` is owned by buildlog.
   - *Workaround:* install.toml lists the files per configuration by name.
   - Consequences:
     - installing both configurations would put two `libggml-base` in one place;
     - install copies files as they are, with the build-tree rpath and symlinks followed.
   - Fix (L3_install.dl):
     - `install_target(t, dir)`, meaning "t's artifacts and links";
     - rpath rewriting to the install layout, i.e. `@loader_path/../lib` for bindir→libdir;
     - consistent visibility of `module_library` and `shared_library`.

6. **`test_regex` is per test, not per configuration.**
   - The predicted module differs per configuration, so the regex belongs to (test, cfg).
   - Here the arm64 prediction also attaches to the x86_64 case. That is harmless only because that case is not run.
   - Fix: `test_regex_in(n, c, kind, re)` in L1_targets.dl (tests ~575–640).

7. **Rosetta: x86_64 on this arm64 Mac is runnable.**
   - `buildlog test` reports x86_64 as "not run (cross)", so the x86 smoke was manual.
   - Fix: a platform fact "exec platform p can run q", via an emulator such as `arch -x86_64` or qemu, in the platform plugin, consumed by the test runner.

8. **Sources are not per configuration.**
   - The native instance's arch-specific sources (`arch/arm/*`) cannot follow the configuration's platform, so they are stated for arm64 (rules.dl:112–118).
   - A native x86_64 build would need `c_source_when`/`cxx_source_when` with a platform condition. The variants avoid it, because each variant's level fixes the architecture.

9. **TOML loader friction** (facts.py 384–690):
   - top-level keys after a `[table]` belong to it, so arrays must come first;
   - a relation with a `number` column fills it from list position, so explicit numbers fail, and top-level scalar lists for such relations fail too (the `[isa_arch.arm64] arm_tag_order = [...]` form works);
   - numeric data (score weights) must be strings, converted with `to_number()`;
   - `cc_check` subjects cannot contain newlines;
   - rules must use the `$And`/`$Eq` ADT syntax for conditions, not the spec's string form;
   - Soufflé aggregates cannot take a disjunctive body, so a candidate relation is needed.

10. **Relation to the zlib-ng study (per-TU flags): proposed unification.**
    - Both are *one source set compiled at several ISA levels*.
      - ggml: per **target**. Every TU of `ggml-cpu-<level>` gets the level's flags, and one TU (the feats object) is exempt, done in CMake with a separate OBJECT target.
      - zlib-ng: per **TU**. Most TUs are baseline, a few get a level's flags, and dispatch is a function-pointer table in one library.
    - A single core notion covers both:
      - `isa_level(level, arch)`, `isa_level_feature(level, feat)`, `isa_feature(arch, feat)` and `isa_level_flag(level, flag)`, as named here;
      - a compile-scope choice, `isa_scope(t, level, "target" | "tu:<src>")`;
      - an exemption, `isa_exempt(t, src)`.
    - ggml's feats object becomes `isa_exempt`, and zlib-ng's per-TU flags become `isa_scope(… "tu:…")`.
    - The flags themselves (`-march` joins, `-m<feat>`) are toolchain-family facts that belong in F_clang/F_gcc, not in projects. This study's `arm_feature`/`x86_feature_flag` tables are the draft.
    - Compatibility errors ("variant for another architecture") are the same rule for both.
    - *Step 4:* done as design input §2 option (c). Levels are buildlog-cpp ISA sets; the scope is a source group (zlib-ng, `isa_group`) or a target (ggml, `isa_target`); the feats object is exempt by being another target, so `isa_exempt` was not needed. The spellings and knowledge are buildlog-cpp's (L2, F_clang).

11. **GGML_NATIVE as compile checks.**
    - Upstream uses run checks. The compile-check form (`-mcpu=native` defines `__ARM_FEATURE_*` for the host) works with Apple clang and can be overridden with `cc_check_known`.
    - It is only valid where the configuration runs on the build machine, which is an error otherwise.
    - A core "host features" probe (the same facts, from `sysctl`) would let both GGML_NATIVE and the score prediction work without a compiler.

12. **Versioning and the exported compatibility constraint** (see the ledger's Versioning table; not implemented).
    - ggml is `0.y.z`, and the soname carries only the major (`.0`). So **every 0.x release has the same install name**, `libggml-base.0.dylib`, although ggml breaks its ABI between minors (the backend and tensor structs change).
    - Upstream's CMake package says `SameMajorVersion`. Under CMake's rule, `find_package(ggml 0.25)` then accepts 0.26 or 0.99.
    - The macOS compatibility version (0.0.0) never lets dyld reject an older library either.
    - What a model should carry, per shared library and per package:
      - `VERSION` and `SOVERSION`: the file names and the install name;
      - the Mach-O current version (= VERSION) and compatibility version (CMake: `MACHO_COMPATIBILITY_VERSION`; unset here);
      - the package's CPS `compat_version`, i.e. buildlog's `export_compat`.
    - The coupling should be checked, not restated: `export_compat same_major` together with a `0.*` version should be a warning, or should mean "same minor" for 0.x, as semver and Cargo read it.
      - A downstream `requires ggml >= 0.25` should then mean `>= 0.25, < 0.26`.
      - The soname major should agree with the compat policy: an ABI boundary that the soname can't express is the author's bug to state, not the consumer's.
    - Modules, like the CPU variants, are never linked, so they need no install name. Their version constraint is the one they share with the loader they are built with:
      - ggml checks `GGML_BACKEND_API_VERSION` at `dlopen`;
      - that belongs on the runtime-dependency edge of item 2 (loader and modules of one build), not on an export.
    - Where it lives: L3_native.dl already has `native_version(t, version, soversion)`. A compatibility version and an `export_compat` derivation from it belong next to it and in L3_install.dl.

## UX input

These are authoring and usability frictions, separate from the design input above. Each item gives what I wrote, its line count, and what I wished I could write. Line numbers refer to `project/` at commit 49b1c39.

### Spec size

These are non-comment, non-blank lines, excluding source-list lines.

| buildlog (project/) | lines | upstream CMake, in scope | lines |
|---|---|---|---|
| spec.toml | 68 | CMakeLists.txt: the options, version and install parts in scope | ~60 (whole file 345) |
| cpu.toml | 239 | src/ggml-cpu/CMakeLists.txt: common, ARM, x86, feats and variant impl | ~292 (whole file 528) |
| rules.dl | 171 | src/CMakeLists.txt: flags, ggml-base, ggml, backend and variant functions | ~252 (whole file 500) |
| tests.toml | 17 | tests/CMakeLists.txt: test-backend-ops only | ~15 |
| install.toml | 23 | the install() calls | ~8 |
| buildlog.toml + 2 presets | 20 | the -D command lines | 1–2 |
| **total** | **~540** | | **~630** |

The totals are close, but the mix differs.
- About 75 of cpu.toml's lines are the score weights and ISA→define tables. Upstream keeps these in C (`arch/*/cpu-feats.cpp`) or implies them. I stated them as facts so that rules can predict the loader's choice.
- About 40% of the logic is Datalog (rules.dl, 171 lines). A new user could not have written it without reading core layers.

### Items (ranked by how much they cost me)

1. **Instantiating one backend per ISA level takes Datalog, plus a phantom target.** rules.dl:88–160, about 45 lines.

   What I wrote:
   ```
   cpu_backend_instance(b, cat(b, "-", l), l) :- cpu_backend(b), ggml_cpu_variant(_, a, l), isa_level(l, a).
   native_kind(t, "module_library") :- cpu_backend_instance(_, t, l), l != "native".
   prop(t, k, v, vis) :- cpu_backend_instance(b, t, _), cpu_backend_prop(b, k, v, vis).
   ... (sources ×4, dep, feats object ×4)
   target(g), native_kind(g, "object_library"), request_all(g) :- ggml_variant_set(_, g).
   condition(cat("ggml.variants.", os, ".", a), $And($Eq("option.GGML_CPU_ALL_VARIANTS", "on"), $And(...))) :- ...
   dep_when(g, t, "private", cat("ggml.variants.", os, ".", a)) :- ...
   ```
   What I wished for (about 6 lines of TOML):
   ```toml
   [target_template.ggml-cpu]          # sources/props/deps as for a target
   instantiate = { over = "isa_level", name = "ggml-cpu-{level}", kind = "module_library",
                   props = "isa_level_flag", when = "GGML_CPU_ALL_VARIANTS",
                   levels = "platform" }   # only the configuration's platform's levels
   ```
   The `cpu_backend_*` relations are my own copies of `c_source`/`cxx_source`/`prop`/`dep`, each re-attached by a one-line rule.
   (Step 4: replaced by core target templates, `target_template` + `instance_of`/`instance_param`; the phantom target went with §3. See "Step 4 changes".)

2. **The loader→module relation is written in Datalog.** rules.dl:225–250, 14 rule lines. (Step 4: replaced by one `runtime_dep_when` rule; see "Step 4 changes".)
   - It uses `action_in` order-only edges, SYMLINK actions through `action_builtin "create_symlink"` with hand-computed relative paths (the `substr(m, strlen(cat("out:lib/", n, "/")), …)` idiom), and order-only inputs on every executable in the `link_closure`.
   - What I wished for (1 line, in `[target.ggml]`):
     ```toml
     loads = [["ggml-cpu-*", "beside_executables"]]
     ```

3. **Two mechanisms, and two syntaxes, for options.**
   - spec.toml declares `option_decl` (3 lines) and a `condition` table in string form (`["dl", "$Eq(option.GGML_BACKEND_DL, on)"]`).
   - In rules.dl, conditions need the ADT form: `$And($Eq("option.GGML_CPU_ALL_VARIANTS", "on"), …)`.
   - To read an option in rules I had to extract it from `config_attr`. That took 5 lines:
     ```
     ggml_option(substr(k, 7, strlen(k) - 7), v) :- config(c), config_attr(c, k, v), substr(k, 0, 7) = "option.".
     ggml_dl() :- ggml_option("GGML_BACKEND_DL", "on").
     ```
   - What I wished for:
     - an option usable directly as a condition, e.g. `when = "GGML_BACKEND_DL"` or `when = "GGML_BACKEND_DL=on"`;
     - an exported `option(name, value)` relation;
     - one condition syntax.

4. **Option constraints are errors written in Datalog.** rules.dl:81–87 and 155–161, 4 rules, about 10 lines.

   What I wished for (3 lines of TOML):
   ```toml
   option_requires  = [["GGML_CPU_ALL_VARIANTS=on", "GGML_BACKEND_DL=on"]]
   option_conflicts = [["GGML_CPU_ALL_VARIANTS=on", "GGML_NATIVE=on"]]
   ```

5. **Joining a flag string takes a `join_item`/`join_rank`/`join_sep` triple per piece.**
   - `-march=armv8.6-a+dotprod+i8mm` took 11 lines (rules.dl:39–53), and `-mcpu=native+dotprod+…` another 9 (rules.dl:180–189).
   - Each piece repeats the join key `cat("ggml.march.", l)` twice.
   - What I wished for: a formatted or ordered-join property, e.g. `flag_join = { key = "...", parts = [...], sep = "" }`. Or, better, the ARM `-march` spelling as a toolchain-family fact (design input 10).

6. **Repetitive tables in cpu.toml.**
   - `ggml_cpu_variant` repeats `"macos", "x86_64"` 17 times.
   - `ggml_isa_define` is mechanical (19 lines: `X → GGML_USE_X` on ARM, `X → GGML_X` on x86).
   - `x86_feature_flag` is a compiler fact.
   - Each `[isa_level.*]` repeats `arch = "…"`.
   - What I wished for:
     ```toml
     [isa_arch.x86_64]
     define_prefix = "GGML_"
     levels = { x64 = [], sse42 = ["SSE42"], ..., haswell = ["SSE42", "AVX", "F16C", "FMA", "AVX2", "BMI2"] }
     platforms = ["macos", "linux"]
     ```
     That is about 20 lines instead of about 110.

7. **Four identical-shaped `cc_check` blocks** (24 lines). Their subjects can't contain newlines. What I wished for (3 lines):
   ```toml
   [cc_check_macro]
   args = "-mcpu=native"
   GGML_NATIVE_HAS = ["__ARM_FEATURE_DOTPROD", "__ARM_FEATURE_MATMUL_INT8", "__ARM_FEATURE_SVE", "__ARM_FEATURE_SME"]
   ```

8. **Install is per file and per configuration.** install.toml is 23 lines, and `out:lib/arm64-macos.release.none` is repeated 11 times. A project cannot derive `install_file` from artifacts (see the confusing errors in item 13). What I wished for (2 lines):
   ```toml
   install_target = [["ggml-base", "libdir"], ["ggml", "libdir"], ["ggml-cpu-*", "bindir"]]
   ```

9. **Property tuples are verbose and repeated.**
   - `["cxx.std", "17", "private"]` appears 6 times: on every target, the backend and the feats object.
   - `ggml_dir_prop` exists only because there is no directory or project scope: 16 lines plus 3 rule lines.
   - What I wished for:
     - a project default, `[defaults] cxx.std = "17"`;
     - a map form, `private = { "cc.define" = ["A", "B"], "cxx.std" = "17" }`.

10. **Things that live in Datalog but should be one TOML key:**

    | what | rules.dl | lines | wished TOML |
    |---|---|---|---|
    | C-only flags | 211–217 | 3 | `c.compile_flag`; see design input 4 |
    | private configured header | 219–224 | 3, with a `substr(p, 0, strlen(p) - 15)` to strip `/ggml-version.h` | `configured_file_include = "private"` |
    | per-configuration `test_regex` | 262–264 | 2 | `test_regex_when` |

11. **File sprawl.**
    - The spec is split over 5 files.
    - There are 2 presets.
    - There are 3 one-line `errors/*.toml` files, which exist only because options can be set only from user files.
    - What I wished for: a CLI `--option GGML_BACKEND_DL=on`. `-D` sets only `[vars]`. Named preset sets would then be one file.

12. **CLI friction.**
    - **Reconfiguring forgot `--config x86_64`.** Re-running `buildlog configure` with the same user files but no `--config` silently dropped the x86_64 configuration. `buildlog-state.json` stores user files and vars but not configs. `compare.py` then found 0 x86 objects and I spent a round on it.
      - Configs should persist like `--user`, or a narrowed rebuild should say "configuring release only (was: release, x86_64)".
    - **Every configure needs the full argument set** (`--user A --user B -D GGML_BUILD_COMMIT=… $P`). The build dir could remember the project path too.
    - **`buildlog test` reports x86_64 as "not run"** on a Mac that can run it through Rosetta (design input 7).
    - There is no `buildlog test --config`.

13. **Confusing errors.**

    | message | what it meant / what would have helped |
    |---|---|
    | `request depends on needed, creating a negation/stratification cycle` | "options (`config_attr option.*`) are computed after `needed`: use `dep_when`/`prop_when` with a condition instead". It cost several iterations. |
    | `... does not have values for all non-number columns` | I had given the number column explicitly. It is filled from list position. Say "column 'rank' is implicit: drop it". |
    | `rows must be arrays or tables` | For a top-level list feeding a relation with a rank column. It should suggest the `[subject] key = [...]` form. |
    | `body relation shared_link is private to buildlog`, `clause head install_map is owned by buildlog; not declared in extends` | The same `install_map` is fine from TOML. Say "state it as TOML facts, or …". |
    | none: `native.link_flag "-lm"` on ggml-base | Silently landed on consumers' links, not ggml-base's own. A lint "link_flag on a target nothing links against it for" would have caught it. |
    | none: `native.link_option "-framework Accelerate"` | Passed as one argv word, and the linker error was far away. Warn on a space in a single-word option. |
    | TOML: top-level keys after a `[table]` | Silently belonged to the table. The loader could warn when a table gets a key that is a known top-level relation. |

14. **`why`.** I used it little. I checked flags through `ninja -t commands` and compare.py, because I needed per-object diffs against CMake, not provenance of one flag. A `buildlog diff --against compile_commands.json` would have replaced compare.py's compile section, about 120 lines.

## Tests

`tests/test_ggml_case.py` (integration). It skips without the checkout
(`BUILDLOG_GGML_SRC`), ninja, Soufflé or xcrun, and checks:

- the linked build's flags against the baseline, and `buildlog test` passing 2/2;
- the variants build: the module set and, per module and feats object, the `-m`/`-U`/`-DGGML_*` flags against both CMake baselines;
- the modules symlinked next to the programs and not linked into the loader;
- `buildlog test` passing with the predicted module (x86 not run);
- the four error demos.

## Not verified

- **x86_64 at run time:** checked only by hand under Rosetta (sse42). The higher x86 levels never ran.
- **SVE/SME paths:** no machine here has them. The apple_m4 module is built and flag-checked but never loaded.
- **The CMake install tree:** not built. The install comparison is against upstream's install rules as read, not an installed tree.

## Step 4 changes

Spec lines are measured with `historical source-line accounting` (non-blank, non-comment, without
single-path source-list lines).

| Change | Files | Before | After |
|---|---|---|---|
| §3: the loader→module relation is core's `runtime_dep_when` (placed `beside_executables`); `ggml_loads`, its order-only edges, symlink actions and the phantom `ggml-cpu-variants` target go | rules.dl | 171 | 157 |
| §1: the per-ISA backends are core target templates (`[target_template.ggml-cpu-backend/-variant/-feats]`, `{arch_dir}`, `{isa_define}`, `{name}` placeholders); the `cpu_backend*` relations and their re-attaching rules go | rules.dl | 157 | 125 |
| | cpu.toml | 206 | 214 |
| | total | 468 | 444 |
| One library kind (the PI's decision): `ggml-base`, `ggml` and `ggml-cpu` were `shared_library`; they are `library` with `default_linkage = "shared"` (upstream honours `BUILD_SHARED_LIBS`, default on), and `GGML_BUILD`/`GGML_SHARED`, `GGML_BACKEND_BUILD`/`GGML_BACKEND_SHARED` become shared `linkage_prop`s; the variant template states its backend defines | spec.toml | 58 | 68 |
| | cpu.toml | 261 | 263 |
| | project total | 462 | 474 |

One library kind: the graph is identical (snapshot diff empty). A user can now
choose `library_linkage = [["ggml", "static"]]`; the defines then go with the
shared build.

- The tests are unchanged and pass: the modules are built with `libggml`, linked beside
  `test-backend-ops` and `simple-backend`, and the loader picks the predicted module.
- The graph differs only in the module links: core spells them `ln -sf` (as it does a
  shared library's links) instead of the `create_symlink` builtin, and they are built
  as the executables' order-only inputs rather than by default.
- Templates: ggml.ninja and ggml-variants.ninja are byte-identical before and after.
  The variant's ISA flags remain a rule, not a template parameter (see "How it is
  modelled").
- §2 (ISA vocabulary): the ARM/x86 feature tables (`arm_feature`, `x86_feature_flag`, `arm_tag_order`, `arm_version`), `isa_feature`, the flag derivation and the architecture check go to buildlog-cpp; ggml maps its feature names to extensions (`ggml_feature_ext`). Measured after the UX port: rules.dl 119 → 93, cpu.toml 242 → 235 (the mapping is 23 lines; AVX512 is five). ggml.ninja and ggml-variants.ninja are byte-identical before and after. The "another architecture" error is now buildlog-cpp's, on the variant (the feats object is linked into it, so it is not repeated).
- Not done: install beside the loader's consumers (install.toml still names each module
  and the configuration), and the API-version coupling on the edge (the loader rejects a
  module whose `GGML_BACKEND_API_VERSION` differs) is recorded here, not stated.

### Linux (step 4)

`linux/run.sh cases gcc|clang` configures, builds and tests the linked build on arm64
Linux with GCC 13 and Clang 18. Both pass 2/2, and `tests/test_ggml_case.py` compares
`ggml-cpu`'s flags with upstream's `baseline/linux` (`linux-clang`) `cpu` build: 5 tests
pass, the variants and x86_64 tests are macOS-only. The spec changes are ports of what
upstream conditions on the platform:

- **Platform-conditioned.** Accelerate (its three defines and `-framework Accelerate`)
  only on macOS, as `APPLE AND GGML_ACCELERATE`. The per-OS definitions
  (`ggml_dir_prop_os`) give `_DARWIN_C_SOURCE` on macOS and `_GNU_SOURCE` on Linux, as
  CMake's `CMAKE_SYSTEM_NAME` tests do.
- **GGML_NATIVE's tags follow upstream's two checks:**
  - `+<tag>` only where the compiler accepts `-mcpu=native+<tag>` (`GGML_NATIVE_PLUS_*`);
  - `+no<tag>` (with `-U__ARM_FEATURE_<F>`) only where it accepts `-mcpu=native+no<tag>`
    (`GGML_NATIVE_NO_*`).
  - GCC 13 accepts no modifier after `native`, so it gets plain `-mcpu=native`, as with
    CMake.
  - The machine answers (`GGML_NATIVE_HAS_*`) are unchanged.
- **GGML_NATIVE and OpenMP, now modelled** (the comparison no longer filters flags):
  - The feature checks are `runs` checks, as upstream's `check_cxx_source_runs`: the
    program is built with `<native flag>+<tag>` and run, so a machine feature a compiler does
    not know (Clang 18 on an Apple-silicon VM sees no dotprod in `-mcpu=native`) is still found.
    `+no<tag>` is a compile check, as upstream.
  - The native flag is the option `GGML_NATIVE_CPU` (default `-mcpu=native`). GCC has no
    `-mcpu=native+<tag>`; upstream reads the `-march=armv8-a+crc+…` list from
    `gcc -mcpu=native -E -v` and appends the tags. buildlog runs nothing at configure
    time, so the user states that list (a machine fact, like `cc_check_known`):
    `case-studies/ggml/native_cpu.py CC` prints the user file (the cases and the tests use it).
    GCC's feature checks use `-march=armv8-a+<tag>`, `cc_check_args_family`.
  - OpenMP is the check `GGML_HAVE_OPENMP` (`-fopenmp`, `omp.h`, `omp_get_num_threads`) and
    the option `GGML_OPENMP`: `-DGGML_USE_OPENMP`, `-fopenmp` and the link option on ggml-base
    and every CPU backend. GCC finds libgomp; Clang 18 in the image has no libomp and none is
    used, as upstream. (Upstream links libgomp by absolute path, buildlog by `-fopenmp`.)
- **Linux ARM variants** (`GGML_CPU_ALL_VARIANTS`): upstream's list for
  `CMAKE_SYSTEM_NAME` Linux is `ggml_cpu_variant`'s `linux` rows (`cpu.toml`), the levels in
  the existing `isa_level` vocabulary, each adding to its predecessor:

  | level | features |
  |---|---|
  | armv8.0_1 | none (`-march=armv8-a`, a rule: buildlog-cpp spells flags for what a level adds) |
  | armv8.2_1 | DOTPROD |
  | armv8.2_2 | + FP16_VECTOR_ARITHMETIC |
  | armv8.2_3 | + SVE |
  | armv8.6_1 | + MATMUL_INT8 |
  | armv8.6_2 | + SVE2 |
  | armv9.2_1 | armv8.6_1 + SME (no SVE2) |
  | armv9.2_2 | armv8.6_2 + SME |

  - **Clang 18:** all eight modules build; each module's and its feature object's `-m*`, `-U*`
    and `-DGGML_*` flags equal upstream's CMake build (`baseline/linux-clang/variants-arm64`),
    the loader sits beside the executable, and `buildlog test` passes only if the module the
    loader picks is the one predicted from the machine: on the Docker VM's CPU (Apple
    silicon: dotprod, fp16, i8mm, no SVE) `armv8.2_2` is loaded, as the rules predict
    (armv8.6_* need SVE, so the machine can run only armv8.0_1, armv8.2_1 and armv8.2_2).
  - **GCC 13** has no `+sme`: buildlog reports the two armv9.2 levels as errors at configure
    time (the ISA spelling of GCC lacks SME), and upstream's CMake configures and then
    fails to compile exactly those two modules (`baseline/linux/variants-arm64/build-status.txt`).
    The other six build under CMake; with GCC they are not compared, since buildlog's
    configure stops. A newer GCC would build all eight.
- **Residuals:**
  - (Gone: `-Wunreachable-code-{break,return}` are now conditional on `cxx.family`, like
    upstream's `ggml_get_flags`; install.toml's installed files come from `install_target`.)
  - Upstream's `-ldl` for ggml on Linux is not stated, since glibc 2.34 has `dlopen` in
    libc.

The macOS graphs (ggml.ninja, ggml-variants.ninja) are byte-identical. The eight new
checks are probe questions only. Spec lines: rules.dl 93 → 102, cpu.toml 235 → 261 (the
eight checks are 16), spec.toml 55 → 58, total 383 → 421.

## UX pass port

The UX-pass port moves the core targets to `[target.*]`/`[target_template.*]` property maps, replaces option declarations and two custom option-error rules with `[option.*]` `requires`/`conflicts`, and uses `join_part` for the ordered ISA flag strings. The per-ISA facts, score weights, custom C-only flag workaround, configure checks and loader score prediction remain project rules.

| file | before | after |
|---|---:|---:|
| buildlog.toml | 6 | 6 |
| cpu.toml | 246 | 251 |
| install.toml | 23 | 23 |
| rules.dl | 171 | 119 |
| spec.toml | 68 | 55 |
| tests.toml | 19 | 18 |
| **total** | **533** | **472** |
| **Datalog** | **171** | **119** |

Graph identity: linked arm64 and variants arm64+x86_64 are IDENTICAL by `ninja_norm.py`. Diagnostics intentionally changed for the two option constraints to core's `option requirement not met` and `options conflict` messages. Tests: `uv run --frozen pytest -q tests/test_ggml_case.py tests/test_spirv_tools_case.py tests/test_halide_phase*.py` (with Halide prefix) passed as part of the combined run.

Vocabulary gaps left here: rules still need ADT condition construction for generated per-platform runtime conditions (~4 lines); option values are still read through `config_attr` for project-specific errors (~5 lines); C-only flags/C standard need action rules (~3 lines); configured-header private include still needs a substring rule (~3 lines); `test_regex` is not per-configuration (~2 lines); ISA/toolchain feature tables are still project facts (roughly 40-60 lines if shared by core/toolchain vocabulary).

T2: the four `-mcpu=native` checks share `[defaults.cc_check]` (language, kind, arguments),
so each is its name and program; 465 → 457 lines. Graphs `IDENTICAL`.
