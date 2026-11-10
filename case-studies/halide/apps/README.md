<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Halide cross-compilation apps: HelloWasm and HelloAndroid

Two Halide apps whose CMake builds need a super-build or a nested configure
because the generators must run on the **host** while the code they emit is
compiled for another platform. Here each is **one `buildlog configure` and one
Ninja graph**: the generator is an ordinary target that the Halide library
names, so the host toolchain is derived from that need (it is an executable run
by an action), and the app's configurations use the SDK's toolchain.

```
apps/
  hellowasm/     apps/HelloWasm   -> wasm32-emscripten (4 configurations)
  helloandroid/  apps/HelloAndroid -> aarch64-android, x86_64-android (JNI .so)
  baseline/
    run-cmake.sh                the CMake builds the studies compare with
    commands.py                 extract / compare commands of Ninja trees
    hellowasm-variants.json     maps configurations to the baseline's unrolled variants
    cmake-hellowasm.json        compact CMake baselines (placeholders, no personal paths)
    cmake-helloandroid.json
```

Prerequisites (see `docs/artifact-guide.md`): `BUILDLOG_HALIDE_PREFIX` (Halide
22 install with the WebAssembly and ARM/AArch64 backends), `EMSDK` (3.1.73) and
`EM_BINARYEN_ROOT` (binaryen >= version_123; emsdk's bundled 119 rejects the
features LLVM 22 emits, for CMake and buildlog alike), `ANDROID_NDK_ROOT`
(NDK 30). The Halide sources are the submodule `case-studies/halide/upstream`.

## Reproduce

```sh
. env.local.sh
# buildlog
uv run --frozen buildlog configure case-studies/halide/apps/hellowasm -C $BUILD/wasm \
  --package-path case-studies/halide/packages -D HALIDE_PREFIX=$BUILDLOG_HALIDE_PREFIX \
  --user case-studies/halide/apps/hellowasm/presets/homebrew-arm64.toml
ninja -C $BUILD/wasm/souffle && uv run --frozen buildlog test -C $BUILD/wasm
# the same for helloandroid (no tests: the outputs are device code)
# CMake baseline, and the comparison
case-studies/halide/apps/baseline/run-cmake.sh wasm        # BUILD=... to relocate
python3 case-studies/halide/apps/baseline/commands.py compare \
  --variants case-studies/halide/apps/baseline/hellowasm-variants.json \
  --root PREFIX=$BUILDLOG_HALIDE_PREFIX --root EMSDK=$EMSDK --root SRC=$HALIDE_SRC/apps/HelloWasm \
  --root BUILD=$BUILD/cmake case-studies/halide/apps/baseline/cmake-hellowasm.json $BUILD/wasm/souffle --details
```

`commands.py` reduces `ninja -t commands` to records: compiles `(target,
source, flags)`, generator runs `(name, Halide target string, params)`, runtime
runs, links and archives. Machine-specific macOS arch/SDK flags are dropped.

## What changed in buildlog (principled, general)

The toolchain knowledge is in two **provisional plugins** that stand in for
what the NDK and Emscripten ecosystems would ship (as `android.toolchain.cmake`
and `Emscripten.cmake` do for CMake); see `plugins/README.md`:

- `plugins/buildlog-ndk`: discovery (`ANDROID_NDK_ROOT`), the NDK's clang,
  the arm64/x86_64 Android platforms, the NDK's default compile and link
  arguments, and the system libraries as packages (`ndk::log`, `ndk::android`,
  `ndk::atomic`: a `dep` on them is `target_link_libraries(t log)`).
- `plugins/buildlog-emsdk`: discovery (`EMSDK`), the `emscripten` toolchain
  family, the wasm32-emscripten platform, `.js` + `.wasm` programs, and what
  Emscripten.cmake and emcc imply: the settings `emscripten.simd`
  (`off|simd128`) and `emscripten.threads` (`off|threads`), whose default is
  left out of configuration names (so the wasm configurations are
  `wasm32-emscripten.release.none`, `.simd128`, `.threads`, `.simd128.threads`);
  `-O3` on the link of a `native.opt = release` configuration (emcc optimizes
  at link time); `-matomics` on the link of a threaded configuration (what
  `Threads::Threads` plus `-matomics` give upstream; `Threads::Threads` itself
  adds nothing on Emscripten); the SDL2 port as the package `emscripten::sdl2`
  (`--use-port=sdl2` at compile and link); and the open `config_cpu_feature`
  rows (`simd128`, `threads`). `-sWASM=1` is not spelled anywhere: it is emcc's
  default, so the spec and the plugin leave it out (a deviation in the table
  below, with no effect on the output).
