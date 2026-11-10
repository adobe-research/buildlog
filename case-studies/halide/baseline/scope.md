<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Halide case study: scope map of the CMake baseline

What the Phase 0 CMake ground truth contains, how it is carved into case-study phases,
which host tools it runs, which tests are sampled, and which CMake behaviours a buildlog
spec will have to match (or deliberately not match).

- **Source:** Halide main at `4e272cbfcc` (2026-10-13), which contains PRs #9473, #9469 and
  #9548 (the `getauxval` include order), taken from the git objects of the `case-studies/halide/upstream` submodule.
  The numbers below were recorded at the previous pin, `35b7aa604e` plus a patched-in
  #9473 (and #9469 for universal2); at `f370931c79` every variant's normalized graph was
  byte-identical (see [Deviations](#deviations)).
  The build uses a `git archive`
  export under that worktree's gitignored `build/`, not the worktree itself (see
  [Deviations](#deviations)).
- **Configuration:** the `macOS` preset (Ninja, `CMAKE_BUILD_TYPE=Release`) with
  `-DHalide_LLVM_ROOT=$LLVM`, where `$LLVM` is Homebrew's `llvm@22` keg (LLVM 22.1.8).
  Everything else is at its default. That includes `WITH_SERIALIZATION=ON` with Homebrew
  flatbuffers 25.12.19, and `Halide_WASM_BACKEND=wabt` with Homebrew wabt 1.0.42.
- **Graph:** the release `build.ninja` has 9092 build edges and the File API
  codemodel 2.11 has 1490 targets. Of the edges, **2711 are in scope** and 6381 are
  excluded with a reason.

The phases are assigned by the ordered rules in [`scope.toml`](scope.toml) (first
match wins; conditions on CMake target, source dir, kind, tool and output). A test
target is in scope iff it is in the transitive File API dependency closure of a sampled
test. Numbers below are from `metrics.py scope-tables --baseline cmake-release.jsonl`.

## Edges per phase and kind (release)

`kind` is derived from the command, not the rule name. `compile` includes the 408
`clang-22 -emit-llvm` runtime compiles, which are custom commands; `custom` means any
other custom command; `copy` means `cmake -E copy`; `phony` is Ninja phony edges
(target aliases, order-only groups, `.update` targets).

| phase | archive | compile | copy | custom | install | link | pch | phony | symlink | total |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 libHalide core |  | 188 |  |  |  | 1 |  | 4 | 1 | 194 |
| 2 generated sources, host tools |  | 8 |  | 6 |  | 3 |  | 9 |  | 26 |
| 3 runtime / initmod |  | 846 | 15 | 861 |  |  |  | 4 |  | 1726 |
| 4 tools, autoschedulers | 8 | 37 |  | 9 |  | 13 |  | 84 |  | 151 |
| 5 serialization |  | 2 |  | 1 |  |  |  | 3 |  | 6 |
| 6 tests (sample) | 46 | 57 |  | 47 |  | 46 | 1 | 335 |  | 532 |
| 7 variants | — | — | — | — | — | — | — | — | — | (see below) |
| 8 install |  |  |  |  | 33 |  |  | 44 |  | 77 |
| **all** | 54 | 1137 | 15 | 924 | 33 | 63 | 1 | 483 | 1 | 2711 |

| excluded because | edges |
|---|---:|
| test not in the sample | 4633 |
| python bindings (out of scope) | 1137 |
| tutorials (not in the scope list) | 279 |
| CMake housekeeping (cpack/ctest/ccmake/rebuild_cache/help/clean) | 252 |
| install edge of an out-of-scope directory | 42 |
| util/ (HalideTraceViz/Dump; not in the scope list) | 27 |
| LLVM-exported utility target (empty phony) | 6 |
| phony edge with no CMake target | 4 |
| CMake regeneration edge (compared separately) | 1 |

**Phase 7 (variants)** has no edges of its own. It is the *difference* between the
variant baselines, measured with `compare.py cmake-release.jsonl cmake-<variant>.jsonl`:

| variant vs release | match | argv differs | only release | only variant | what differs |
|---|---:|---:|---:|---:|---|
| debug | 1918 | 793 | 0 | 0 | `-O3 -DNDEBUG` becomes `-g` on every C++ compile and link; tests differ only by `-g` (see WipeStandardFlags) |
| asan | 1918 | 793 | 0 | 0 | `+-fsanitize=address` on every C++ compile and link |
| x86_64 | 1866 | 845 | 0 | 0 | `-arch arm64` becomes `-arch x86_64`; generator `target=` strings |
| universal2 (#9469) | 1815 | 849 | 47 | 411 | `+-arch x86_64`; per-slice `-x86-64-macos` generator runs, runtimes and archives; lipo dummy-symbol TUs |

The same edges match in every variant: the 408 `clang-22 -emit-llvm` runtime compiles,
the 423 `llvm-as` runs, the 438 + 5 `binary2cpp` runs, flatc and every install and
phony edge. The runtime bitcode is compiled for the *Halide target*, not with the
CMake configuration, so none of the host-configuration knobs reach it. This is the
exec/target split of the paper, visible directly in the ground truth.

### Linux scenarios (M3 phases 7b and 7c)

These baselines are recorded by `../docker/` (see its [README](../docker/README.md)). They
use the same pin, scope and sample.
- `cmake-linux-gcc-release.jsonl` (7b) is configured and built inside the Linux image, with GCC 13 and apt LLVM 22.
- `cmake-cross-linux-aarch64.jsonl` (7c) is the *target* tree of the macOS → aarch64-linux-gnu cross build. It has 2842 in-scope edges.

| pair | match | argv differs | only first | only second | what differs |
|---|---:|---:|---:|---:|---|
| release vs linux-gcc-release | 1853 | 854 | 4 | 4 | GCC flags (no `-arch arm64`; `-ldl`, `--push-state/--whole-archive/--pop-state` instead of `-force_load`, `-Wl,--version-script`); `.dylib` → `.so` + soname links (the 4 + 4); cmake from `/usr/local/lib/python3.12/dist-packages/cmake` instead of `$HOMEBREW`; generator `target=arm-64-linux…` |
| release vs cross-linux-aarch64 | 499 | 2145 | 67 | 198 | as above, plus the rows below |
| linux-gcc-release vs cross-linux-aarch64 | 451 | 2195 | 65 | 196 | Homebrew clang `--target=aarch64-linux-gnu --sysroot=$SYSROOT` + `--ld-path` lld instead of GCC; every build-time tool run prefixed by the `host-twin.py` emulator |

The rows specific to the cross tree:
- **Only in cross.**
  - 194 `clang-scan-deps` `.ddi` edges plus `CXX.dd`: C++20 module scanning for libHalide. Oddity 16's `cxx_std_20` from wabt meets a compiler that CMake can scan with.
  - The clang `cmake_pch.hxx.pch`, where GCC has `.gch`.
- **Only in 7b.**
  - The 13 test generators, compiles and links, and their stub headers. In the cross tree they are imported from the macOS host tree.
  - One tutorial edge.
- **Kind change.** The 408 runtime `clang-22`/`llvm-as` compiles are `custom` in the cross tree, because argv[0] is the emulator.

## In-scope CMake targets by phase

- **1 libHalide core:** `Halide`, meaning its 188 C++ objects,
  `libHalide.22.0.0.dylib` and the soname symlinks.
- **2 generated sources and host tools:** `binary2cpp`, `build_halide_h`, and
  `regexp_replace` (built by `all`, but no in-scope command runs it). Plus the
  `Halide` edges that generate sources: `Halide.h`, the 2 `c_template.*` and 3
  `html_template.*` `binary2cpp` outputs, and their compiles.
- **3 runtime / initmod:** `Halide_initmod`, an OBJECT library of 438 generated
  `_initmod_*.cpp` files. They come from 408 bitcode modules (each runtime module per
  word size and debug/non-debug), 15 hand-written `.ll` files and 15 inlined
  headers/sources.
- **4 tools and autoschedulers:**
  - `Halide_GenGen`, `Halide_GenRT` (gengen), `get_host_target`;
  - the plugins `Halide_Adams2019`, `Halide_Anderson2021`, `Halide_Li2018`,
    `Halide_Mullapudi2016` and `Halide_ASLog`;
  - the self-hosted cost models: `adams2019_cost_model{,.generator,.runtime}` and
    `anderson2021_cost_model{,.generator,.runtime}`, plus
    `*_weights_obj`, `*_retrain_cost_model`, `*_train_cost_model`,
    `*_weightsdir_to_weightsfile` and `featurization_to_sample`.
  - `Halide_RunGenMain` is an INTERFACE library, so its `RunGenMain.cpp` is compiled
    into each consumer (in the sample, `adams2019_demo_apps_autoscheduler`, phase 6).
- **5 serialization:** the `Halide` edges for `halide_ir.fbs.h` (flatc),
  `Serialization.cpp` and `Deserialization.cpp`.
- **6 tests:** 93 targets. They are the 32 sampled ctest executables plus everything
  they depend on:
  - `_test_internal` (the PCH) and `Halide_terminate_handler`;
  - for each `add_halide_library`, its `<lib>`, `<lib>.generator` and `<lib>.runtime`
    targets (`alias*`, `autograd*`, `cxx_mangling*`, `example*`, `metadata_tester*`,
    `multitarget*`, `rniso_*`, `stubuser*`, `user_context*`, `adams2019_demo*`,
    `configure.generator`, `stubtest.generator`).
- **8 install:** the `install`, `install/local` and `install/strip` edges of `.`,
  `src`, `src/*`, `tools` and `packaging`. The installed tree is captured separately in
  `cmake-release-install.jsonl`: 157 files, 11 of them Python and excluded. Each Mach-O
  records its archs, install name, linked dylibs and rpaths.

## Custom commands and host tools

Every in-scope custom command, grouped by its main program. "Built in this graph"
means the program is the output of a link edge in the same graph. Such a tool **must
be built for, and run on, the build machine**, whatever the target configuration is:
it is an *exec-configuration* use. "Imported" tools come from outside the graph. The
"also runs" column lists other built artifacts the command loads at run time, from
`runs_built` in the records.

| phase | tool | origin | edges | what it does | also runs |
|---|---|---|---:|---|---|
| 2 | `binary2cpp` | built | 5 | `binary2cpp <sym> < file > out.cpp`: embeds the C-backend prologue/vectors templates and the StmtToHTML css/js/html as C arrays in libHalide | |
| 2 | `build_halide_h` | built | 1 | `make_directory && build_halide_h LICENSE <175 headers> > Halide.h.tmp && copy_if_different && rm` builds the single-header `include/Halide.h` | |
| 3 | `clang-22` | imported (LLVM) | 408 | `clang -MJ … -emit-llvm -S -MD -MF x.d -o x.ll runtime/*.cpp && cmake -E cmake_transform_depfile …` compiles the runtime to LLVM IR for each bitness/debug variant (kind `compile`) | |
| 3 | `cmake -E copy` | imported (CMake) | 15 | copies hand-written `runtime/*.ll` to `*.ll.transformed.ll` (kind `copy`) | |
| 3 | `llvm-as` | imported (LLVM) | 423 | `.ll` becomes `.bc` (408 compiled + 15 copied) | |
| 3 | `binary2cpp` | built | 438 | `.bc` (and 15 inlined headers/sources) becomes `_initmod_*.cpp`, compiled into `Halide_initmod` | |
| 4 | `adams2019_cost_model.generator`, `anderson2021_cost_model.generator` | built | 2 + 2 | **self-hosting:** a Halide generator, linked against the just-built libHalide, AOT-compiles the autoscheduler's own cost model (the `.h`/`.o` and its runtime) | libHalide |
| 4 | `binary2cpp` | built | 2 | embeds `baseline.weights` for adams2019/anderson2021 (`*_weights_obj`) | |
| 4 | `gengen` | built | 3 | `gengen -r …` builds standalone runtimes (`*.runtime.o`) for the cost models and `Halide_GenRT` | |
| 5 | `flatc` | imported (Homebrew) | 1 | `flatc --cpp --cpp-std C++17 … --filename-suffix .fbs -o include/flatc src/halide_ir.fbs` generates `halide_ir.fbs.h` | |
| 6 | 13 test generators, e.g. `alias.generator` | built | 27 | the `add_halide_library` generator run: `<gen> -n … -d 0 -g … -f … -e c_header,object,… -o dir/ target=…` | Adams2019/Li2018/Mullapudi2016 `.so` via `-p` (alias ×5, autograd ×2, stubuser ×2, adams2019_demo ×1) |
| 6 | `gengen` | built | 20 | per-library runtime (`gengen -r <lib>.runtime … target=…`) | |
| 8 | `cmake -P cmake_install.cmake` | imported (CMake) | 33 | per-directory install scripts (`install`, `install/local`, `install/strip`) | |

In total there are **1380 in-scope custom-command edges**: 924 of kind `custom`, 408
`compile` (clang-22), 15 `copy` and 33 `install`.
- **host tools built in the graph:** `binary2cpp`, `build_halide_h`, `gengen`, 2 cost-model
  generators and 13 test generators. They run 500 times, and 10 of those runs also load
  a built autoscheduler plugin;
- **imported:** `clang-22` and `llvm-as` from `$LLVM`, `flatc` from Homebrew, and
  `cmake` itself.
- The generators also load the just-built `libHalide.22.dylib` via `@rpath`, i.e.
  `-Wl,-rpath,$BUILD/src`. So every host tool except `binary2cpp` and `build_halide_h`
  pulls libHalide into the exec configuration.

## Test sample

There are 20 correctness tests and 12 generator-style ctests: 11 in
`test/generator` and 1 autoscheduler demo. These are ctest names, and each is also
the executable target name. Results are from the release tree: `ctest -R` with the
regex from `normalize.py test-regex`.

| ctest name | why it is in the sample | release |
|---|---|---|
| `correctness_bounds` | the plain `tests()` shape: one .cpp, PCH-reusing, JIT only | pass |
| `correctness_plain_c_includes` | the one **C** source (`.c`): C compiler, no PCH, no `-std=c++17` | pass |
| `correctness_image_io` | links `Halide::ImageIO`, i.e. imported libpng/libjpeg from Homebrew (find_package results) | pass |
| `correctness_gpu_allocation_cache` | links `Halide::ThreadPool`; a GPU test that skips via ctest's `SKIP_REGULAR_EXPRESSION` on a host without a GPU target | skipped |
| `correctness_spirv_ir` | internal header: `-I$SRC/src`, the vendored SPIRV-Headers `find_package`, `-DWITH_SPIRV` | pass |
| `correctness_x86_cpu_detect` | cross-target feature logic on an arm64 host | pass |
| `correctness_extern_stage` | `ENABLE_EXPORTS` (JIT calls back into the test executable) | pass |
| `correctness_serialization` | `WITH_SERIALIZATION` → `-DTEST_WITH_SERIALIZATION` | pass |
| `correctness_generator_cache` | also serialization-conditional; exercises the generator cache | pass |
| `correctness_auto_schedule_no_parallel` | test ARG is `$<TARGET_FILE:Halide::Mullapudi2016>`, a run-time dependency on a built plugin | pass |
| `correctness_simd_op_check_arm` | big multithreaded test (label `multithreaded`), heavy ARM codegen | pass |
| `correctness_codegen_c` | the C backend at JIT level (uses the `c_template` embeds) | pass |
| `correctness_stmt_to_html` | uses the `html_template` embeds (binary2cpp, phase 2) | pass |
| `correctness_compile_to_multitarget` | multitarget output from `compile_to_multitarget` | pass |
| `correctness_embed_bitcode` | embeds bitcode in objects | pass |
| `correctness_cross_compilation` | compiles for every enabled LLVM backend, validating `WITH_<backend>` (90 s, the slowest) | pass |
| `correctness_callable_generator` | in-process generator registration; `ENABLE_EXPORTS` | pass |
| `correctness_custom_auto_scheduler` | registers an autoscheduler in-process (the plugin API without a `.so`) | pass |
| `correctness_bad_schedule` | a former `test/error` test (converted at `35b7aa604e`); expects a user error | pass |
| `correctness_multi_gpu_gpu_multi_device` | a different group (`correctness_multi_gpu`), giving a non-standard target/ctest name prefix | skipped |
| `generator_aot_multitarget_0`, `_1` | **multitarget**: `TARGETS cmake-no_bounds_query cmake` gives `target=a,b`, per-target `.o` plus `_wrapper.o`. One generator run feeds two test executables, which pick the path via ctest `ENVIRONMENT` | pass |
| `generator_aotcpp_example` | the **C backend**: `example_cpp` is generated with `-e c_header,c_source,…` and the `.cpp` is compiled as a real (non-imported) STATIC library | pass |
| `generator_aot_runtime_prefixes_iso` | **separate runtime** (`USE_RUNTIME`) with runtime-name prefixes: 3 libraries × 2 runtimes | pass |
| `generator_aot_cxx_mangling_define_extern` | **external** code: `EXTERNS` links an extern-defining library into the AOT pipeline, plus C++ name mangling. There is no truly imported external generator in-tree; see "Unsure" in the README | pass |
| `generator_aot_user_context` | **user context** (`target=host-user_context-no_runtime`) | pass |
| `generator_aot_stubuser` | **generator stubs**: `stubtest.generator` emits a stub consumed by `stubuser.generator`; the build-time chain is two generators deep | pass |
| `generator_aot_alias` | **autoscheduler plugins via `-p`**: `alias_Adams2019`, `alias_Li2018`, `alias_Mullapudi2016`; one generator reused `FROM` five libraries | pass |
| `generator_aot_autograd` | `GRADIENT_DESCENT` plus an `AUTOSCHEDULER`; two libraries from one generator | pass |
| `generator_aot_metadata_tester` | configure-time `TARGETS` resolution (`target=arm-64-macos-no_bounds_query-no_runtime,arm-64-macos-no_runtime`), generator params on the command line (`input.type=uint8 input.dim=3 …`), and a user-context variant (`metadata_tester_ucon`) | pass |
| `rungen_test` | RunGen registration (`REGISTRATION` output); calls `RunGen` directly rather than RunGenMain | pass |
| `adams2019_demo_apps_autoscheduler` | **RunGenMain** (INTERFACE source) plus the Adams2019 plugin and its self-hosted cost model at build time | pass |

Results with the previous pin plus #9473 (the wabt executor on), all 32 sampled tests, `ctest -j16` (Release at `f370931c79`: the same 30 passed, 2 skipped):
- **Release: 30 passed, 2 skipped (no GPU target), 0 failed** (76 s wall).
- Debug: 30 passed, 2 skipped (86 s).
  - The first baseline recorded 1025 s for a 31-test Debug run. I can't reproduce or
    explain that number now.
- ASan: 24 passed, 2 skipped and **6 failed** (96 s).
  - The failures are the same tests with the same causes as before the wabt change. See
    [Oddities §12](#oddities).
- The results match the `Halide_WASM_BACKEND=OFF` baseline test for test. No sampled
  test runs the wasm JIT by default. As a spot check outside ctest, I ran
  `correctness_bounds`, `correctness_vector_math` and `correctness_extern_stage` from
  the Release build with `HL_JIT_TARGET=wasm-32-wasmrt`.
  - All three print `Success!`.
  - `HL_DEBUG_CODEGEN=2` shows them JIT-compiling for `wasm-32-wasmrt-jit-user_context`,
    i.e. through the wabt executor.

## Oddities

These are CMake behaviours the buildlog spec must reproduce, or deliberately not
reproduce (with the comparison configured accordingly). The ones expected to be
hardest come first.

1. **Self-hosting generators need the just-built compiler on the exec side.** Each
   `add_halide_library` is a generator executable linked against the just-built
   `libHalide.22.dylib` (with `-Xlinker -force_load libHalide_GenGen.a` and
   `-Wl,-rpath,$BUILD/src`). Its output is linked into the target.
   - The adams2019/anderson2021 cost models do this *inside the compiler's own
     build*: the autoscheduler plugin `libautoschedule_adams2019.so` contains code
     generated by a program that links libHalide.
   - Generators can also load *other* built plugins at run time (`-p a.so,b.so`,
     comma-joined in one argv token).
   - CMake models this with ordinary target deps; nothing tells it these are
     exec-configuration uses. In the universal2 build this breaks (§5).
2. **The runtime pipeline** (phase 3, 1726 edges): `clang-22 -emit-llvm` (with a
   `DEPFILE` that CMake rewrites through
   `cmake -E cmake_transform_depfile … CMakeFiles/d/<sha256 of the abs path>.d`),
   then `llvm-as`, then `binary2cpp < x.bc > _initmod_x.cpp`, then a C++ compile.
   - The 15 hand-written `.ll` files are `cmake -E copy`'d to `*.transformed.ll` first.
   - `$<SHELL_PATH:>` wraps every path. It is the identity on POSIX, but the command
     needs a shell for `<`/`>`.
   - Custom commands are emitted per directory rather than per target, so
     `build.ninja`'s section comments don't identify the target. The normalizer
     attributes edges through the File API instead.
   - It uses the *imported* LLVM `clang`/`llvm-as` (Halide_LLVM's), not the host C++
     compiler. The bitcode is target-, not host-configured: it is byte-identical
     across Release, Debug, ASan and x86_64.
3. **`add_halide_library` expansion** (`HalideGeneratorHelpers.cmake`, 1118 lines):
   - one generator run emits a *set* of files that depends on `-e`
     (`c_header,object,function_info_header,registration,c_source,…`) and on the
     number of targets in `target=`;
   - a separate runtime is built by `gengen -r` (or `<gen> -r`) unless
     `USE_RUNTIME`;
   - there is a `<lib>.update` phony per library;
   - the library is a `STATIC IMPORTED` target over the generator's own `.a`
     (`TYPE static_library`, the default), or a real CMake `STATIC` library when the
     generator emits `object`/`c_source` (multitarget, C backend). Runtimes are always
     `STATIC IMPORTED`, with a `<rt>.update` custom target;
   - `target=host-no_runtime` is resolved *by the generator at build time*, on the
     build machine, while `TARGETS cmake` is resolved at configure time to
     `Halide_CMAKE_TARGET`;
   - generator executables get Halide's default warning flags unless
     `NO_DEFAULT_FLAGS`/`Halide_NO_DEFAULT_FLAGS` is set.
4. **PCH reuse:**
   - `_test_internal` compiles `cmake_pch.hxx.pch` (`-Xclang -emit-pch -x c++-header`);
   - every C++ test compiles with `-Winvalid-pch -Xclang -include-pch -Xclang <pch>
     -Xclang -include -Xclang cmake_pch.hxx` (`PRECOMPILE_HEADERS REUSE_FROM`);
   - `plain_c_includes.c` does not.

   The PCH is target-private state that another target consumes. A buildlog spec may
   instead drop PCH and set the comparison to ignore those flags.
5. **universal2 host tools.** With `CMAKE_OSX_ARCHITECTURES=arm64;x86_64`:
   - At `35b7aa604e`, **generate fails**: 170 × "Attempt to add a custom rule to
     output `….h.rule` which already has a custom rule". Two per-slice rules claim one
     header.
   - With PR #9469 (b085c6e787, merged in `f370931c79`, the current pin), configure succeeds.
     `add_halide_generator` targets become host-only (`-arch arm64`), and each
     library gets per-slice `-x86-64-macos` generator runs and archives plus a
     `Halide_lipo_dummy_symbol_*.cpp`.
   - But the host tools that don't go through `add_halide_generator` stay fat:
     `binary2cpp`, `build_halide_h`, `regexp_replace`, both cost-model generators
     (plain `add_executable`) and libHalide itself. So the exec/target split is
     applied by hand, per tool, and this commit misses some.
   - That is the paper's "exec configuration inferred from use" argument, in the
     ground truth.
6. **WipeStandardFlags:** `HalideTestHelpers.cmake` strips `-DNDEBUG` and `-O*`
   from `CMAKE_<LANG>_FLAGS_<CONFIG>` for everything under `test/`.
   - Test objects therefore get no optimization flags in Release, and Release vs
     Debug differs there only by `-g`.
   - The generated code's optimization comes from the generator, not the C++ flags.
   - It is directory-scoped mutation of a global variable, which buildlog will
     express as a per-target flag set.
7. **Visibility and exports differ by target:**
   - tools and tests compile with `-fvisibility=hidden -fvisibility-inlines-hidden`;
   - libHalide does not. It controls its ABI at link time with
     `-exported_symbols_list $SRC/src/exported_symbols.osx`, chosen by the configure
     probe `LINKER_HAS_FLAG_EXPORTED_SYMBOLS_LIST` (the GNU `--version-script` probe
     fails on macOS);
   - tools, tests and generators use `-std=c++17`, but libHalide's own objects compile
     with `-std=c++20`. That standard comes from wabt's imported target, not from
     Halide (see 16). The runtime also gets `clang`'s freestanding flags.
8. **Shared library naming:**
   - the link output is `libHalide.22.0.0.dylib` with
     `-install_name @rpath/libHalide.22.dylib`;
   - then *one* `CMAKE_SYMLINK_LIBRARY` edge with two outputs creates
     `libHalide.22.dylib` and `libHalide.dylib`;
   - consumers link the real file but order-only-depend on the symlink.
9. **rpath handling:**
   - in the build tree, `-Wl,-rpath,$BUILD/src` on every executable and plugin;
   - in the install tree, CMake rewrites these at install time to `@loader_path` and
     `@loader_path/../lib` (from the manifest);
   - the installed `libHalide` still links `$LLVM/lib/libLLVM.dylib`,
     `$HOMEBREW/opt/lld@22/lib/liblld{Common,Wasm}.dylib` and
     `$HOMEBREW/opt/openssl@3/lib/libcrypto.3.dylib` (via wabt) by absolute install
     name, so it is not relocatable across machines;
   - install/strip edges re-run `cmake -P`.
10. **`copy_if_different` and `restat`:**
    - `Halide.h` is generated to `.tmp` and then `cmake -E copy_if_different`'d, so
      unchanged headers don't cause rebuilds;
    - every custom-command edge carries `restat = 1` (1946 in the full graph);
    - buildlog needs the same (or a content-hash) behaviour to match rebuild
      counts.
11. **Depfiles:**
    - compiles use `-MD -MT <obj> -MF <obj>.d` with `deps = gcc`;
    - the custom-command `DEPFILE`s are transformed and hashed (§2);
    - there are **no response files** (`rspfile` count 0) on macOS, even for the
      175-header `build_halide_h` line and the 438-object `Halide_initmod`;
    - the recorded `ninja -t deps` dump is 163 MB.
12. **ASan runtime coupling.** With `-fsanitize=address` on Apple clang 21, Halide's
    JIT (LLVM 22) instruments JIT'd code with ASan ABI `v8`, but the process
    carries Apple's `___asan_version_mismatch_check_apple_clang_2100`.
    - Five JIT tests fail to materialize symbols: bounds, image_io, extern_stage,
      serialization and callable_generator.
    - `simd_op_check_arm` dies with a stack overflow (`___chkstk_darwin` BUS) in a
      worker thread.
    - Python generators fail at build time: an uninstrumented `python3` dlopens an
      ASan extension.
    - The sanitizer runtime must come from the same LLVM as the JIT, a constraint
      between the host compiler and a library dependency that CMake cannot express.
      Upstream's `toolchain.linux-x64-asan.cmake` says as much: "we must use our
      LLVM version of clang for *all* compilation".
13. **Order-only phonies:**
    - `cmake_object_order_depends_target_<T>` and the per-target phony aliases
      (`$BUILD/<T>`, and `$BUILD/<artifact name>`) are CMake bookkeeping (phony
      counts);
    - the path map can ignore them, but they fix ordering (generated headers before
      compiles);
    - custom-command outputs are sometimes listed twice, relative and absolute; the
      normalizer de-duplicates them.
14. **Build-time consumers of the Python interpreter and platform probes** (out of
    scope but in the graph): `Python_EXECUTABLE`, `$<TARGET_FILE:Python::Interpreter>`
    in generator commands, and `cmake -E env PYTHONPATH=… --` wrappers.
15. **`ENABLE_EXPORTS`** on 44 correctness tests adds **nothing** to macOS link
    lines. It is a semantic property that is empty on this platform (`-rdynamic` on
    Linux). `-force_load` of `libHalide_GenGen.a` into every generator is the
    macOS spelling of `--whole-archive`.
16. **An imported package's usage requirements reshape libHalide.**
    `src/CMakeLists.txt` does `target_link_libraries(Halide PRIVATE wabt::wabt)` (the
    default `Halide_WASM_BACKEND=wabt`). Homebrew's `wabt-targets.cmake` declares
    `INTERFACE_COMPILE_FEATURES cxx_std_20` and `INTERFACE_LINK_LIBRARIES
    OpenSSL::Crypto`, and `wabt-config.cmake` does `find_dependency(OpenSSL)`. The
    result in every one of libHalide's 194 compiles:
    - `-std=c++17` becomes `-std=c++20`;
    - `-isystem $HOMEBREW/Cellar/openssl@3/3.6.4/include` is added. Halide's sources
      include nothing from OpenSSL;
    - Halide's own `-DWITH_WABT` is added.

    The link gains `$HOMEBREW/lib/libwabt.a` and
    `$HOMEBREW/Cellar/openssl@3/3.6.4/lib/libcrypto.dylib`. So a third-party
    package's metadata, found first-match, silently raises the language standard of
    the consumer and pins a version-specific Cellar path: a `brew upgrade openssl`
    changes the graph. A buildlog spec must decide whether to reproduce this
    propagation (feature and include usage requirements of an imported target) or
    to pin the standard explicitly and treat the OpenSSL include as noise.

## Deviations

- **The pin is Halide main `4e272cbfcc`** (repinned 2026-10-13; it was `f370931c79` from 2026-09-28, and the notes below on identical graphs are for that repin; the 2026-10-13 repin adds `LowerStructTypes.cpp` and the new tests and generators (release baseline: 238 lines added, 29 removed), see README, "Repin to 4e272cbfcc"). It contains
  PR #9473 (`reinterpret_cast<const uint8_t *>` in `src/WasmExecutor.cpp`, so the
  default `Halide_WASM_BACKEND=wabt` builds against wabt 1.0.42, whose
  `ReadBinaryInterp` takes `const uint8_t*`) and PR #9469 (universal2).
  - The previous pin was `35b7aa604e` plus #9473 applied with `patch -p1` into
    `build/src-9473`, and a `universal2-9469` variant that exported `b085c6e787`
    with #9473 on top. At `f370931c79` the release, debug, ASan, x86_64 and
    universal2 graphs and the install manifest normalize byte-identically (universal2
    to the old `universal2-9469` file), so every number in this file still holds.
  - The first version of this baseline (`38c5d3c`/`70fe6f8`) used
    `-DHalide_WASM_BACKEND=OFF` instead. Enabling wabt changes 195 in-scope edges in
    every variant and adds none: `WasmExecutor.cpp.o` was already compiled with the
    backend off, but without `WITH_WABT`. See oddity 16 for the changes.
  - `run-cmake.sh` `git archive`s the pin into `build/src-4e272cbfcc`. No Halide
    worktree is modified.
- **Normalizer changes for the Linux baselines (M3 phase 7b).** Both are pure
  normalization fixes. Re-normalizing the raw release, debug, ASan, universal2 and x86_64
  dumps gives byte-identical files.
  - `Roots` no longer re-matches a short root inside an already-matched path. Before
    the fix, the container's `/src` also matched inside `/build/…/src/…`. The regex now
    captures the remainder of a matched root path and keeps it literal.
  - `classify` treats GCC's precompiled-header compiles (`-x c++-header`/`-x c-header`)
    as `pch`, like clang's `-emit-pch`.
  - Tests cover both. The self-test runs on the release, linux-gcc-release and
    cross-linux-aarch64 baselines.
- Scope decisions:
  - `util/` (HalideTraceViz/Dump) and `tutorial/` are excluded because they are not
    in the brief's scope list;
  - `regexp_replace` is included because `all` builds it in `tools/`, although no
    in-scope command uses it.