- **buildlog-cpp** (generic hooks): `config_cpu_feature` (open: the CPU
  features a configuration enables, neutral names, stated by an SDK plugin from
  its settings and read by project rules), `cross_target` (a cross toolchain targets
  only its own platforms and no other toolchain targets them, so no pin is
  needed), `platform_os_level` (spelled into clang's triple), open `elf_driver`
  and `platform_vocabulary`, and capability probes that read any plugin's
  `res_cross_<name>_*` inventory.
- **Core** (`L3_native.dl`): open `platform_executable_suffix`,
  `platform_link_side_file` and `tc_link_arg` (a toolchain's link-only defaults),
  and `setting_name_elide` listed as open (a plugin's settings keep default
  values out of configuration names).
- **Spec loader** (`facts.py`): `[target.x] key = { column = value, ... }` states
  one row of the key's relation by column name (a symbol column left out is
  empty), and each other key in the table states the relation `<key>_<part>`
  for the target. This is what makes `halide_build_library = { from = ... }`
  readable; it is general (any relation with the target as its first column).
- **halide-build** (an ordinary rule package, no discovery; no Emscripten or
  NDK names in it): `halide_build_generator` (the sugar for
  `add_halide_generator`: executable, `Halide::Generator`,
  `-Wdeprecated-declarations`, what the helper adds under GNU/Clang),
  `halide_build_library = { from = ... }` (the sugar for `add_halide_library`:
  `GENERATOR` and `FUNCTION_NAME` default to the target name and the runtime
  to the library's own, as in CMake; `registration = "<consumer>"` writes the
  registration where its consumer is built; `params` keyed by condition),
  Halide target features derived from
  `config_cpu_feature` (`simd128` -> `wasm_simd128`, `threads` ->
  `wasm_threads`), registration written only where its consumer is built, the
  `wasm-32-wasmrt` / `android` Halide targets, and `halide_build_install`
  (the imported `Halide::Runtime`, `Tools`, `ImageIO`, `GenRT` of an installed
  Halide, which CMake's package files do not give a non-CMake consumer).
- Only what is specific to the apps (the targets, options, Halide targets) is in
  the specs.

## HelloWasm

**Upstream.** A CMake super-build. `ExternalProject` builds `generator/` with
the host compiler, configures `app/` under Emscripten's toolchain file, and
runs the generator for four `wasm-32-wasmrt` variants (`wasm_simd128` crossed
with `wasm_threads`; each variant's `init`, `update`, `render` pipelines), then
links `index*.js/.wasm` with `em++` and SDL2 from Emscripten ports. The generator
project also builds a native runner (`RunGenMain`) and three benchmark tests
(`ctest`).

**buildlog.** `hellowasm/spec.toml`, 138 lines (plus a 13-line project file and
a 13-line user preset naming where the CMake exports are; it was 307 lines
before the rewrite, which unrolled CMake's `foreach` by hand: 15
`halide_build_library` rows, 30 `prop` rows, `file_base` rows). The spec states
the app's own facts: one `index` target, the generator
(`halide_build_generator = true`), the three libraries once, `run`, the
`-sTOTAL_MEMORY=512MB` link option, `cxx.std = 17` in a profile, the `threads=`
param of the generator by condition, the benchmarks, and five configurations:
`host` and the wasm configurations `wasm`, `wasm-simd`, `wasm-threads`,
`wasm-simd-threads`. **The variants are configurations**, which CMake cannot do
(one target in several configurations of one tree; hence upstream's
unrolling): the same `index` is built in four wasm32-emscripten configurations
that differ in `emscripten.simd` and `emscripten.threads`. The Halide target
features (`wasm_simd128`, `wasm_threads`) are derived from the configuration,
the compile and link flags from the plugin. The generator and `run` are built
for the host because the libraries and the program name them. One configure,
75 Ninja steps.

**Added to the command lines.** Nothing is added to the compile lines (the SIMD
setting changes only the Halide target, as upstream: no `-msimd128` is passed
to the app's own compile). On the link, `-O3` (from `native.opt`) and, in
threaded configurations, `-matomics` are upstream's own spelling, derived
instead of stated.

**Results.** All four `.wasm` pass `wasm-validate` (`--enable-threads` for the
threaded ones). The three native benchmark tests pass
(`buildlog test`: `init_bench`, `update_bench`, `render_bench`).
Running `index.js` is impractical: the demo needs SDL's canvas and a browser,
and is not exercised under node, so the study stops at build fidelity (a plain
Emscripten executable does run under node, checked during development).

**Fidelity vs CMake** (`commands.py compare`):

| records | CMake | buildlog | same keys | notes |
|---|---|---|---|---|
| compile | 9 | 9 | 9/9 | `-I`/`-isystem` only (deviation 1); `--use-port=sdl2 -O3 -DNDEBUG -std=c++17` identical |
| generator runs | 15 | 15 | 15/15 identical | target strings, features, params, `-e`, `-f`, `-g` |
| runtimes | 15 | 15 | 15/15 identical | `gengen -r` name and target string; the names carry no variant suffix upstream either, so `--variants` has nothing to map |
| archives | 30 | 30 | 30/30 | |
| links | 6 | 6 | 6/6 | |

Deviations, with causes:
1. Include spelling: buildlog passes generated-header and Halide include
   directories as `-isystem`, CMake as `-I` (buildlog's include model marks
   imported/generated directories as system; CMake's FILE_SET base dir is `-I`).
2. libpng/libjpeg paths come from pkg-config (`libpng16`, `-lz`) rather than
   CMake's `FindPNG` (`/opt/homebrew/include`, `libpng.dylib`, `libz.tbd`):
   different discovery mechanisms for the same libraries.
3. `-O3 -DNDEBUG` is not repeated on host link lines (CMake passes
   `CMAKE_CXX_FLAGS_RELEASE` to the link); on wasm `-O3` is a link option and
   `-DNDEBUG` is absent. Cause: buildlog's optimization level is a compile
   fact, not a link one.
4. `-Xlinker -force_load` vs `-Wl,-force_load,`: spelling of the same option.
5. Runtime and library names: a configuration has one set of three libraries
   (`reaction_diffusion_init`, `.runtime`), upstream one per variant
   (`index_simd_init`, ...); `commands.py --variants` maps the names. Same
   content, same `gengen -r` target strings, same `-n`/`-g`/`-f` arguments.
   Consequently each library has its own generated-header directory
   (`-isystem` per library), CMake one `OUTPUT_DIR` per variant.
   Registration sources are written only for the host libraries (where `run`
   consumes them), as upstream.
6. macOS `-arch`/`-isysroot` and the linker defaults
   (`-Wl,-search_paths_first`, `-headerpad_max_install_names`) differ from
   `--target=...-apple-...`; the host toolchain facts differ in spelling only.
7. `RunGenMain.cpp` is compiled as a listed source of `run`, because the CMake
   importer rejects `Halide::RunGenMain`'s `INTERFACE_SOURCES`.
8. The static assets (`index.html`, `main.js`, `sw.js`) CMake copies next to
   the outputs are not modeled: they are copies, not build steps that depend
   on the toolchain.
9. `EM_BINARYEN_ROOT` points to binaryen 123 for both builds (not a deviation
   between them; a deviation from stock emsdk 3.1.73).

## HelloAndroid

**Upstream.** Gradle's `externalNativeBuild` runs `app/src/main/cpp/CMakeLists.txt`
under the NDK toolchain file, once per ABI (`abiFilters`: `arm64-v8a` and
`x86_64`; minSdk 23). That file configures itself a second time for the host
(`execute_process` into `.cxx-generators`) to build the two generators, then
runs them for `arm-64-android` / `x86-64-android` and links `libHelloAndroid.so`
(JNI) with `log android atomic`. Upstream builds no `armeabi-v7a`, so neither do
we.

**buildlog.** `helloandroid/spec.toml`, 61 lines (+13 project, +7 preset).
Each generator is one `halide_build_generator = true` table plus its source,
`cxx.std = 17` is in a profile, and the system libraries are the NDK plugin's
packages (`ndk::log`, `ndk::android`, `ndk::atomic`), not raw link options.
Configurations `arm64-v8a` (aarch64-android) and `x86_64` (x86_64-android) and
the host in one graph; the generators are built because the libraries name them,
and run on the host. One configure, 30 Ninja steps.

**Results.** Both `.so` are ELF `DYN` shared objects of the right machine
(AArch64, x86-64), `DT_NEEDED` is `libandroid liblog libm libdl libc` (no
libc++, no Halide: both static), SONAME `libHelloAndroid.so`, and the eight
`Java_...` JNI entry points are exported (`llvm-readelf`, `llvm-nm` from the NDK).

**Out of scope:** Gradle, Java/Kotlin compilation, APK packaging and signing,
and running on a device or emulator. Gradle cannot run here (no JRE; it needs
Gradle 9.6.1 and AGP 9.3.0 and the full SDK). The baseline therefore
configures upstream's `cpp/CMakeLists.txt` directly with the NDK toolchain file,
same ABIs and API level, `Release`; Gradle would use its own build types.

**Fidelity vs CMake** (the comparison keys collapse the two ABIs; the
x86_64 records have the same shape):

| records | CMake | buildlog | same keys | notes |
|---|---|---|---|---|
| compile | 8 | 10 | 4 | 4 differ in flags (below); 2 extra are the host generators |
| generator runs | 4 | 4 | 4/4 identical | |
| runtimes | 4 | 4 | 4/4 identical | |
| archives | 8 | 8 | 4/4 identical | |
| links | 2 | 4 | 1 | 2 extra are the host generators |

Deviations, with causes:
1. The triple is `aarch64-linux-android23`, CMake's `aarch64-none-linux-android23`
   (the NDK toolchain file's spelling; the same target to clang). The probe
   takes it from the NDK's own tools.
2. The generators' host compiles/links appear in buildlog's graph; CMake builds
   them at configure time inside a nested configure, outside the ABI's Ninja
   graph (that is the point of the study).
3. CMake adds `-DHelloAndroid_EXPORTS`, `-pthread`, `-fPIC` and `-I$BUILD`
   (compile), and `-pthread -ldl -lm`, `-fPIC` and the dependency-file option
   (link): these come from CMake's own target model (a SHARED library's
   export define and PIC, the Threads dependency of Halide's runtime
   interface). buildlog states none of them; the `.so` still links
   (`--no-undefined`) and `-isystem` carries the generated header
   directories. `-O3 -DNDEBUG` are not on the link line (see HelloWasm, 3).
4. Archive tool flags (`llvm-ar` flags) are the toolchain's own.
5. `-Wl,--dependency-file` is a CMake-generator detail.

## Metrics (this machine: Apple silicon, 16 cores, `-j4`/`-j8`)

| | HelloWasm | HelloAndroid |
|---|---|---|
| spec lines (`spec.toml`, `wc -l`; non-blank, non-comment) | 138; 96 | 61; 40 |
| project + user preset lines | 13 + 13 | 13 + 7 |
| CMake description (CMakeLists of the app, incl. super-build) | upstream `CMakeLists.txt` + `generator/` + `app/` | `app/src/main/cpp/CMakeLists.txt` (+ Gradle's `build.gradle`) |
| Ninja steps (`ninja -t commands`) | 75 | 30 |
| CMake commands (all trees) | 78 | 13 per ABI |
| historical configure time (cold build dir and cache; median, range of 10; predates this release) | 3.1 s (3.0-3.5) | 2.3 s (2.2-2.5) |

Rule and plugin lines, counted separately from the specs (non-blank,
non-comment lines):

| | Datalog | Python |
|---|---|---|
| `plugins/buildlog-ndk` (provisional; stands in for the NDK) | 43 | 104 |
| `plugins/buildlog-emsdk` (provisional; stands in for Emscripten) | 51 | 73 |
| `halide-build` rule package (`halide_build_generator.dl`, `halide_build_install.dl`) | 252 | 0 |

The generic additions these studies needed (buildlog-cpp's cross hooks, core's
`platform_executable_suffix`, `platform_link_side_file` and `tc_link_arg`,
the named-row sugar in the spec loader) are shared by any project and are not
counted against them.

## Linux (tier 3 HelloWasm; tier 4 HelloAndroid)

The specs (`hellowasm/spec.toml`, `helloandroid/spec.toml`) and the emsdk and NDK plugins are the
macOS ones; the rules derive the flags, there are no hand-written flag combinations. Only
`presets/linux.toml` is new for each (where the installed Halide's exports are, apt's pkg-config
directory for libpng/libjpeg). Both build in the one image: `linux/run.sh cases gcc|clang hellowasm helloandroid`;
`tests/test_halide_apps.py` passes 7/7 (it needs the Halide checkout, which the container copy does
not hold, so the suite skips it).

- **HelloWasm**: Emscripten 3.1.73 from emsdk in the image (its Linux arm64 binaries), binaryen
  version_123 as on macOS (`EM_BINARYEN_ROOT`), the SDL2 port pre-built into the cache. The host
  generators link the installed Halide (`/opt/halide`, built in the image from the submodule) with an
  rpath (the importer change in `case-studies/qt-moc/README.md`). 75 Ninja steps.
- **HelloAndroid, emulation approach**: NDK r30-beta2's Linux host tools are x86_64 only. The
  image stays arm64 and runs them through Docker Desktop's binfmt x86_64 emulation, with amd64
  `libc6`, `libstdc++6` and `zlib1g` installed through apt multiarch (no second image: the build
  machine is the one arm64 container, `runs_on` is arm64-linux; the plugin needs no change, it only
  spawns the NDK's clang). On a native x86_64 Linux host the same Dockerfile works unemulated. Both ABIs
  link (`libHelloAndroid.so` is AArch64 and X86-64), 30 steps. Emulation costs build time on the
  NDK's clang (a few seconds here); nothing was infeasible.
- **Against CMake** (`baseline/linux/cmake-*.json`, `baseline/linux-clang/` for Clang; the compare
  steps of `cases.sh`): the shape of the macOS tables.
  HelloWasm: compile 9/9 same keys (`-I`/`-isystem` only differ, deviation 1), generator runs 15/15
  identical, archives 30/30, links 6/6 same keys (deviations 3-5: `-DNDEBUG -O3`
  and `-sWASM=1` not on the link line, library-name mapping, and ELF's
  `--whole-archive,…,--no-whole-archive` spelling for GenGen where macOS has `-force_load`). The
  `run` link has `libm`, `libpng16`
  where CMake has `libpng.so`. Halide's installed export states no link
  libraries for `Halide::Runtime`; `add_halide_library` (HalideGeneratorHelpers.cmake, a function, not an
  exported target property) adds `Threads::Threads` and `${CMAKE_DL_LIBS}` to each generated runtime
  library. buildlog's `halide_build` rules now do the same (fixed): each runtime library depends publicly on
  two interface targets whose link flags come from buildlog-cpp's per-OS libc facts
  (`platform_os_threads_arg`, `platform_os_dl_lib`: Linux `-pthread` and `-ldl`; macOS, Emscripten and
  Android's bionic state "nothing"; an OS with no stated value is an error). The Linux `run` link has `-ldl`
  as CMake's does; macOS, wasm and Android links are unchanged. One difference remains: the link also has
  `-pthread`, which CMake leaves out here because FindThreads finds pthread in glibc 2.34+'s libc (it would
  give `-lpthread` or `-pthread` on an older glibc); buildlog states the OS's flag and does not probe, and
  `-pthread` is also left off compiles. (`-ldl` and `-pthread` are not needed to link on 24.04, but are with older glibc.)
  The compare tool's runtime records formerly keyed on the target string alone (the 3 runtimes
  of a target collapsed to 1 key, "5 same keys, 1 differing" with a record-order difference
  and no detail); they key on name and target now: 15/15 identical, on macOS too. HelloAndroid: generator runs 4/4, runtimes 4/4, archives 4/4 identical;
  compile 8 vs 10 and links 2 vs 4 (the two host generators, deviation 2), one link differs
  (`--target=aarch64-linux-android23` against CMake's `…-none-…`, `-fPIC -pthread -ldl -lm`, deviations
  1 and 3).
