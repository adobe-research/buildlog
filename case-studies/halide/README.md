<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: building Halide with buildlog

The case study builds Halide with buildlog and compares the result against
Halide's own CMake build.
**Phase 0**, this directory's `baseline/`, holds the CMake
ground truth and the tools that every later phase is measured with.

## Layout

```
case-studies/halide/
  README.md                this file
  baseline/
    run-cmake.sh           configure/build/test/dump/normalize driver for the CMake variants
    scope.toml             phase rules, test sample, exclusions (first match wins)
    normalize.py           Ninja graph (+ File API) -> canonical JSONL records
    compare.py             compare two JSONL graphs per phase and kind
    metrics.py             CMake description size, implicit choices, scope tables
    scope.md               scope map: phases, host-tool table, test sample, oddities
    metrics.md             timings, CMake size, implicit choices, machine
    cmake-release.jsonl    normalized baselines (checked in, placeholder paths only)
    cmake-debug.jsonl
    cmake-asan.jsonl
    cmake-x86_64.jsonl         configure-only graphs (the builds fail, see below)
    cmake-universal2.jsonl
    cmake-release-install.jsonl  install tree manifest (Mach-O archs, install names, rpaths)
    cmake-linux-gcc-release.jsonl    7b: Linux/GCC in Docker (see docker/)
    cmake-cross-linux-aarch64.jsonl  7c: macOS -> aarch64-linux-gnu, target tree
    tests/                 pytest tests with a small fake Ninja/File API fixture
    raw/                   gitignored: build.ninja, ninja -t commands/targets/deps,
                           compile_commands.json, File API reply, logs, ctest results
  docker/                  Phases 7b/7c: the Linux image, sysroot export, cross toolchain and
                           workarounds, run-cmake-{linux,cross}.sh; README.md has the 7c CMake effort ledger
  phase1/
    llvm-discovery-assessment.md, config_values.py, option1_cmake_probe.py, ...
                           the LLVM discovery assessment and its prototypes
    compare_libhalide.py   normalize buildlog's libHalide graph, compare with the baseline
    pathmap-libhalide.toml the path map it uses (output layout renames, -MT)
    oracle_check.py        discovery facts vs the Option 1 CMake probe (llvm@22, llvm@21)
  packages/share/buildlog/
    halide-build/          Halide's LLVM policy (thin); phase 2's binary2cpp/build_halide_h rules;
                           phase 3's runtime (halide_build_runtime.dl); phase 5's flatc
                           (halide_build_gen.dl); phase 4's generators and runtimes, grown in
                           phase 6 to add_halide_library in full (halide_build_generator.dl)
  llvm-install/            what LLVM (Homebrew, apt.llvm.org) would install for buildlog: tier-2 packages
                           LLVM, Clang, LLD (manifests importing the install's CMake exports, facts, rules)
  libhalide/               the phase 1-6 project: libHalide against llvm@22, its host tools, its runtime,
                           its serialization header; tools/ and the autoschedulers (autoschedulers.toml);
                           the sampled tests (tests.toml), run by `buildlog test`
```

## Pinned inputs

- **Halide main at `4e272cbfcc`** (2026-10-13, the merge of #9548). It contains PR #9473 (the wabt 1.0.42 fix for the default wabt WASM
  executor), PR #9469 (`add_halide_library` for universal2) and PR #9548 (the `getauxval` include order in `src/Target.cpp`).
  - **Repinned (2026-10-13)** from `f370931c79` (17 upstream commits). Fixed upstream in #9548 (issue #9480); the workaround
    (`-include sys/auxv.h` for `Target.cpp` on Linux/clang) was removed at this pin, from `libhalide/rules.dl` and from
    the CMake cross baseline. Upstream changes adapted: see [Repin to 4e272cbfcc](#repin-to-4e272cbfcc).
  - **Earlier repin** (2026-09-28) from `35b7aa604e` plus a patched-in #9473 (and #9469 for a separate `universal2-9469` variant). All
    five variants' in-scope graphs are identical at the new pin (2711 records, and 3075 for universal2), and so is the
    install manifest; the normalized files are byte-identical, and the universal2 file is the old `universal2-9469`
    one under its new name. Main differs from the old pin only in `HalideGeneratorHelpers.cmake` and
    `WasmExecutor.cpp`. Release was rebuilt and tested at the new pin; debug, ASan, x86_64 and universal2 were
    re-configured only (their build outcomes below are from the old pin).
  - The git objects come from the submodule `case-studies/halide/upstream` (https://github.com/halide/Halide.git, pinned to the pin). The `HALIDE_SRC` variable overrides it. `HALIDE_BUILD_ROOT` (default `build/case-halide`) holds the `cmake-<variant>` trees and the export.
  - `run-cmake.sh` never builds that worktree. It `git archive`s the pin into `$HALIDE_BUILD_ROOT/src-4e272cbfcc` (it can still
    apply patch commits on top, with `patch`).
  - A `.buildlog-export` marker records the base and patch commits, and an export that doesn't match is rebuilt.
  - Build trees go to `$HALIDE_BUILD_ROOT/cmake-<variant>`. Everything under `build/` is covered by Halide's `build*/` ignore rule.
- **Configuration:** the `macOS` preset with the following changes.
  - `-DHalide_LLVM_ROOT=$LLVM`, where `$LLVM` is Homebrew's `llvm@22` keg (22.1.8). The `LLVM_ROOT` variable overrides it.
  - `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`.
  - Nothing else: in particular `Halide_WASM_BACKEND` stays at its default, `wabt`, using Homebrew's wabt 1.0.42.
- **Tools:** CMake 4.4.3, Ninja 1.13.2, and AppleClang 21 as the C/C++ compiler. See `baseline/metrics.md`.

## Rerunning the baseline

```sh
cd case-studies/halide/baseline
./run-cmake.sh release                   # configure build dump test normalize
./run-cmake.sh release install metrics   # install tree manifest; timings + size + implicit
./run-cmake.sh debug configure build dump test normalize
./run-cmake.sh asan  configure build dump test normalize
./run-cmake.sh x86_64 configure dump normalize
./run-cmake.sh universal2 configure dump normalize
```

The steps do the following:
- `configure` writes a File API query (codemodel-v2, toolchains, cmakeFiles, cache).
- `build` runs `ninja -j$JOBS $NINJA_TARGETS`, which defaults to all cores and `all`.
- `dump` copies `build.ninja`, `compile_commands.json` and the File API reply into `raw/<variant>/`, and records the
  `ninja -t commands|targets|deps` output there.
- `test` runs the sampled tests with `ctest -R <regex from scope.toml>`.
- `normalize` writes `cmake-<variant>.jsonl`.

Variant graphs are compared against release with
`uv run python compare.py cmake-release.jsonl cmake-<variant>.jsonl`; the results are in scope.md.

After renormalizing, check that no machine paths leaked into the committed files:

```sh
grep -l -e /Users/ -e /opt/homebrew -e "$HOME" -e "$(xcrun --show-sdk-path)" *.jsonl
```

## Variant outcomes

| variant | configure | build | tests (sample) |
|---|---|---|---|
| Release | yes | full `all`, 113 s at `-j16` (116 s at the new pin) | 30 pass, 2 skipped (GPU); the same at the new pin |
| Debug | yes | full `all`, 134 s | 30 pass, 2 skipped |
| ASan (`-fsanitize=address`, AppleClang, `WITH_TEST_FUZZ=OFF`) | yes | all except 16 python_bindings edges (uninstrumented python); 125 s with `-k 0` | 24 pass, 2 skipped, 6 fail: 5 on the JIT (LLVM 22) vs Apple ASan runtime version check, 1 stack overflow |
| universal2 | yes (at the old pin only with #9469; without it, generate failed with 170 "already has a custom rule" errors in `HalideGeneratorHelpers.cmake`) | **no**: fails at libHalide link; Homebrew LLVM, lld, wabt and OpenSSL are arm64-only (old pin with #9469) | — |
| x86_64 cross | yes | **no**: fails at libHalide link, same reason | — |
| Linux/GCC 13 in Docker (7b, `docker/run-cmake-linux.sh`) | yes | full `all`, 136 s at `-j16` in the container | 30 pass, 2 skipped |
| macOS → aarch64-linux-gnu (7c, `docker/run-cmake-cross.sh`) | yes, 2 trees and 6 workarounds (7 before the pin had #9548; docker/README.md) | full `all`, 87 s (plus 44 s for the host tree) | 30 pass, 2 skipped, in the container |

A working cross or universal2 build would need x86_64 or fat builds of LLVM, lld, wabt, OpenSSL and flatbuffers. That
was out of time budget. The configure-only graphs still show what CMake decides per architecture;
see scope.md, oddity 5.

## Record format

`normalize.py` writes one JSON object per Ninja build edge, sorted by `id`, with sorted keys.
- `id`: the first output.
- `outputs`: all outputs.
- `inputs`: `explicit`, `implicit` and `order_only` lists.
- `phony`: boolean.
- `rule`: the Ninja rule name.
- `kind`: one of `compile`, `pch`, `link`, `archive`, `custom`, `copy`, `symlink`, `install`, `phony`, `meta`, `regen` or `stamp`.
- `steps`: the command decomposed at `cd … &&`, `&&`, `/bin/sh -c`, `cmake -E env … --` and redirections.
  - Each step has its `argv` (via `shlex`), its `cwd`, and any `stdin`/`stdout` redirections.
  - `main` is the index of the step that does the work.
- `argv` (top level): the original full command, kept when it is more than its one step.
- `tool`, `tool_name`: the main step's program.
- `tool_built`: true if that program is an output of this graph.
- `runs_built`: other built files passed to the main step, such as `-p plugin.so`.
- `target`, `target_type`: the CMake target, from the File API.
- `target_via`: how the target was attributed: `fileapi-objdir`, `fileapi-source`, `fileapi-artifact`, `fileapi-utility`, `fileapi-order`, `ninja-section`, `consumer` or `input`.
- `origin`: `rule`, `custom_command` or `phony`.
- `phase`: 1–8, from `scope.toml`.
- `excluded`: the reason, if the record is out of scope. Excluded records keep only `id`, `outputs`, `kind`, `target`, `dir`, `phase` and `tool_name`.
- Also present: `depfile`, `deps`, `restat`, `pool`, `link_type`, `dir` and `variant`.

Machine paths become placeholders:
- `$SRC` and `$BUILD`;
- `$LLVM`, the LLVM prefix;
- `$SDK`, the macOS SDK;
- `$TOOLCHAIN`, the Xcode toolchain;
- `$HOMEBREW`;
- `$HOME`;
- `$PREFIX`, the install prefix, in the install manifest only;
- `$SYSROOT`, `$HOST_BUILD` and `$DOCKER`, in the 7c cross baseline only (the sysroot, the macOS host tree and `docker/`).

Paths in system locations such as `/usr/bin/c++` are left as they are.

## Using `compare.py` in later phases

A later phase runs buildlog on its spec. It then normalizes the emitted `build.ninja` with the same
tool, and compares against the baseline:

```sh
uv run python baseline/normalize.py normalize --build-ninja BUILD/build.ninja \
    --build BUILD --src "$HALIDE_SRC" --llvm "$LLVM" --scope baseline/scope.toml \
    --variant buildlog-release -o buildlog-release.jsonl
uv run python baseline/compare.py baseline/cmake-release.jsonl buildlog-release.jsonl \
    --map phase1-pathmap.toml --phase 1 --details 20 --json phase1-compare.json
```

- buildlog's Ninja has no File API. The phase tag therefore comes from `scope.toml`'s output, directory and tool
  conditions, or from the path map's rewrite into CMake's layout.
  `normalize.py --target-map` supplies targets for outputs, and `--target-dirs` (phase 4) supplies each target's CMake
  source directory, which the `dir` conditions match.
- The **path map** is TOML. Each `[[map]]` entry has a `side`, and a `from` regex with a `to` replacement. They rewrite buildlog's output
  layout onto CMake's before matching. An example is `baseline/tests/fixtures/pathmap.toml`.
- **Comparison modes** are per kind:
  - `compile` and `pch` compare flags as a multiset of flag units, but keep include-path order.
  - Everything else (link, archive, custom, copy, install) is compared in order.
  - `[modes]` in the map, or `--mode KIND=set|ordered`, overrides this.
  - `[ignore] tokens` drops tokens known to be irrelevant, and `[ignore] flags` drops a flag together with its
    separate argument (phase 1 uses it for `-MT` only).
  - Include order is compared over the search paths both sides have, so an order difference is reported
    even when the flag sets differ too.
- Only the main step is compared by default; `--full-argv` compares the whole command. `--check-inputs` also compares input sets.
- `--fail-on-diff` makes the command usable as a gate.
- `compare.py --self-test cmake-release.jsonl` checks that the tool reports a baseline identical to itself. It also checks that
  it catches a perturbed copy: a dropped record, a changed flag, and a reordered link.

## Tests

`uv run --frozen pytest -q case-studies/halide` runs the normalizer and compare tests against a
small fixture. They don't need a Halide build. One test renormalizes the real `raw/release`
and checks that the result matches the checked-in baseline. It skips if `raw/` is absent.
`tests/test_halide_phase1.py` (in the main suite) tests the phase 1 packages against Homebrew's
llvm@22; it skips without it. `tests/test_halide_phase2.py` and `tests/test_halide_phase3.py` test
phases 2 and 3; they also need the pinned export and the CMake release tree.
`tests/test_halide_phase5.py` tests phase 5; it needs the pinned export and Homebrew's flatbuffers,
and the CMake release tree only for its byte and graph comparisons.
`tests/test_halide_phase4.py` tests phase 4 with the same needs as phase 5 (its comparison needs only
the checked-in baseline); `BUILDLOG_HALIDE_BUILD=1` also builds the graph and loads the plugins.
`tests/test_halide_phase6.py` tests phase 6; it also needs Homebrew's libpng and jpeg-turbo, and
`BUILDLOG_HALIDE_BUILD=1` builds the graph and runs the sampled tests.

## Phase 1: libHalide against LLVM 22

Phase 1 builds libHalide with buildlog: the core sources, their defines, options and include dirs as
in the baseline's Release configuration, linked against Homebrew's llvm@22 (22.1.8), found as the tier-2 packages LLVM, Clang and LLD (`llvm-install/`: manifests that import llvm@22's own CMake
exports, plus a few facts and rules).

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle                                          # libHalide.22.0.0.dylib + symlinks
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --details 5
uv run python case-studies/halide/phase1/oracle_check.py WORKDIR --llvm 22 --llvm 21
uv run --frozen pytest -q tests/test_halide_phase1.py
```

### What is modeled

**LLVM, Clang and LLD** (`llvm-install/`, **[replaces the plugin `buildlog-llvm`]**): tier-2 packages, one
manifest per upstream package as LLVM has one `LLVMConfig.cmake`, `ClangConfig.cmake` and
`LLDConfig.cmake` each. They stand in for what LLVM (or a distro) would install under
`share/buildlog/<Pkg>/`; `llvm-install/README.md` lists the overlay prefixes that put them beside
Homebrew's llvm@21/@22 and lld@21/@22 and the Linux image's `/usr/lib/llvm-22` without writing there.
No Python, no conversion script: an imported file is facts, and a package ships them as its upstream's
exports plus a few more.
- **Find**: `package_search_dir("buildlog", "<overlay>/share/buildlog")` (a user preset). The `buildlog`
  provider finds `LLVM::LLVM` in `LLVM/manifest.toml`, `Clang::clang` in `Clang/`, `LLD::lldWasm` in `LLD/`.
  **Selection is core's**: several candidates for a name are core's `ambiguous package` error until
  the dirs are narrowed or a `package_pin` names one.
- **Imports**: each manifest `imports` its own file, `lib/cmake/llvm/LLVMExports.cmake`,
  `clang/ClangTargets.cmake`, `lld/LLDTargets.cmake`, with the namespace `LLVM`, `Clang`, `LLD`
  (`LLVM::LLVM`, `Clang::clang`, `LLD::lldWasm`). `Clang` and `LLD` `require` `LLVM` at the version of the
  LLVM they were built with (`"==22.1.8"`): a Clang of another version than the bound LLVM is a load error.
- **Package-level facts** (`facts.toml`, the CMake variables): `llvm_version`, `llvm_define`
  (`LLVM_DEFINITIONS`), `llvm_include_dir`, `llvm_enable_rtti`, `llvm_enable_eh`, `llvm_target`
  (`LLVM_TARGETS_TO_BUILD`); `clang_version` for Clang. Per-version files: LLVM 21 adds
  `EXPERIMENTAL_KEY_INSTRUCTIONS`, the Linux image's LLVM adds `_GNU_SOURCE`,
  `_GLIBCXX_USE_CXX11_ABI=1`, and the M68k and Xtensa targets.
- **Rules** (`rules/LLVM.dl`, 31 lines; Clang 2, LLD 4): the facts become `package_version` and
  `package_attr("LLVM", p, "llvm.define" | "llvm.system_include_dir" | "llvm.built_target", v)` for
  halide-build to read; `cxx.rtti` and `cxx.exceptions` build props on every `LLVM::*` library (the
  generic C++ ABI relation then checks consumers); the components `LLVM:<C>` (interface targets) with
  one dependency per linkage (shared, `LLVM::LLVM`; static, the `LLVM<T>{CodeGen,AsmParser,Desc,...}`
  libraries); and an interface target `LLVM:Config` carrying the defines and include dirs, for a
  consumer that wants what `llvm_map_components_to_libnames` and `LLVM_DEFINITIONS` give.
- **Ownership**: the loader refuses a package that states facts about targets outside the namespaces
  its imports own (`tests/test_export_buildlog.py`). Defines are not put on `LLVM::*` (LLVM's CMake
  does not either): consumers read them from the package-level facts.
- **Linkage** is the invoker's pin `package_linkage` (core): llvm@22 offers both `libLLVM.dylib` and
  the component archives, so an unpinned build is core's `package linkage not chosen` error. The static
  path links and runs (an X86-only consumer, `tests/test_halide_phase1.py`); it needs the project to
  bind `zstd` (its CMake export file) and `ZLIB::ZLIB` (a `-lz` target).

**`halide-build`** (`packages/share/buildlog/halide-build/`, 28 lines of rules in `halide_build.dl`, Halide policy only; reads LLVM's `package_attr` facts):
the interface target `halide-build::llvm` carries LLVM's defines minus `_GLIBCXX_ASSERTIONS`, plus
`LLVM_VERSION=<major><minor>` and the `WITH_<backend>` defines of the backends the project enables
(`halide_build_llvm_backend`), LLVM's (and for WebAssembly LLD's) include dirs as system includes, the
components (OrcJIT, BitWriter, Linker, Passes, the backends), and `LLD::lldWasm`/`lldCommon` for
WebAssembly. It asks for `LLVM`, `Clang` and (for WebAssembly) `LLD` with `package_want`. Errors,
never a downgrade: an enabled backend missing from `LLVM_TARGETS_TO_BUILD`, no Clang, WebAssembly
without an LLD (a Clang or LLD of another version than LLVM is the packages' `requires`). The
RTTI/exceptions check against LLVM is buildlog-cpp's generic C++ ABI relation: libHalide builds with
RTTI and exceptions (the defaults), LLVM with RTTI and without exceptions, which is allowed.

**The project** (`libhalide/`): `bindings.toml` declares the author-supported configurations, while `presets/homebrew-arm64.toml` records the user's local choices (default configuration, toolchain, the LLVM overlay search dirs `package_search_dir("buildlog", …)` for llvm@22 and lld@22, and `package_linkage` LLVM shared). The preset also binds SPIRV-Headers, flatbuffers and wabt through their CMake export files,
and OpenSSL's `libcrypto` through pkg-config (Homebrew's `OpenSSLConfig.cmake` is hand-written and
conditional). `spec.toml` is libHalide as `src/CMakeLists.txt` states it. `sources.toml` lists the 189
core sources in CMake order.

**Excluded and taken from the CMake build tree** (`CMAKE_TREE`, the baseline's `cmake-release`):
- the 443 objects of later phases, linked as external objects (`external-objects.toml`): 438
  `Halide_initmod` objects (phase 3: runtime bitcode through binary2cpp) and the 5 template objects
  (phase 2: `c_template`/`html_template` through binary2cpp). **Since phase 2** the 5 template objects
  are built (below); the 438 remain; **since phase 3** all are built and `external-objects.toml` is gone;
- `include/flatc/halide_ir.fbs.h` (phase 5, flatc), as an include dir. **Since phase 5** it is built,
  and `CMAKE_TREE` is gone from `buildlog.toml`;
- `include/Halide.h` (phase 2, build_halide_h), only for the smoke test. **Since phase 2** it is built.

`Serialization.cpp` and `Deserialization.cpp` are compiled, because the library has undefined
symbols without them, but `scope.toml` puts them in phase 5, so the comparison covers the other 188.

### Comparison (phase 1)

| | match | differ | only CMake | only buildlog |
|---|---|---|---|---|
| compile (188) | 0 | 188 | 0 | 0 |
| link (1) | 0 | 1 | 0 | 0 |
| symlink | 0 | 1 | 0 | 1 |
| phony (4) | 0 | 0 | 4 | 0 |

Without the path map nothing pairs up (193 only in CMake, 190 only in buildlog), because the output
layouts differ. With it, every command pairs, and every remaining difference is in one of these
categories:

1. **Tool spelling** (all compiles and the link): `/usr/bin/c++` (the xcrun shim) vs the resolved
   `$TOOLCHAIN/usr/bin/clang++`.
2. **Platform spelling** (all): `-arch arm64` vs `--target=arm64-apple-macos13 -isysroot $SDK`. This is
   a real difference: buildlog's inventory sets deployment target 13, CMake leaves the SDK default
   (26). ld warns that Homebrew's wabt and libcrypto were built for 26.
3. **`-O3` vs `-O2`** (all compiles): CMake's Release is `-O3 -DNDEBUG`, buildlog-cpp's
   `native.opt=release` is `-O2 -DNDEBUG`. *Closed 2026-09-29: buildlog-cpp's release is now `-O3`.*
4. **Include order** (all compiles): the same 2 `-I` and 5 `-isystem` directories, in CMake's
   declaration order vs sorted. `cc.include_dir` is a set in buildlog: there is no declaration order to
   keep.
   *Changed 2026-09-29 ("Include-directory classes" below): the flatc dir, an in-project dependency's,
   is `-isystem` here and `-I` in CMake; the order within a class is still sorted.*
5. **Link: `-O3 -DNDEBUG`** is on CMake's link line (CMake passes the configuration's compile flags to
   the link driver). *Corrected 2026-10-12:* not inert. clang passes `-O3` on to Apple's ld, which
   then outlines link-once-ODR functions; relinking with CMake's flags changes the code of 10 of the
   17 paired links, libHalide's dylib among them (historical comparison).
   *Closed 2026-10-11:* the spec gives every non-imported target `native.link_config_flags=on`
   (rules.dl), which puts the configuration's compile flags on the target's link line, as CMake does
   when the compiler driver links. It is a per-target property, not a plugin default: Meson (fribidi)
   passes no `-O` to links, and buildlog-cpp's links stay as they were for every other project.
6. **Link object order**: the same 632 objects, in source order vs sorted.
7. **Link library order**: the same 6 libraries (lldWasm, lldCommon, libLLVM, flatbuffers, wabt,
   libcrypto) and the same two rpaths, in a different order.
8. **Symlink shape**: one `cmake -E cmake_symlink_library` record with two outputs vs two `ln -sf`
   edges.
9. **CMake-only phonies**: `Halide`, the two `cmake_object_order_depends_target_*` and the
   `libHalide.dylib` alias.

Everything else matches: all 27 `-D` flags, all warnings, `-std=c++20` (from wabt's `cxx_std_20`
requirement, as in CMake), `-fPIC`, the `-isystem` set, and on the link `-dynamiclib`,
`-headerpad_max_install_names`, the exported symbols list, the compatibility and current versions,
the install name, the rpaths, the libraries and the objects.

**Normalizations** (each a rename of the same file or a no-op flag, documented in the file):
- `normalize.py` resolves relative paths that climb out of the build directory (`../../src/X.cpp`,
  `-I../../src`), because buildlog writes source paths relative to its build directory and CMake
  writes them absolute. It also classifies `ln -s` edges as `symlink`. The checked-in baselines
  renormalize byte-identically.
- `pathmap-libhalide.toml`: buildlog's `obj/<config>/Halide/src/` and `lib/<config>/` onto CMake's
  `src/CMakeFiles/Halide.dir/` and `src/`, for outputs and `-MF` depfiles; the CMake tree's own files
  (`$CMAKE`) onto `$BUILD`; and `-MT` ignored (it names the target inside the depfile; Ninja's
  `deps = gcc` doesn't read it).
- `--llvm /opt/homebrew/opt/llvm@22`: normalize.py already maps a root's realpath too, so buildlog's
  realpathed `Cellar/llvm@22/22.1.8` is `$LLVM`, like CMake's `opt/llvm@22`.

### Build

`ninja` runs 192 edges (189 compiles, the link, two symlinks) in 25 s wall on this machine and links
`libHalide.22.0.0.dylib` with an install name of `@rpath/libHalide.22.dylib` and 5587 exported
symbols. It links lldCommon, lldWasm, libLLVM, libcrypto, libc++ and libSystem.

The smoke test JIT-compiles and realizes a Func through the buildlog-built library, using the CMake
tree's `Halide.h`:

```
$ c++ -std=c++17 -I $CMAKE_TREE/include main.cpp -L$LIB -lHalide -Wl,-rpath,$LIB && ./a.out
arm-64-macos-arm_dot_prod-arm_fp16 6
```

### Oracle

`oracle_check.py` configures a small project per LLVM (the eight backends, llvm@N/lld@N pinned) and
compares what buildlog derives with Option 1's rows. Those rows are Halide's own
`FindHalide_LLVM.cmake`, run under CMake. It compares version, components, linkage, RTTI, the defines
a consumer of `halide-build::llvm` gets, the include dirs, the rpath link flags, and the imported
locations (`libLLVM.dylib`, `clang-22`, `llvm-as`, `liblldWasm`, `liblldCommon`). **All eight rows
agree for llvm@22 and for llvm@21**, including llvm@21's extra `EXPERIMENTAL_KEY_INSTRUCTIONS` define.

### Explanations

A define LLVM states leads to its `set()` line:

```
$ buildlog why -C BUILD --flag -D__STDC_LIMIT_MACROS --output obj/arm64-macos.release.none/Halide/src/Lower.cpp.o --leaves case-studies/halide/libhalide
  dep("Halide", "halide-build::llvm", "private")  ← case-studies/halide/libhalide/spec.toml:19
  res_pkg_candidate("find:llvm:dir:/opt/homebrew/opt/llvm@22:LLVM", "LLVM", "llvm", "…/LLVMConfig.cmake")  ← …; probe pkg_find …
  res_pkg_prop("import:llvm:…/LLVMConfig.cmake", "LLVM", "cc.define", "__STDC_LIMIT_MACROS", "")  ← /opt/homebrew/Cellar/llvm@22/22.1.8/lib/cmake/llvm/LLVMConfig.cmake:357; probe pkg_import …
  …
```

`tests/test_halide_phase1.py` checks these and the errors:
- an RTTI-off consumer: `C++ RTTI mismatch: … uses LLVM::LLVM, built with cxx.rtti=on`, whose proof
  leads to the consumer's prop and `LLVMConfig.cmake`'s `LLVM_ENABLE_RTTI` line (on libHalide, a
  `cxx.rtti off` prop gives the same error, explained to that prop's line and `LLVMConfig.cmake:279`);
- a literal LLVMConfig without WebAssembly: `Halide LLVM backend not built: WebAssembly
  (WITH_WEBASSEMBLY) is enabled but the bound LLVM … does not build it`, explained to the backend row
  in the spec and the `LLVM_TARGETS_TO_BUILD` line;
- llvm@21 and llvm@22 on the search path, no pin: core's `ambiguous package` per name, listing both
  candidates, explained to each search dir;
- a Clang of another version; static linkage built and run; unpinned linkage
  (`package linkage not chosen`).

### Timings (configure, i.e. emit)

| | buildlog | CMake (baseline) |
|---|---|---|
| cold | 2.3 s (2.8 s after R3b, 10.0 s after R3a, 11.7 s before) | 5.99 s |
| warm (nothing changed) | 2.0 s (2.3 s after R3a/R3b, 3.6 s before R3a) | 3.61 s |

Where the time goes (cProfile, single runs, so estimates):
- **Since R4** (105 probes: 78 `pkg_find`, 10 `pkg_import`, 6 `cxx_std`, 6 `file_archs`, 4
  `cc_capability`, the inventory): cold 2.3 s, warm 2.0 s, single runs. The find probes are cheap
  directory listings; one fewer rule package and no reader probes shorten the Soufflé rounds.
- **Cold, at R3b** (54 probes): Soufflé takes about 1.4 s over 5 rounds, composing and checking the program
  0.5 s, compiler probes (inventory, `cc_capability`, `cxx_std`) about 0.35 s, and parsing the six
  export files 0.2 s. Before refactor step R3b, 876 `lipo -info` import probes took about 7.4 s:
  buildlog-cpp probed every imported file (all of LLVM's ~430 archives and tools, once per inventory
  platform). Core's `file_archs` probe now reads object headers in-process, once per file of a
  *needed* import (6 files here, about 1 ms in total).
- **Warm**: Soufflé takes about 1.5 s over 5 rounds. Before refactor step R3a, re-appending the 914
  cached probe results took about 2.4 s, because `discovery.append_results` rewrote every fact file
  per result (quadratic); the fixpoint now writes each round's results once.

Soufflé itself is not the bottleneck at 189 compiles.

### Size

| | lines (non-blank, non-comment) |
|---|---|
| LLVM discovery, before refactor R4: `llvm_discovery.dl` + manifest; `L3_config.dl` | 119 + 22; 35 |
| LLVM discovery, since R4 (plugin removed): `buildlog_llvm/provider.py` + manifest (no Datalog) | 158 + 8 (127 + 8 + 4 counting `__init__`, non-blank non-comment) |
| LLVM, Clang, LLD tier-2 packages, one macOS preset (llvm@22 + lld@22): rules `LLVM.dl`, `Clang.dl`, `LLD.dl`; manifests + facts | 31 + 2 + 4; 39 + 32 + 16 + 2 (no Python) |
| the same for all five overlays (llvm@21/@22, lld@21/@22, Linux 22): manifests + facts | 272 |
| `halide_build.dl` (37 before the tier-2 packages) | 28 |
| `libhalide/` `buildlog.toml` + `bindings.toml` + `spec.toml` | 6 + 15 + 45 (70 since phase 2, 72 since phase 3; 5 + 16 + 72 since phase 5) |
| `libhalide/sources.toml`, `external-objects.toml` (generated lists) | 191, 445 (440 since phase 2; removed in phase 3) |
| `halide_build_gen.dl` (phase 2); `libhalide/headers.toml` (phase 2, generated list) | 33 (41 since phase 3, 58 since phase 5); 177 |
| `halide_build_runtime.dl` (phase 3; 54 of them the module and flag lists) | 140 |
| `src/runtime/CMakeLists.txt` (the runtime, plus the `Halide_Runtime` interface target) | 325 |
| generic reader library, in buildlog-cpp: `cmake_config.py` (a library for discovery plugins since refactor R2) | 279 |
| `cmake/FindHalide_LLVM.cmake` | 122 |
| `src/CMakeLists.txt` (all of libHalide's CMake, including phase 2/3/5 and install) | 562 |

### Rough edges (phase 1 log)

- **Importer: older export policy header.** SPIRV-Headers' export (vendored by Halide) states
  `CMake >= 2.8.0` in its first message. The importer template now accepts that version separately.
- **Probe cache keyed by importer version, not source.** Editing an importer without bumping
  `IMPORTER_VERSION` reuses stale (even failed) results. `rm -rf BUILD` is the workaround.
- **`as` is a Soufflé keyword** (a column name failed to parse), and **core's `used` is private**
  (packages test `vis != "interface"` instead).
- **Lock and golden churn.** Every buildlog-cpp change rewrites the plugin hash in every tracked
  `buildlog.lock`, and the `why` goldens embed rule line numbers, so new rules go at the ends of files.
- **Proofs through aggregates** don't list the counted tuples: core's `ambiguous package` error
  lists the candidates in its message, and its proof names each search dir. A `__agg_single(1)` leaf shows up in
  some proofs; its origin is the aggregate rule that contains it.
- **Negated causes.** `!llvm_component("WebAssembly")` alone doesn't say where the list came from.
  The error cites the list: since refactor R4, `package_attr("LLVM", "llvm.built_targets_from", file)`
  carries the `LLVM_TARGETS_TO_BUILD` line as its origin.
- **Relative `source_root`** resolves against the working directory, not the project (not fixed;
  phase 1 uses `${HALIDE_SRC}`).
- **LLVM's `-std`.** LLVM's headers need C++17, but its export files state no compile feature, so a
  consumer must set `cxx.std` itself (libHalide does; wabt raises it to 20).
- **Static LLVM needs extra bindings** (zstd, ZLIB). Homebrew's lld dylibs link `libLLVM.dylib`, so
  static LLVM with WebAssembly would load two LLVMs (not attempted).
- **Deployment target** (category 2). The eager import probes and quadratic fact appends (timings) were fixed by refactor steps R3b and R3a.
- **argv precision**: include order and link order are sets (categories 4, 6, 7). Not fixed: the
  build is correct, and a source-order mechanism is a design question.

### Open design questions

1. Exec LLVM ≠ target LLVM: both would import the same `LLVM::`/`Clang::` namespaces. Phase 3 (or a
   cross build) needs per-role namespaces or exec-configured imports. Since refactor R4 there is one
   binding per name (core selection), so a second LLVM is simply ambiguous.
2. Declaration order for include dirs and link inputs (a source-order key on set props?), or accept
   sorted order as buildlog's canonical form.
3. `-arch arm64`/SDK-default deployment target vs `--target=…-macos13`; `-O3` vs `-O2` for release. Is
   matching CMake's conventions a goal, or should these be project settings?
4. Should imported include dirs be system includes by default (`package_system_includes` is opt-in)?
   *Decided 2026-09-29: every dependency's dirs are `-isystem`, imported or not; see below.*
5. Static LLVM: should discovery own the ZLIB/zstd bindings (LLVM knowledge), and how should a closure
   that reaches a shared libLLVM through lld be cut or rejected?

## Phase 2: generated sources through host tools

Phase 2 builds Halide's host tools (`binary2cpp`, `build_halide_h` and `regexp_replace`) in the graph,
generates the 5 C/HTML template sources and `Halide.h` with them, and compiles the templates into
libHalide. **The headline: exec configuration is inferred from use.** No spec row or rule says that a
tool runs on the build machine. An action names its program by target reference
(`action_run(a, 0, "", "binary2cpp", "run")`), and core derives `tool_dep`, builds the tool for the
exec configuration, and fills in its path.

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --phase 1 --phase 2 --details 5
uv run --frozen pytest -q tests/test_halide_phase2.py tests/test_exec_inference.py
```

### What is modeled

**Core** (generic; see docs/architecture.md):
- `action_run(a, pos, flag, target, use)`: position `pos` of action `a` holds target `target`'s
  artifact; `use` is `run` (the program, or a tool the program runs) or `load` (a plugin or data file
  the program loads). Core derives `tool_ref`, resolves the reference like a `dep`, and derives
  `tool_dep(t, g)`. The rule reads only the reference, never a derived path, so it stays stratified.
  After `needed(g, exec)`, it fills in the path of `g`'s exec instance (`run_artifact`) as an
  `action_arg` and an implicit input. `tool_dep` is no longer open.
- `action_stdin(a, path)` / `action_stdout(a, path)`: typed redirections. The emitter appends
  `< in > out` after the argv; stdin is an explicit input and stdout must be an output of `a`.

**buildlog-cpp**: `cxx_generated_source(t, c, path)` (open) compiles an `out:` path an action produces
into `t`, like a `cxx_source`, at `obj/<config>/<t>/<path>.o`.

**halide-build** (`halide_build_gen.dl`, 33 lines, Halide-specific):
- `halide_build_embed(lib, tool, symbol, input, file)` gives `tool symbol < input > out:gen/<cfg>/<lib>/<file>`
  as a generated source of `lib`. These are the 5 templates in `spec.toml`, and phase 3's runtime
  modules will use the same form.
- `halide_build_mono_header(lib, tool, license, file)` with ranked `halide_build_header` rows
  (`headers.toml`, the 175 headers in CMake's HEADER_SET order) gives
  `tool LICENSE.txt headers… > out:include/<cfg>/Halide.h`. The directory becomes an include dir for
  `lib`'s consumers, and the header is a default output.

**The project**: the three tools are plain `executable` targets with their `cxx_source`. The 5 template
lines are gone from `external-objects.toml` (438 initmod objects remain). `regexp_replace` is
requested, because CMake's `all` builds it, but nothing runs it until phase 3.

**Annotation count: 0.** No `tool_dep`, `exec`, `host` or toolchain row for a tool appears in
`libhalide/` or `halide-build/`; `test_no_exec_annotations_in_the_spec_or_package` checks this. The
toolchain pin in the user preset is the user's per-language choice, the same one as in phase 1, and
it applies to the library and the tools alike.

### Comparison (phase 2, and phase 1 again)

| phase | kind | match | differ | only CMake | only buildlog |
|---|---|---|---|---|---|
| 1 | compile (188) | 0 | 188 | 0 | 0 |
| 1 | link (1) | 0 | 1 | 0 | 0 |
| 1 | phony (4) | 0 | 0 | 4 | 0 |
| 1 | symlink | 0 | 1 | 0 | 1 |
| 2 | compile (8) | 0 | 8 | 0 | 0 |
| 2 | custom (6) | 5 | 1 | 0 | 0 |
| 2 | link (3) | 0 | 3 | 0 | 0 |
| 2 | phony (9) | 0 | 0 | 9 | 0 |

**Phase 1 is unchanged** (the same table, with the same categories). In phase 2:
- **Custom commands**: the 5 binary2cpp commands **match exactly** (program, symbol, stdin, stdout). The
  one difference is Halide.h: CMake writes `Halide.h.tmp` and then runs `copy_if_different` (a
  restat-like guard), while buildlog writes `Halide.h` directly.
- **Compiles**: the 5 template objects and the 3 tool objects differ only in phase 1's categories 1–4
  (tool spelling, `-arch` vs `--target`/`-isysroot`, `-O3` vs `-O2`, include order). The tools are
  compiled with `-std=c++17` and `-DNDEBUG`, as in CMake.
- **Tool links**: phase 1's category 5 (`-O3 -DNDEBUG` on CMake's line) and argument order: CMake puts
  `-Wl,-search_paths_first -Wl,-headerpad_max_install_names` before the object, buildlog puts them
  after it. The same arguments appear in both.
- **CMake-only phonies**: `HalideIncludes`, the three tool names, and their
  `cmake_object_order_depends_target_*`.

The comparison tooling now takes `--phase` (repeatable). Its target map comes from buildlog's
`target_action` facts. The path map adds `gen/`→`src/`, `bin/`→`tools/`, tool objects →
`tools/CMakeFiles/<tool>.dir/`, and `include/<cfg>/`→`include/`. `compare.py` compares stdin and stdout
as part of the command (`< in`, `> out`).

### Build

`ninja` runs 209 edges in 26.7 s wall on this machine. The edges are phase 1's 192, plus 3 tool
compiles, 3 tool links, 5 binary2cpp runs, 5 template compiles and the Halide.h command.
- **The generated files are byte-identical to the CMake tree's**: `Halide.h` and all 5 template `.cpp`
  files (checked by `tests/test_halide_phase2.py`).
- The libHalide link takes the 5 buildlog-built template objects, and the dylib defines the 10
  `halide_*_template_*` symbols.

The smoke test (not checked in) JIT-compiles and realizes a Func through the buildlog-built library,
**now with buildlog's `Halide.h`**:

```
$ c++ -std=c++17 -I BUILD/souffle/include/arm64-macos.release.none main.cpp -L$LIB -lHalide -Wl,-rpath,$LIB && ./a.out
arm-64-macos-arm_dot_prod-arm_fp16-jit 6
```

### Explanations: why is binary2cpp built for this configuration, with this toolchain?

For libHalide the exec configuration is the target configuration (a native build), so the tools share
`bin/arm64-macos.release.none/`. `why` on the tool's compiler traces the configuration to its use, and
the toolchain to `runs_on`/`can_target` (excerpt):

```
$ buildlog why -C BUILD --flag $TOOLCHAIN/usr/bin/clang++ --output bin/arm64-macos.release.none/binary2cpp case-studies/halide/libhalide
action_arg("out:bin/arm64-macos.release.none/binary2cpp", 0, "", "…/clang++")  [buildlog:L3_native.dl:132]
  executable("binary2cpp", ‹arm64-macos.release.none›, …)
    needed("binary2cpp", ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:231]
      tool_dep("Halide", ‹arm64-macos.release.none›, "binary2cpp")  [buildlog:L1_targets.dl:463]
        tool_ref("Halide", …, "out:gen/…/c_template.CodeGen_C_prologue.template.cpp", "binary2cpp", "run")
          halide_build_embed_file(…)  [halide-build:halide_build_gen.dl:32]  (via action_run)
            halide_build_embed("Halide", "binary2cpp", …)  ← case-studies/halide/libhalide/spec.toml:20
      exec_config(‹arm64-macos.release.none›, ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:119]
        host_platform("arm64-macos")  …
  …
  tc_for("native-link", ‹arm64-macos.release.none›, "apple-clang")
    tc_select("native-link", "arm64-macos", "arm64-macos", "apple-clang")
      tc_candidate("native-link", "arm64-macos", "arm64-macos", "apple-clang")
        toolchain_runs_on("apple-clang", "arm64-macos")  ← probe inventory
        toolchain_can_target("apple-clang", "native-link", "arm64-macos")
      toolchain_pin("native-link", "arm64-macos", "arm64-macos", "apple-clang")  ← case-studies/halide/libhalide/presets/homebrew-arm64.toml:8
```

The chain reads: it runs during the build (`action_run` → `tool_ref` → `tool_dep`), so it is built for
the exec config, and it gets the toolchain that qualifies for it. Here that is also the pin.

**The cross case** is a toy, because real Halide cross builds are phase 7 (`tests/fixtures/exec-demo/`,
`tests/test_exec_inference.py`). A `gen` tool runs during a `toy-a` build on a `toy` host, and there are
two toolchains and no pin. `gen` is built once, for `toy.release.none`, and the product is built for
`toy-a.debug.none`. `why` shows that the choice follows from use and from `runs_on ∧ targets`:

```
$ buildlog why -C BUILD --package-path tests/fixtures/defined --flag /bin/cp --output toy.release.none/gen tests/fixtures/exec-demo/project
action_arg("out:toy.release.none/gen", 0, "", "/bin/cp")  [exec-demo:exec_demo.dl:28]
  execdemo_out("gen", ‹toy.release.none›, "out:toy.release.none/gen")
    needed("gen", ‹toy.release.none›)  [buildlog:L1_targets.dl:231]
      tool_dep("blob", ‹toy-a.debug.none›, "gen")  [buildlog:L1_targets.dl:463]
        tool_ref("blob", ‹toy-a.debug.none›, "out:toy-a.debug.none/blob", "gen", "run")
          action_run("out:toy-a.debug.none/blob", 0, "", "gen", "run")  [exec-demo:exec_demo.dl:34]
      …  host_platform("toy"), exec_value("native.opt", "debug", "release")
  tc_for("toy", ‹toy.release.none›, "tcy")
    tc_select("toy", "toy", "toy", "tcy")  (tc_candidate: toolchain_runs_on, toolchain_can_target; !pinned("toy"))
```

`why-not 'tc_candidate("toy","toy","toy","tcx")'` names the failing `toolchain_can_target` literal:
tcx only targets toy-a.

### Stated `tool_dep`s, migrated

- `examples/cross` (blurgen) now uses `action_run(…, "run")` for its generator. Its `build.ninja` is
  **byte-identical**.
- The halide rule-package fixture (`tests/fixtures/rule-packages/…/L5_halide_real.dl`) now uses
  `action_run` for its stub and object actions. Its three projects' `build.ninja` differ in one way
  only: the generator moves from an explicit input to an implicit one (`run  | …`). The commands and
  the input sets are identical, which a script over the before/after snapshots checked.

### Timings (configure)

Cold 2.8 s and warm 2.5 s (single runs on this machine, so estimates; phase 1 was 2.3 s and 2.0 s).

### Rough edges (phase 2 log)

- ~~**No restat / copy_if_different.**~~ Fixed after phase 2 (below): Halide.h is generated to
  `Halide.h.tmp` and copied with `buildlog -E copy_if_different` and restat.
- ~~**cwd is not modeled.**~~ Fixed after phase 2 (below): `action_cwd`, and `compare.py` compares it.
- **Header order needs ranks.** build_halide_h's output depends on argument order, and set-valued
  rows have none, so `headers.toml` ranks them. This is the same argv-order question as phase 1's
  categories 4, 6 and 7.
- **Generated object paths** repeat the config: `obj/<cfg>/Halide/gen/<cfg>/Halide/….cpp.o`.
- **Cosmetic**: an action with no explicit inputs emits `run  | …` (two spaces).
- **Churn**: every change to L4 or core rewrites every tracked lock and shifts the engine variable
  numbering in the `why` goldens.
- **Lost escape hatch**: `tool_dep` is derived only. A tool reached through the environment, a
  response file or a computed path can't be declared. None exists here.

### Open design questions

1. ~~**Pin scope.**~~ Decided and done in phase 7a (pins are `(lang, exec, target, tc)`). `toolchain_pin` was per language. In a cross build, a pin for the target's toolchain
   would also apply to the tools and fail (it can't target exec). Pins scoped by (language, exec,
   target) are a core feature.
2. ~~Restat~~ and 3. ~~cwd~~: decided and done after phase 2 (below).
4. Should the emitter write the program as an explicit input when nothing else is? It is currently
   always implicit.

### After phase 2: repin, working directory, restat, `buildlog -E`

- **Repin** to Halide main `f370931c79` (at the time; the pin is now `4e272cbfcc`, see Pinned inputs). The phase 1 and 2 comparisons are
  unchanged by it (the same table as above before the changes below).
- **Core**: `action_cwd(a, dir)`, `action_restat(a)` and
  `action_builtin(a, name)`, whose program is `buildlog -E name` (`copy`, `copy_if_different`,
  `create_symlink`, `make_directory`, `remove`, `touch`). `tests/test_actions_cwd_restat.py` runs
  them under Ninja, including a restat that keeps a consumer from rebuilding.
- **halide-build** now states the working directories CMake uses: binary2cpp runs in
  `out:gen/<cfg>/Halide` (CMake's `build/src`) and build_halide_h in `src:src`. Halide.h is two edges,
  `build_halide_h … > Halide.h.tmp` and `buildlog -E copy_if_different Halide.h.tmp Halide.h` with
  `restat = 1`. CMake's one custom command has four steps (make_directory, generate to `.tmp`,
  copy_if_different, rm); buildlog keeps the `.tmp`, which Ninja needs as the first edge's output.
  Halide.h and the 5 template sources are still byte-identical to CMake's.
- **Comparison**: `compare.py` now compares a step's working directory (`cd DIR` when it isn't
  `$BUILD`), and `compare_libhalide.py` fuses a `copy_if_different` edge with the edge that alone
  produces its input into one record (the producer is the main step), which is how CMake writes it.
  **Phase 2 custom: 6 match, 0 differ** (was 5 and 1), with the working directories. Everything
  else in the phase 1 and 2 tables is unchanged.
- **Checks**: every other example and project `build.ninja` is byte-identical (`ninja_snapshot`);
  libHalide rebuilds with Ninja (28.5 s, one run) and the smoke test still prints
  `arm-64-macos-arm_dot_prod-arm_fp16-jit 6`.

## Phase 3: the runtime (CMake's `Halide_initmod`)

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --phase 1 --phase 2 --phase 3 --details 5
uv run --frozen pytest -q tests/test_halide_phase3.py
```

### What is modeled

`halide-build/halide_build_runtime.dl` is `src/runtime/CMakeLists.txt`'s initmod part. The spec
states one fact, `halide_build_runtime(Halide, binary2cpp, src/runtime, 22.0.0, 22, 0, 0)`, plus the
Vulkan headers dir (`halide_build_runtime_include`, CMake's `find_package(VulkanHeaders)`). The
package has the module lists as explicit facts (`RUNTIME_CPP` 102, `RUNTIME_LL` 15,
`RUNTIME_HEADER_FILES` 13; no glob) and `RUNTIME_CXX_FLAGS`, and derives CMake's chains:

- **C++ modules** × {32, 64} × {plain, debug} = 408: `clang <flags> [-fpic] [-fshort-wchar] [-g
  -DDEBUG_RUNTIME] -D… -mJ -target T -emit-llvm -S -MD -MF … -o initmod.m_J.ll m.cpp`, then `llvm-as`,
  then `binary2cpp halide_internal_initmod_m_J < .bc > _initmod_m_J.cpp`. The data triple `T` follows
  the module's name the way CMake's `MATCHES` does (windows x86/arm/generic, webgpu, generic), and
  Windows-on-ARM only for the ARM/AArch64 backends the build enables.
- **LLVM modules** (15): `buildlog -E copy m.ll m.ll.transformed.ll`, `llvm-as`, `binary2cpp`.
- **Embedded sources** (15): the 13 runtime headers, `halide_buffer_t.cpp` and the PTX libdevice
  bitcode, straight through `binary2cpp`.

All land in `gen/<cfg>/Halide/runtime/` (CMake's `build/src/runtime/`) and the 438 `.cpp` files
compile into libHalide (`cxx_generated_source`). `llvm-as`, the copies and `binary2cpp` run in that
directory, as CMake's do. The clang compile doesn't: it writes a depfile, and a depfile and a cwd are
exclusive (CMake rewrites the depfile with `cmake_transform_depfile`).

**Exec, again with 0 annotations.** The tools are target references: `Clang::clang` and
`LLVM::llvm-as` are the bound LLVM's imported executables (from `ClangTargets.cmake` and
`LLVMExports.cmake`; `halide_build.dl` already checks that Clang is LLVM's version), and binary2cpp is
built. `tool_dep` now has four rows for Halide (binary2cpp, build_halide_h, Clang::clang,
LLVM::llvm-as), all derived. The data triples are arguments, not configurations. Annotation count
in the spec and halide-build: **0** (checked by `test_no_exec_annotations_in_the_spec_or_package`).

`why 'needed("Clang::clang", …)'` (excerpt):

```
needed("Clang::clang", ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:231]
  tool_dep("Halide", ‹arm64-macos.release.none›, "Clang::clang")  [buildlog:L1_targets.dl:463]
    tool_ref("Halide", …, "out:gen/…/Halide/runtime/initmod.alignment_128_32.ll", "Clang::clang", "run")
      halide_build_rt_cc("Halide", …, "alignment_128", 32, 0, "alignment_128_32", "i386-unknown-unknown-unknown", …)
          [halide-build:halide_build_runtime.dl:141]  (via action_run)
        halide_build_runtime("Halide", "binary2cpp", "src/runtime", "22.0.0", "22", "0", "0")  ← case-studies/halide/libhalide/spec.toml:32
```

**`external-objects.toml` is gone.** No object comes from the CMake tree any more. The only CMake-tree
input left is `include/flatc` (phase 5; built since then).

### Comparison (phases 1, 2 and 3)

| phase | kind | match | differ | only CMake | only buildlog |
|---|---|---|---|---|---|
| 1 | compile | 0 | 188 | 0 | 0 |
| 1 | link / symlink | 0 | 1 / 1 | 0 | 0 / 1 |
| 1 | phony | 0 | 0 | 4 | 0 |
| 2 | compile / custom / link | 0 / **6** / 0 | 8 / 0 / 3 | 0 | 0 |
| 2 | phony | 0 | 0 | 9 | 0 |
| 3 | compile | 0 | 846 | 0 | 0 |
| 3 | copy | 0 | 15 | 0 | 0 |
| 3 | custom | **861** | 0 | 0 | 0 |
| 3 | phony | 0 | 0 | 4 | 0 |

Phases 1 and 2 are unchanged. Phase 3 has no missing or extra edge. The differences, by category:

1. **408 clang compiles**: no `cd src/runtime` (the depfile, above) and no `-MJ …json` (CMake's
   `CMAKE_EXPORT_COMPILE_COMMANDS`). Flags, defines, triples, the depfile and the source are equal.
2. **438 initmod object compiles**: compiled into libHalide with libHalide's flags and toolchain (the
   phase 1 categories, plus libHalide's defines and include dirs); CMake compiles them in the OBJECT
   library `Halide_initmod` with only `-O3 -DNDEBUG -std=c++17 -arch arm64`. buildlog has no object
   libraries. The sources are byte arrays, so the flags don't change their meaning.
3. **15 copies**: `buildlog -E copy` where CMake has `cmake -E copy`.
4. **4 phonies** of the `Halide_initmod` target, only in CMake.

All 423 `llvm-as` and 438 `binary2cpp` runs match exactly, with their working directory and
redirections. For the comparison, `compare_libhalide.py` maps the runtime's edges to CMake's target
`Halide_initmod`, and the path map renames `obj/<cfg>/Halide/gen/<cfg>/Halide/runtime/` to
`src/runtime/CMakeFiles/Halide_initmod.dir/`. `normalize.py` now classifies `buildlog -E copy…` like
`cmake -E copy…` (kind `copy`).

### Outputs

- The 15 copies and the 30 `.cpp` files with no source path inside (the 15 embedded sources, the 15
  LLVM modules) are byte-identical to CMake's.
- The 408 compiled `.ll` files differ from CMake's **only in how the source path is spelled**:
  buildlog passes it relative to the build directory, CMake absolute, and clang records it in the
  module id, in `__FILE__` strings (the runtime's assertion messages) and in debug info (with the
  working directory). 192 are equal once the path is replaced; the other 216 (the debug modules, and a dozen Windows ones: Windows
  CodeView records the directory) also differ in metadata numbering. Rerunning buildlog's
  command for three of them (a debug, a Windows and a Windows debug module) from CMake's directory with
  absolute paths reproduced CMake's files byte for byte. So the 408 `.bc` and embedded `.cpp` files
  differ too. This is an open question.
- The dylib defines the same 876 `halide_internal_initmod_*`/`halide_internal_runtime_header_*`
  symbols as CMake's.

### Build

`ninja` builds all 1932 edges in 36.9 s (one cold run on this machine, so an estimate). The smoke
test (not checked in; written to a scratch directory and deleted) still prints
`arm-64-macos-arm_dot_prod-arm_fp16-jit 6`, and now also AOT-compiles a pipeline to static libraries
for `x86-64-linux-debug`, `arm-64-windows`, `x86-32-windows` and `wasm-32-wasmrt`, which links in the
buildlog-built runtime modules for those targets.

### Timings (configure)

Cold 3.5 s, warm 3.1 s (single runs; phase 2 was 2.8 s and 2.5 s).

### Rough edges (phase 3 log)

- **Source path spelling reaches the product**: relative `src:` paths end up
  in `__FILE__` strings and debug info inside libHalide.
- **A cwd excludes a depfile**, so the runtime's clang runs in the build directory. A `buildlog -E`
  depfile rewrite (CMake's `cmake_transform_depfile`) would lift this.
- **No object libraries**: the initmod objects take libHalide's flags. Harmless here, but a real
  difference for sources whose meaning depends on flags.
- **Name-derived facts**: CMake derives the header symbols by replacing `.` with `_`; halide-build
  states both names (no string surgery in rules). The module lists are facts copied from CMake's
  lists, so they must be kept in sync by hand, as CMake's are.
- **The program as an implicit input** (open question 4 of phase 2): CMake lists clang and llvm-as as
  explicit inputs, buildlog as implicit ones. `compare.py` doesn't count it.

## Phase 5: the serialization header (CMake's flatc step)

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --phase 1 --phase 2 --phase 3 --phase 5 --details 5
uv run --frozen pytest -q tests/test_halide_phase5.py
```

Phase 5 comes before phase 4: it is the
last input libHalide took from the CMake tree.

### What is modeled

The spec states one fact, `halide_build_flatc(Halide, flatbuffers::flatc, src/halide_ir.fbs,
halide_ir.fbs.h)`, and the user preset binds flatbuffers' `FlatcTargets.cmake` (the imported
executable `flatbuffers::flatc`, next to the library's `FlatBuffersTargets.cmake`). halide-build's
`halide_build_gen.dl` (17 lines more) derives CMake's `generate_fb_header` custom command:

- **FLATC**: `cd gen/<cfg>/Halide && flatc --cpp --cpp-std C++17 --no-union-value-namespacing
  --keep-prefix --filename-suffix .fbs -o …/include/<cfg>/flatc …/src/halide_ir.fbs`, CMake's flags,
  its working directory (`build/src`) and its output dir (`build/include/flatc`).
- The dir and header reach libHalide's compiles through an interface target
  `halide-build::Halide.flatc` that libHalide depends on privately: `cc.include_dir` (a private `-I`,
  CMake's `FILE_SET fb_headers`) and `cc.generated_header` (an order-only dependency of every
  libHalide compile, CMake's `add_dependencies(Halide generate_fb_header)`).

**Exec, 0 annotations.** flatc is a target reference (`action_run … "run"`), so `tool_dep` gains
`flatbuffers::flatc`, derived; the annotation scan still counts **0**.

**Nothing comes from the CMake build tree any more.** `buildlog.toml` has no `CMAKE_TREE` variable,
and `build.ninja` names no file under `cmake-release` (checked by `tests/test_halide_phase5.py`).

### Comparison (phases 1, 2, 3 and 5)

| phase | kind | match | differ | only CMake | only buildlog |
|---|---|---|---|---|---|
| 1 | compile | 0 | 188 | 0 | 0 |
| 1 | link / symlink | 0 | 1 / 1 | 0 | 0 / 1 |
| 1 | phony | 0 | 0 | 4 | 0 |
| 2 | compile / custom / link | 0 / **6** / 0 | 8 / 0 / 3 | 0 | 0 |
| 2 | phony | 0 | 0 | 9 | 0 |
| 3 | compile / copy / custom | 0 / 0 / **861** | 846 / 15 / 0 | 0 | 0 |
| 3 | phony | 0 | 0 | 4 | 0 |
| 5 | compile | 0 | 2 | 0 | 0 |
| 5 | custom | **1** | 0 | 0 | 0 |
| 5 | phony | 0 | 0 | 3 | 0 |

Phases 1–3 are unchanged in counts. The flatc run matches CMake's exactly, working directory
included. The 2 compiles (`Serialization.cpp`, `Deserialization.cpp`) differ in phase 1's categories
only (toolchain, `-O2`/`-O3`, `--target`/`-arch`, sysroot, the `-isystem` order). The 3 phonies are
CMake's `generate_fb_header` target. The path map now renames `include/<cfg>/` (in `-I` too) to
CMake's `include/`. **One improvement in phase 1**: the `-I` order of all libHalide compiles now
equals CMake's (`src`, then `flatc`); before, the CMake tree's flatc dir came as a spec include
dir ahead of `src`.

### Outputs and build

- `halide_ir.fbs.h` is byte-identical to CMake's.
- `ninja` builds all 1933 edges in 34.2 s (one cold run, an estimate). The smoke test (written to a
  scratch directory and deleted) prints `arm-64-macos-arm_dot_prod-arm_fp16-jit 6` and AOT-compiles
  to `x86-64-linux-debug` and `wasm-32-wasmrt` static libraries.
- **No CMake tree** (the exit check, rerun after phase 6 on a fresh build directory): the spec and
  packages name no CMake-tree path (only the comparison tools and byte-equality tests read
  `build/cmake-release`). `build.ninja` mentions neither `cmake-release` nor `CMakeFiles`, and
  `ninja -t deps` records no header from there after `ninja lib/<cfg>/libHalide.dylib` (1930 edges,
  30 s, one run). The smoke test prints `arm-64-macos-arm_dot_prod-arm_fp16-jit 6`. The run found a
  core bug: `libHalide.dylib` links to `libHalide.22.dylib` but did not depend on it, so building
  the development name alone left it dangling. It is fixed in `L3_native.dl` and tested in
  `test_shared_library_links_versioned_and_runs`.

### Timings (configure)

Cold 3.8 s, warm 3.3 s (single runs; phase 3 was 3.5 s and 3.1 s).

### Rough edges (phase 5 log)

- **One header, every compile**: `cc.generated_header` orders all 189 core compiles after flatc, not
  only the two that include the header. CMake does the same (a target-level dependency); a
  per-source relation would be finer.
- **The interface target is a naming device** (`halide-build::Halide.flatc`): a generated include
  dir private to one target needs *some* carrier for `cc.include_dir`/`cc.generated_header`; a
  target's own private props would do without it.
- **flatc's flags are package facts**, copied from `src/CMakeLists.txt`.

## Phase 4: the generator tooling and the autoschedulers

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --phase 4 --details 5
BUILDLOG_HALIDE_BUILD=1 uv run --frozen pytest -q tests/test_halide_phase4.py
```

Scope (`scope.toml`): everything CMake builds under `tools/` and `src/autoschedulers*`, which is 151
edges.

### What is modeled

The spec is `libhalide/autoschedulers.toml`, and `spec.toml` gains the `Halide_Runtime` interface
target. halide-build gains `halide_build_generator.dl` (41 lines).

- **tools/**:
  - `Halide_GenGen` is a static library.
  - `Halide_Generator` is an interface target with a whole-archive edge to GenGen (CMake's
    `$<LINK_LIBRARY:WHOLE_ARCHIVE,…>`).
  - `gengen` is Halide_GenRT.
- **The helpers**:
  - `Halide_ASLog`: static, PIC.
  - `Halide_Plugin`: an interface target (the `common` include dir; Halide and ASLog as interface
    dependencies).
  - `get_host_target` and `featurization_to_sample`.
- **The four autoschedulers are module libraries**: `libautoschedule_<name>.so`, built with
  `-bundle`.
- **For adams2019 and anderson2021**:
  - The weights are an object library built from `binary2cpp baseline_weights < baseline.weights >
    baseline.cpp`. This reuses `halide_build_embed`.
  - The `cost_model.generator` links `Halide_Generator`.
  - Two generated libraries.
  - A generated runtime.
  - `retrain_cost_model` and `weightsdir_to_weightsfile`.
- **`halide_build_library(lib, from, generator, function, runtime)`** is `add_halide_library` (sugar: `[target.lib] halide_build_library = { from = ... }`, see `packages/README.md`). It
  runs `gen -n lib -d 0 -g … -f … -e c_header,object -o . target=<T>-no_runtime` in
  `gen/<cfg>/<lib>/`.
  - `lib` is a static library of the `.o`.
  - Its consumers get the `.h` directory as an include dir, and the `.h` as a generated header.
  - It depends publicly on its runtime.
- **`halide_build_runtime_library(rt, tool, headers)`** is `add_halide_runtime`. It runs
  `gengen -r rt -o . -e object target=<T>`.
- **`<T>` is the Halide target of the library's configuration**: its platform's cpu and os, spelled
  the Halide way (`arm64` becomes `arm-64`). The configuration where the generator runs doesn't
  matter.

The core mechanisms this phase needed are generic, with tests in `tests/test_cpp_mechanisms.py`:

- module and object libraries;
- whole archives of built targets;
- interface dependencies;
- the private link dependencies a shared or module library hides;
- build-tree rpaths.

**Exec, 0 annotations.** Each generator and gengen is a target reference (`action_run … "run"`).
So `tool_dep` gains, all derived:

- `(<m>_cost_model, <m>_cost_model.generator)`
- `(<m>_train_cost_model, …generator)`
- `(<m>_cost_model.runtime, gengen)`
- `(<m>_weights_obj, binary2cpp)`

The generators link libHalide, so libHalide is needed in the exec configuration too. For this native
build that is the same instance. The annotation scan (`tests/test_halide_phase2.py`) now covers
every layer of halide-build and every spec file, and it still counts **0**.

### Why is the cost model's generator built for this configuration?

```
$ buildlog why -C BUILD 'needed("adams2019_cost_model.generator", ‹arm64-macos.release.none›)' case-studies/halide/libhalide
needed("adams2019_cost_model.generator", ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:231]
  tool_dep("adams2019_cost_model", ‹arm64-macos.release.none›, "adams2019_cost_model.generator")  [buildlog:L1_targets.dl:468]
    tool_ref("adams2019_cost_model", …, "out:gen/…/adams2019_cost_model/adams2019_cost_model.o", "adams2019_cost_model.generator", "run")
      action_run(…, 0, "", "adams2019_cost_model.generator", "run")  [halide-build:halide_build_generator.dl:30]
        halide_build_library("adams2019_cost_model", "adams2019_cost_model.generator", "cost_model", …)  ← libhalide/autoschedulers.toml:56
        eff_dep("adams2019_retrain_cost_model", …, "adams2019_cost_model", "private")  …  request(…)
  exec_config(‹arm64-macos.release.none›, ‹arm64-macos.release.none›)  [buildlog:L1_targets.dl:119]
```

The generator runs to make the library, so it is built for the library's exec configuration. The
library's Halide target reads the other way, from the configuration the library is built for:

```
$ buildlog why -C BUILD --flag target=arm-64-macos-no_runtime --output gen/arm64-macos.release.none/adams2019_cost_model/adams2019_cost_model.o case-studies/halide/libhalide
action_arg(…, 13, "target=", "arm-64-macos-no_runtime")  [halide-build:halide_build_generator.dl:31]
  halide_build_lib_out("adams2019_cost_model", ‹arm64-macos.release.none›, …)
  halide_build_target(‹arm64-macos.release.none›, "arm-64-macos")  [halide-build:halide_build_generator.dl:15]
    platform_constraint("arm64-macos", "cpu", "arm64")
    halide_build_arch("arm64", "arm-64")  ← halide-build/halide_build_generator.dl:11
```

**A `why` fix.** Before this phase, `why` attributed a rule that is one head of a multi-head clause
to the wrong clause: `halide_build_runtime.dl:147` instead of the generator's clause. Soufflé splits
such a clause into one rule per head, and those rules print without locations. The clause matcher
(`provenance/program.py`) used to score the whole clause, so it now scores each head together with
the body. The goldens are unchanged, and `tests/test_halide_phase4.py` checks the citation.

### Comparison (phase 4)

The comparison needs two things a CMake graph gets from its File API. `compare_libhalide.py`:

- gives buildlog's output-named targets CMake's names (gengen becomes `Halide_GenRT`,
  `autoschedule_<name>` becomes `Halide_<Name>`);
- passes each target's CMake source directory, read from the baseline, to `normalize.py
  --target-dirs`. This is what `scope.toml`'s `dir` rules match.

The path map gains the phase 4 layout renames.

| phase | kind | match | differ | only CMake | only buildlog |
|---|---|---|---|---|---|
| 4 | archive | 0 | 8 | 0 | 0 |
| 4 | compile | 0 | 37 | 0 | 0 |
| 4 | custom | **2** | 6 | 1 | 0 |
| 4 | link | 0 | 13 | 0 | 0 |
| 4 | phony | 0 | 0 | 84 | 0 |

Phases 1, 2, 3 and 5 are unchanged: the same table as phase 5 above.

The differences, by category:

- **custom**:
  - The 2 weights embeds match.
  - The 4 generator runs and 2 runtime runs differ only in `-o .` versus CMake's absolute `-o
    $BUILD/src/autoschedulers/<m>/`, which is the same directory: the run's working directory.
  - The 1 only-CMake edge is the tutorial's `my_first_generator_win32.runtime.lib`. It is gengen's,
    but it belongs to `tutorial/`, which is out of scope.
- **compile**:
  - All 37 differ in phase 1's toolchain categories: the tool, `-O2`/`-O3`, `--target`/`-arch`, and
    the sysroot. For 13 of them that is the only difference.
  - 24 also differ in `-I` order. buildlog orders a target's own dirs first, then its dependencies'
    dirs sorted by value; CMake follows link order.
  - 22 of those 24 have one more `-I`. Each generated library has its own directory
    (`gen/<cfg>/<lib>/`), while CMake writes both of a directory's libraries into one.
- **link** (13): the toolchain; `-Wl,-rpath,@loader_path/../../lib/<cfg>` versus CMake's absolute
  `$BUILD/src`; `-Wl,-force_load,X` versus `-Xlinker -force_load -Xlinker X`; and the order of link
  options, objects and libraries.
- **archive** (8): `ar rcs` versus CMake's `ar qc` plus `ranlib`.
- **phony** (84): CMake's per-target and per-directory phonies, the `*.update` utility targets, and
  the object-order phonies.

### Build and use

- **The build.** `ninja` builds all 2000 edges in 43.8 s (one cold run, an estimate; phase 5 was 1933
  edges).
- **The runs.** The four generator runs and the two gengen runs write `.h`/`.o` and `.o`.
  `get_host_target` prints `arm-64-macos-arm_dot_prod-arm_fp16`.
- **The plugins load.** A buildlog-built generator loads each of the four plugins with `-p`
  (`test_build_runs_generators_and_loads_plugins`, under `BUILDLOG_HALIDE_BUILD=1`).
- **The smoke test.** It was written to a scratch directory and deleted. It prints
  `arm-64-macos-arm_dot_prod-arm_fp16-jit 6`. It then `load_plugin`s each autoscheduler and
  `apply_autoscheduler`s a small pipeline, and all four schedule and realize (`3970`). Anderson2021
  needs a GPU target, so it ran with `HL_JIT_TARGET=host-metal`; on the plain host target it reports
  `target.has_gpu_feature()`, which is Halide's check.

### Timings (configure)

Cold 3.8 s, warm 3.7 s (single runs; phase 5 was 3.8 s and 3.3 s).

### Size

| | lines (non-blank, non-comment) |
|---|---|
| `libhalide/autoschedulers.toml` (the props are per target: `cxx.std`, visibility, link options) | 179 |
| `halide_build_generator.dl` | 41 |
| CMake: `tools/CMakeLists.txt` + `src/autoschedulers/**/CMakeLists.txt` | 303 (plus `HalideGeneratorHelpers.cmake`'s `add_halide_library`) |

### Rough edges (phase 4 log)

- **Directory-wide settings are repeated per target.** CMake sets `CMAKE_CXX_STANDARD` everywhere,
  and `CMAKE_CXX_VISIBILITY_PRESET` and `CMAKE_VISIBILITY_INLINES_HIDDEN` for a whole directory.
  The spec has no directory scope, so it states `cxx.std`, the visibility flags and the link
  options on each target (about 60 of its 179 lines).
- **PIC for a static library is a clang flag.** ASLog's `POSITION_INDEPENDENT_CODE` is a
  `cc.compile_flag -fPIC`. Core spells PIC only for shared and module libraries; a `cc.pic` setting
  would do this properly.
- **Output names are target names.** buildlog names artifacts by target, so gengen and
  `autoschedule_<name>` are target names, and the comparison renames them. An output-name
  property would keep CMake's target names.
- **FEATURES is not modeled.** `add_halide_library`'s `FEATURES[arm-64-macos]` is not applied by
  the baseline's CMake either. buildlog matches the baseline, which leaves out
  `-arm_dot_prod-arm_fp16`.
- **The weights object is in the link, not in the sources.** CMake's `$<TARGET_OBJECTS:…>` in a
  target's sources is a private dependency on the object library here. The result is the same
  objects.
- **The build-tree rpath is relative** (`@loader_path`), not CMake's absolute path. The build tree
  can be moved; the install rpaths are phase 8.

## Phase 6: the sampled tests

```sh
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml
ninja -C BUILD/souffle
uv run --frozen buildlog test -C BUILD -j 4
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --phase 6 --details 5
BUILDLOG_HALIDE_BUILD=1 uv run --frozen pytest -q tests/test_halide_phase6.py
```

Scope (`scope.toml`): the dependency closure of the 32 sampled ctest tests under `test/` (20
correctness tests, 11 generator tests and the adams2019 demo; `baseline/scope.md` says why each is
in the sample), which is 532 CMake edges (197 without phonies).

### What is modeled

The spec is `libhalide/tests.toml` (new; it needs libpng and libjpeg, bound from Homebrew's `.pc`
files, and zlib from the SDK's). halide-build's `halide_build_generator.dl` grows from 41 to 194
lines; the phase 4 libraries' commands are unchanged.

- **The tests' settings**: CMake's directory-wide options and `HalideTestHelpers`' interface
  libraries are interface targets (`test_defaults`, `test_warnings`, `halide_generator_options`,
  `halide_aot_test`, `Halide_test`), as are `Halide::ImageIO`, `Tools`, `ThreadPool` and
  `RunGenMain`. `Halide_terminate_handler` is an object library, `_test_internal` an executable.
- **The executables**: the 20 correctness tests (`plain_c_includes` is C), 13 generators, 2
  externs libraries, 11 AOT tests, `rungen_test` and the adams2019 demo.
- **`add_halide_library`**, in full for what the sample uses:
  - `TARGETS` (several: a multitarget library, whose `lib_wrapper.o` names the action, with one
    `lib-<target>.o` per target), `FEATURES` and `PARAMS` (named sets);
  - `PLUGINS` and `AUTOSCHEDULER`, whose plugin CMake appends to the plugins (so a library can load
    the same plugin twice, as CMake's command does) and whose `autoscheduler=` param it adds;
  - `GRADIENT_DESCENT` (`-d 1`), `FUNCTION_INFO_HEADER`, `REGISTRATION` (a generated source of its
    consumer), `C_BACKEND` (the `.halide_generated.cpp` is compiled, PIC, into the library);
  - the default target: `host` when the library's configuration is built on its generator's
    platform, else the configuration's own (CMake: `Halide_TARGET`); `host` on another platform is
    an error;
  - the implicit `<lib>.runtime`, and **derived runtimes**: `add_halide_runtime` without
    `TARGETS` takes its users' targets without the per-library features (`no_runtime`,
    `no_bounds_query`, `user_context`, ...), deduplicated;
  - **stubs** (`add_halide_generator`'s `STUB`): `-e cpp_stub` into an interface target.
- **Core**: `native.link_option` now propagates like CMake's `INTERFACE_LINK_OPTIONS` (public and
  interface props reach consumers), for the tests' interface targets. The rest came in the phase 6
  core commit: joined argv words and tool-reference lists (the `-p a.so,b.so` plugin list),
  C sources, `cxx.compile_flag` and `cc.pic`.

**Exec, 0 annotations.** Every generator, plugin and gengen is a target reference
(`action_run`, `action_run_item … "load"`). `tool_dep` has 74 rows, all derived, e.g.
`(autograd_grad, autograd.generator)`, `(autograd_grad, autoschedule_mullapudi2016)`,
`(stubtest.stub, stubtest.generator)`, `(multitarget.runtime, gengen)`. The annotation scan counts
**0**. The default `host` target is the one place the package *reads* core's derived
`exec_config` (one rule body; it states nothing about where anything runs); the scan allows
exactly that read.

### Why does autograd_grad load Mullapudi2016 in the exec configuration?

```
$ buildlog why -C BUILD 'tool_dep("autograd_grad", ‹arm64-macos.release.none›, "autoschedule_mullapudi2016")' case-studies/halide/libhalide
tool_dep("autograd_grad", ‹arm64-macos.release.none›, "autoschedule_mullapudi2016")  [buildlog:L1_targets.dl:468]
  tool_ref("autograd_grad", …, "out:gen/…/autograd_grad/autograd_grad.o", "autoschedule_mullapudi2016", "load")  [buildlog:L1_targets.dl:483]
    action_run_item("out:gen/…/autograd_grad/autograd_grad.o", 12, 0, "autoschedule_mullapudi2016", "load")  [halide-build:halide_build_generator.dl:187]
      halide_build_lib_out("autograd_grad", …)  [halide-build:halide_build_generator.dl:128]
        halide_build_library("autograd_grad", "autograd.generator", "autograd", "autograd_grad", "")  ← libhalide/tests.toml:162
        eff_dep("generator_aot_autograd", …, "autograd_grad", "private")  …  request("generator_aot_autograd", "release")
```

The generator loads the plugin while it runs, so the plugin is needed wherever the generator
runs: the exec configuration (here the same instance, a native build).

### Tests

The sampled tests are typed facts. The spec states which target each
test runs, with its CMake GROUPS (`halide_build_test`, `halide_build_test_group`), the multitarget
tests' ENVIRONMENT (`halide_build_test_environment`), the demo's ARGS (`test_arg`) and
`$<TARGET_FILE:Halide::Mullapudi2016>` as a target reference (`test_arg_ref`). halide-build's
`halide_build_test.dl` derives what `add_halide_test` sets: the labels, `HL_TARGET` and
`HL_JIT_TARGET` (`host`, since the configuration is native; a cross configuration would get its
Halide target, and its tests are not run), the SKIP regex, `Success!` as the pass regex, and
RUN_SERIAL for `multithreaded`. The configure writes `BUILD/souffle/tests.json`. `buildlog test -C
BUILD` builds what the tests need (the Mullapudi2016 plugin is needed because the test names it)
and runs them with ctest's rules, each in `test/<config>/<name>` under the build directory:
**30 passed, 2 skipped, 0 failed**, the same as CMake's release tree (the 2 GPU tests skip). With
`-j 4` (the 7 serial tests run alone), the run takes 93 s of test time and 75 s wall. That is one
run on a built tree; 50 s of it is `correctness_cross_compilation`. Nothing Halide-specific is in
the runner; the phase 6 case-study script it replaces is gone.

### Comparison (phase 6)

`compare_libhalide.py` computes the sampled tests' closure in the buildlog graph
(`dep_resolves` and `tool_dep`) and passes it to `normalize.py --target-closure`, which a CMake
graph gets from its File API; the baseline's own phase 6 targets stay in scope too. A stub target
gets its generator's directory. The path map gains the test directories' layout renames.

| phase | kind | match | differ | only CMake | only buildlog |
|---|---|---|---|---|---|
| 6 | archive | 0 | 46 | 0 | 0 |
| 6 | compile | 0 | 57 | 0 | 0 |
| 6 | custom | 0 | 46 | 1 | 0 |
| 6 | link | 0 | 46 | 0 | 0 |
| 6 | pch | 0 | 0 | 1 | 0 |
| 6 | phony | 0 | 0 | 335 | 0 |

Phases 1–5 are unchanged. Every CMake edge but the PCH and one stub has a counterpart, and nothing
is only buildlog's. The differences, by category:

- **custom** (46 generator and gengen runs): all differ only in `-o .` versus CMake's absolute
  output directory (the run's working directory, as in phase 4); the 6 with plugins also spell the
  plugin paths relative to it. Target lists, features, params, `-e` lists and plugin lists are
  CMake's. The only-CMake edge is `example.stub.h`, the stub of CMake's `generator_jit_example`
  support, which the sample doesn't use and the spec doesn't declare.
- **compile** (57): phase 1's toolchain categories (tool, `--target`/`-arch`, sysroot) and, because
  `wipe_standard_flags` is not modeled, `-O2 -DNDEBUG` where CMake's tests have none; for 23 that is
  all. 20 also lack CMake's PCH (`-include-pch` of `_test_internal`'s header). 10 have more
  `-I $BUILD/test/generator` (one gen dir per library, as in phase 4). 5 get libpng/libjpeg's
  include dirs from their `.pc` files (`$HOMEBREW/opt/...`) where CMake's FindPNG/FindJPEG give
  `$HOMEBREW/include`.
- **link** (46): the toolchain, `-Wl,-rpath,@loader_path/…` versus absolute, `-force_load`
  spelling and the order of objects, libraries and options (phase 4's categories); 3 link
  libpng16/libjpeg/`-lz` from `.pc` files where CMake links `$HOMEBREW/lib/libpng.dylib`,
  `libjpeg.dylib` and the SDK's `libz.tbd`.
- **archive** (46): `ar rcs` versus `ar qc` plus `ranlib`; `libmultitarget.a` also lists its
  members in another order.
- **pch** (1): `_test_internal`'s PCH (not modeled).
- **phony** (335): CMake's per-target phonies, the `*.update` utility targets and the object-order
  phonies.

### Build

`ninja` builds all 2194 edges (2195 `build` statements) with 0 failures in 50 s wall on this
machine (one cold run, an estimate). The only output is ld's warning that Homebrew's libpng and
libjpeg are built for macOS 26 (the platform is macOS 13).
`test_build_and_run_the_sampled_tests` (`BUILDLOG_HALIDE_BUILD=1`) has `buildlog test -j 4` build
a fresh tree and run the sample: 127 s (one run; it was 161 s with the serial case-study script). Build directory: 339 MB.

### Timings (configure)

Cold 4.9 s, warm 4.5 s (single runs; phase 4 was 3.8 s and 3.7 s).

### Size

| | lines (non-blank, non-comment) |
|---|---|
| `libhalide/tests.toml` | 503 (71 of them the test rows) |
| `halide_build_generator.dl` (phases 4 and 6) | 194 |
| `halide_build_test.dl` | 24 |
| CMake: `test/CMakeLists.txt`, `test/{correctness,generator}/CMakeLists.txt`, `test/autoschedulers/adams2019/CMakeLists.txt`, `cmake/HalideTestHelpers.cmake` | 1671 (all their tests, not only the sample; plus `add_halide_library`) |

### Rough edges (phase 6 log)

- **Tests run in `test/<config>/<name>`**, not in CMake's `WORKING_DIRECTORY` default (the test's
  binary directory, e.g. `test/correctness`). The sampled tests write only scratch files there.
- **WILL_FAIL (`EXPECT_FAILURE`) is not modeled**, nor is Adams2019's `LD_LIBRARY_PATH`
  ENVIRONMENT_MODIFICATION (not needed on macOS, and its test is not in the sample).
- **No PCH.** `_test_internal` is built, without its precompiled header, and the tests compile
  without it.
- **`wipe_standard_flags`** (test/'s HalideTestHelpers.cmake strips `[/-]D *NDEBUG` and
  `[/-]O[^ ]+` from the directory's `CMAKE_<LANG>_FLAGS_<CONFIG>`) *was not modeled until 2026-10-11*.
  It is now the core property `native.wipe_config_flag` (a regex over the family's spelling of a
  configuration flag; a target that uses it compiles and links without the matches), set on
  `test_defaults` (interface) and `Halide_terminate_handler` in rules.dl. The 57 test compiles and
  the 46 test links carry exactly CMake's flags.
- **An interface source is listed by its consumer.** `Halide::RunGenMain`'s `RunGenMain.cpp` is a
  source of the demo in the spec; core has no interface sources.
- **A C-backend twin.** `example_cpp`'s consumers include `example.h`, the header of its twin
  library `example`; the package passes the twin's include dir and header, and it is an error if
  the twin is not built.
- **One gen directory per library** (phase 4): an extra `-I` per library a test uses.
- **`-o .` and relative `-p`** where CMake writes absolute paths (the working directory).
- **Directory settings are interface targets**, which needed `native.link_option` propagation; the
  generators still lack `add_halide_generator`'s `-Wdeprecated-declarations` in phase 4's
  (unchanged) libraries.
- **`example.stub`** (CMake's `generator_jit_example` support) is not declared.

## Phase 7a: variants, per-platform discovery, scoped pins

```sh
# release is the spec's config_default; debug, asan, universal2 and x86_64 are declared too
uv run --frozen buildlog configure case-studies/halide/libhalide -C BUILD \
    --package-path case-studies/halide/packages \
    --user case-studies/halide/libhalide/presets/homebrew-arm64.toml --config debug
ninja -C BUILD/souffle
uv run --frozen buildlog test -C BUILD -j 4
uv run python case-studies/halide/phase1/compare_libhalide.py BUILD --variant debug \
    --phase 1 --phase 2 --phase 3 --phase 4 --phase 5 --phase 6 --details 5
```

### What changed

- **One author spec plus user preset** (`libhalide/bindings.toml` and `presets/homebrew-arm64.toml`): `release`, `debug`, `asan`
  (`native.opt=release` plus `native.sanitize=address`, as `run-cmake.sh`'s asan variant),
  `universal2` and `x86_64`; `config_default = [["release"]]`. The request lists
  (`spec.toml`, `tests.toml`, `autoschedulers.toml`) are `request_all`: every declared
  configuration, of which `--config` builds the selected ones. An unselected configuration is
  never searched or probed (`tests/test_variants.py`).
- **Exec settings follow CMake's single tree**: `exec_setting = [["native.opt", "inherit"],
  ["native.sanitize", "inherit"]]`, so the tools and generators of the debug build are debug and
  those of the asan build are instrumented, as in CMake. Without it core builds them release and
  uninstrumented (a second, release libHalide for the generators). This is an open policy
  question.
- **Debug against Release-only imports.** Homebrew's CMake exports have only
  `IMPORTED_LOCATION_RELEASE`. `package_config_map = [["debug", "release"]]` states what CMake
  does implicitly (its fallback to the first imported configuration), like
  `CMAKE_MAP_IMPORTED_CONFIG_DEBUG=Release`.
- **Scoped toolchain pins**: `toolchain_pin` rows are `(lang, exec, target, tc)`, one per
  language for arm64→arm64 and one for arm64→x86_64 (the x86_64 configuration and universal2's
  second slice).
- **Per-platform LLVM** (`halide_build.dl`): every LLVM fact is keyed by platform
  (`package_version("LLVM", p, …)`, `package_attr`, the relayed definitions under `on:<p>`). The
  version check relates the Clang a configuration **runs** (bound on its exec platform, through
  `tool_dep(t, c, "Clang::clang")`) to the LLVM it **links** (bound on its own platform), and it
  applies only where the runtime runs Clang. The `package_want`s of Clang and LLD are gone: they
  are demanded by use.

### Comparison (phases 1–6, per variant)

`compare_libhalide.py --variant V` compares against `baseline/cmake-V.jsonl` and rewrites the
path map's `arm64-macos.release.none` to the build's one configuration directory. All three
variants give the same table as release:

| variant | match | differ | only CMake | only buildlog |
|---|---|---|---|---|
| release | 870 | 1322 | 442 | 1 |
| debug | 870 | 1322 | 442 | 1 |
| asan | 870 | 1322 | 442 | 1 |

Per phase and kind the rows are identical too (e.g. phase 3: 846 compile differ, 861 custom
match, 15 copy differ; phase 6 as in the phase 6 table). The differences are phase 1–6's
categories, with one variant-specific change each:

- **release**: CMake `-O3`, buildlog `-O2 -DNDEBUG` (known since phase 1).
- **debug**: both have `-g` on the same 933 of 1137 compiles; buildlog adds `-O0` where CMake's
  Debug has no `-O` flag (so the phase 1 `-O3`/`-O2` pair becomes an only-buildlog `-O0`).
- **asan**: `-fsanitize=address` is on exactly CMake's edges: 729 compiles and 63 links on both
  sides (host tools and generators included, via the inherit policy); the runtime's 408
  `clang -emit-llvm` compiles have none on either side (the bitcode is target code, and CMake
  adds no asan feature to it); otherwise as release (`-O3` vs `-O2 -DNDEBUG`).

### Build and tests

| variant | ninja (2194 edges, cold, one run) | `buildlog test -j 4` | CMake (scope.md) |
|---|---|---|---|
| release | 50 s (phase 6) | 30 passed, 2 skipped, 0 failed | 30 / 2 / 0 |
| debug | 57 s | 30 passed, 2 skipped, 0 failed (88 s wall) | 30 / 2 / 0 |
| asan | 74 s | 24 passed, 2 skipped, 6 failed (99 s wall) | 24 / 2 / 6 |

The 6 ASan failures are CMake's, with the same causes (scope.md, oddity 12):
`correctness_bounds`, `callable_generator`, `extern_stage`, `image_io` and `serialization` fail
with `JIT session error: Symbols not found: [ ___asan_version_mismatch_check_v8 ]` (LLVM 22's
JIT instruments for ASan ABI v8, the process carries Apple clang 21's runtime);
`correctness_simd_op_check_arm` dies with `AddressSanitizer: BUS … in ___chkstk_darwin` (the
stack overflow). The 2 skips are the GPU tests. The builds' only warnings are ld's "built for
newer macOS" for Homebrew's libpng, libjpeg and (debug) wabt.

### universal2 and x86_64: graphs only

Homebrew's LLVM, LLD, wabt, flatbuffers, OpenSSL, libpng and libjpeg are arm64-only. Configure
writes the graph and exits 1 with attributed errors (x86_64: 20, universal2: 30), e.g.

```
error: imported file lacks the platform's architecture: no x86_64 slice of LLVM::LLVM: /opt/homebrew/Cellar/llvm@22/22.1.8/lib/libLLVM.dylib (x86_64-macos.release.none)
error: artifact lacks platform coverage: Halide in x86_64-macos.release.none needs /opt/homebrew/Cellar/llvm@22/22.1.8/lib/libLLVM.dylib on x86_64-macos
```

The first comes from core's `file_archs` probe on the imported file itself (new in phase 7a;
before, only consumers reported coverage), the second from the consumer. Every x86_64 action
of Halide (and of what links it) is an edge that prints these errors and fails, so `ninja
lib/x86_64-macos.release.none/libHalide.dylib` stops with them, never with a linker error.
`buildlog why 'error_at("imported file lacks the platform's architecture", …, "LLVM::LLVM",
‹x86_64-macos.release.none›)'` shows `import_artifact_in` (LLVM::LLVM relayed under
`on:x86_64-macos` from `LLVMExports.cmake`), the `LLVM:AArch64 → LLVM::LLVM` edge that needs
it, `platform_constraint("x86_64-macos", "cpu", "x86_64")`, `res_file_format(…, "macho")` and
`!artifact_covers(libLLVM.dylib, "x86_64-macos")`.

The tools and generators are arm64 exec instances: in the x86_64 graph all 52 generator and
runtime runs that produce x86_64 files run `bin/arm64-macos.release.none/…`, and the arm64 side
(664 C++ compiles, the 408 + 423 runtime steps, 18 links) is the exec libHalide and its tools,
which build. Structure against CMake's configure-only graphs:

| | CMake x86_64 | buildlog x86_64 | CMake universal2 | buildlog universal2 |
|---|---|---|---|---|
| tools, generators | x86_64 (run under Rosetta) | arm64 (exec) | libHalide, binary2cpp, build_halide_h, cost-model generators fat; `add_halide_generator` arm64 (#9469) | arm64 (exec), shared with the arm64 slice |
| libHalide | x86_64 | x86_64 (fails: coverage) + arm64 (exec) | fat | per slice + `lipo` (x86_64 slice fails) |
| generator runs | x86_64 targets | 28 GENERATOR + 22 GENRT, `target=x86-64-macos` | per slice (`-x86-64-macos` runs) | 28 + 22 per slice, then `lipo` (43 LIPO) |

buildlog's universal2 does per slice what #9469 does per `add_halide_library` by hand, for every
tool, from use (scope.md §5: the tools that do not go through `add_halide_generator` stay fat in
CMake). Neither graph links on this machine.

### Why does the runtime's Clang bind on arm64 in the x86_64 build?

```
buildlog why -C BUILD 'import_want("Clang::clang", "arm64-macos")' case-studies/halide/libhalide
import_want("Clang::clang", "arm64-macos")
  import_demand("Clang::clang", "arm64-macos")
    tool_ref("Halide", ‹x86_64-macos.release.none›, "out:gen/x86_64-macos.release.none/Halide/runtime/initmod.alignment_128_32.ll", "Clang::clang", "run")
      halide_build_rt_cc(…)  ← halide_build_runtime("Halide", …) ← spec.toml:34, needed("Halide", ‹x86_64-macos.release.none›) ← request_all
    exec_config(‹x86_64-macos.release.none›, ‹arm64-macos.release.none›)
    config(‹arm64-macos.release.none›)
  build_platform("arm64-macos")
  !import_bound("Clang::clang")
```

and `why 'import_select("Clang::clang", "arm64-macos", "llvm", ".../ClangConfig.cmake")'` ends at
`package_root("arm64-macos", "llvm", "dir", "/opt/homebrew/opt/llvm@22")` ←
`package_search_dir` (presets/homebrew-arm64.toml): the exec platform's search locations. `LLVM:OrcJIT` is
wanted on both platforms (x86_64 for the product, arm64 for the exec libHalide the generators
link); `Clang::clang` and `LLVM::llvm-as` only on arm64. Here both platforms search the same
directory (Homebrew has one LLVM); `tests/test_variants.py` shows different search directories
per platform binding different files for a tool and a library.

### Snapshots

`scripts/ninja_snapshot.py` against phase 6: `libhalide` (release), `cross`, `linear_blur`,
`bilateral_grid`, `collision` and `target_ids` are byte-identical. `bilateral_grid_coverage`
differs only in the failing edges' error text: the import's own coverage error is now
"imported file lacks the platform's architecture: no x86_64 slice of pc:libpng: …".

### Timings (configure, warm probe cache, single runs)

| | release | debug | asan | universal2 | x86_64 |
|---|---|---|---|---|---|
| phase 7a | 5.7 s | 6.6 s | 6.3 s | 7.2 s | 6.6 s |
| phase 6 (same machine, release) | 4.5 s | | | | |

Discovery takes one more round than in phase 6 (final round 5 vs 4): a name is now demanded on
a platform, and the platforms come from the inventory probe, so round 0 runs only the inventory
and the first package finds wait for round 1 (phase 6 ran them in round 0). Each round is a
Soufflé run, about 1 s here. Relayed names are searched too (order independence), so the final
round has 108 finds against phase 6's 28 (all cache hits on a warm configure).

### Rough edges (phase 7a log)

- **Inert selections.** A platform that shares a search root with one that wants a name also
  selects it (selection cannot read demand without a negation cycle); the relay conditions it on
  that platform, so it contributes nothing, but it shows in `import_select`.
- **LLVM ambiguity is per component**: two LLVMs report one error per used `LLVM:<comp>` name,
  and per platform.
- **Configuration-level errors break whole targets**: on x86_64 every action of Halide fails
  with the coverage errors, not only its link.
- **`config_select` has no origin** in proofs (it comes from the command line).

## Authoring abstraction pass (2026-09-29)

This pass exercises the generic authoring mechanisms, not the later vocabulary work. `libhalide/buildlog.toml` explicitly lists every spec input and the project-local `rules.dl`. The Halide rule package remains reusable policy; rules that only abstract this case study's sampled tests live in the project layer.

The rewrites are intentionally graph-preserving:

- `libhalide/headers.toml` uses `[target.Halide]` plus an ordered `halide_build_header` list. The loader fills the rank column from list order.
- `libhalide/rules.dl` introduces project-local relations for correctness tests, generator executables, AOT tests and autoscheduler modules. They derive the repeated target declarations, sources, default deps/properties, requests and test/group rows. Irregular helpers, `add_halide_library` variation and explanatory CMake correspondences stay as readable raw facts and comments.
- `libhalide/tests.toml` and `libhalide/autoschedulers.toml` use subject tables for per-test and per-autoscheduler variation. Because TOML cannot return to the top level after a table, those subject-table blocks are kept at the ends of the files.

Honest size counts for `headers.toml`, `tests.toml`, `autoschedulers.toml` and project `rules.dl` (active means nonblank, non-comment; authored rows/facts count TOML entries and project rule clauses): before 855 active lines / 1046 rows, after 952 active lines / 797 rows. `tests.toml` itself is 500 → 498 active lines and 660 → 390 rows. `correctness_bounds` spellings: 11 → 0; `generator_aot_alias`: 17 → 1.

Validation: configuring the release build before and after the rewrite produced identical `build.ninja` after normalizing the regeneration edge and build-directory path. The debug configuration also configures successfully, and `buildlog test` on the release build reports 30 passed / 2 skipped.

## Include-directory classes (2026-09-29)

Include-directory order: a compile's own `cc.include_dir`s are `-I`, every
dependency's (direct or transitive, in-project or imported) `-isystem`, then
`cc.system_include_dir`s `-isystem`; lexical within a class. The release build with the preset
(`presets/homebrew-arm64.toml`, Apple clang), before and after, on the same source:

- **716 of 1,625 argv-bearing edges changed, and only in the class of a dir**: no dir was added,
  removed or reordered, and no other flag changed. The dirs that moved from `-I` to `-isystem`
  (number of compiles): `include/<cfg>/flatc` 632, `src/runtime` 83, `include/<cfg>` (Halide.h) 65,
  `tools` 35, `test/common` 31, `src/autoschedulers/common` 24, and 30 generated-library dirs
  `gen/<cfg>/<lib>` (1 to 12 each). Imported dirs were already `-isystem`
  (`package_system_includes`), and keep their class and order after the in-project ones.
- **Against CMake** this is a new difference: CMake gives in-project dependencies' dirs `-I` and
  only imported targets' `-isystem`. E.g. libHalide's compiles are `-I $SRC/src -isystem
  $BUILD/include/flatc -isystem …` here, `-I $SRC/src -I $BUILD/include/flatc -isystem …` in CMake.
- **Warnings: 51 before, 51 after**, the same messages (17 `-Wunused-variable` in tests, the rest
  ld's deployment-target notes). No dependency header of this build warned in its consumers
  before, so nothing was suppressed here.
- `ninja` builds all 2,194 edges; `buildlog test -j 8`: 30 passed, 2 skipped (the GPU tests).
- **The runtime's `-Werror`** (`halide_build_runtime_flag(13, "-Werror")`, CMake's
  `RUNTIME_CXX_FLAGS`) is allowed since 2026-09-30: the author's `-Werror` check applies only
  where the user chooses the compiler, and the runtime's `Clang::clang` (and `llvm-as`) are
  tied to the LLVM libHalide links by `import_same_package("Clang::clang", "LLVM:OrcJIT")`
  (halide_build_runtime.dl), which core checks against the user's package selection.
  `buildlog why --flag -Werror --output gen/<cfg>/Halide/runtime/<m>.ll` shows the
  `action_arg_basis` "runs Clang::clang, from the same package as LLVM:OrcJIT
  (import_same_package: llvm dir /opt/homebrew/opt/llvm@22)", with the fact, the preset's
  `package_search_dir` and the find results (408 runtime actions).
- **`-Wshadow-header`**: Apple clang 21 rejects it, so the preset's build has none. A second
  build pinning `c`/`cxx` to Homebrew clang 22 (`BUILDLOG_LLVM_BIN=/opt/homebrew/opt/llvm@22/bin`,
  a copy of the preset with the two arm64→arm64 compile pins set to `llvm`) has it on all 729
  compiles, builds all 2,194 edges, and reports **0** shadowed headers. Clang only looks at
  non-system dirs, so this covers each target's own `-I` dirs. Rerunning the same 729 compiles
  with `-fsyntax-only` and the old classes (the moved dirs back to `-I`) also reports 0: Halide's
  include dirs do not collide (`include/<cfg>` holds only `Halide.h`, none of `src/runtime`'s
  headers). Not a pinned configuration of this study; not in the tests.


## UX pass port

The cheap UX-pass port only touches `libhalide/spec.toml`: the core `Halide`, `Halide_Runtime`, and three host tools now use `[target.*]` tables and a small `halide-tool` profile. The halide-build rule package, apps, autoschedulers, tests, and generated source abstractions are intentionally left in their existing form.

| file | before | after |
|---|---:|---:|
| autoschedulers.toml | 253 | 253 |
| bindings.toml | 15 | 15 |
| buildlog.toml | 14 | 14 |
| headers.toml | 3 | 3 |
| rules.dl | 74 | 74 |
| sources.toml | 191 | 191 |
| spec.toml | 101 | 99 |
| tests.toml | 544 | 544 |
| **total** | **1195** | **1193** |
| **Datalog** | **74** | **74** |

Graph check: `ninja_norm.py` showed only the expected build-directory path embedded in the `build_halide_h` host-tool command (`scratch/old-halide` vs `scratch/new-halide`); no semantic edge/argv change was introduced. Tests: `BUILDLOG_HALIDE_PREFIX=<install> uv run --frozen pytest -q tests/test_ggml_case.py tests/test_spirv_tools_case.py tests/test_halide_phase*.py` passed in the combined run (50 passed, 2 skipped).

Vocabulary gaps left here are unchanged: the sampled Halide test/generator/AOT patterns still need project Datalog; the generated-runtime/flatc/host-tool abstractions remain in halide-build; ordered include/link arguments and source groups are still outside this cheap port.

## One library kind (step 4)

Built libraries have one kind, `library`; a fixed linkage is a requirement with a reason.

- `Halide` was `shared_library`. Upstream honours `BUILD_SHARED_LIBS` (default on), so it is
  `library` with `default_linkage = "shared"` and no requirement; a user may build it static.
- `Halide_GenGen` and `Halide_ASLog` require static, with upstream's reasons (both are
  `add_library(... STATIC ...)`: GenGen carries the generator `main`, ASLog is linked into
  each autoscheduler plugin).
- The tests' externs libraries are `library` with no requirement.
- halide-build's generated filter libraries and runtime require static (a generator emits a
  `.a`; the runtime is linked once per program). Its manifest extends `linkage_require`.

The graph is identical for libhalide and the sampled tests (snapshot diff empty).

| Change | File | Before | After |
|---|---|---:|---:|
| `Halide`: `library`, `default_linkage = "shared"` | libhalide spec.toml | 99 | 100 |
| GenGen, ASLog: static requirement with reasons | autoschedulers.toml | 249 | 253 |
| | project total | 1189 | 1194 |
| generated libraries and runtime: `library` plus a static requirement | halide_build_generator.dl | 194 | 198 |

## Linux (GCC 13 and LLVM 22, tier 2)

libHalide builds and tests on arm64 Ubuntu 24.04 in the single image of
[`linux/`](../../linux/README.md) (`linux/run.sh cases gcc halide`), against scenario 7b's GCC 13 CMake ground truth
(`baseline/cmake-linux-gcc-release.jsonl`, recorded by `docker/run-cmake-linux.sh`).
`libhalide/presets/linux-gcc-arm64.toml` is the Linux counterpart of `homebrew-arm64.toml`:
GCC pins, apt.llvm.org's LLVM at `/usr/lib/llvm-22`, libpng, libjpeg and OpenSSL by
pkg-config, flatbuffers and wabt from `/usr/local`, the SPIRV-Headers in the submodule.
The Linux overlay `llvm-install/linux-22` is the LLVM 22 package for the image (its manifests import
`/usr/lib/llvm-22`'s exports).

| | macOS (Homebrew, Apple clang) | Linux (GCC 13, LLVM 22) |
|---|---|---|
| Build | 2194 edges | 2194 edges, none fail |
| `buildlog test` (32-test sample) | 30 passed, 2 skipped | 30 passed, 2 skipped (the two GPU tests) |
| Graph comparison, phases 1–6 | 870 match, 1322 differ, 442 only CMake, 1 only buildlog | the same four numbers |

The differing records are the same classes on both hosts (the compiler driver's name
`c++` against `g++`, `-isystem` against `-I` for the generated include directories, the
optimization flags buildlog states explicitly, object and library layout), and the 442
CMake-only records are CMake's phony targets. The comparison runs through
`phase1/compare_libhalide.py BUILD --variant linux-gcc-release` (LLVM root
`/usr/lib/llvm-22`, `--llvm` overrides it); the path map's configuration name is rewritten
to `arm64-linux.release.none` as for the other variants.

Measured on an Apple-silicon host, `--cpus 8`, `ninja -j8`, a fresh container (cold: no ccache, no prior objects), host load 1-minute 2.8 before and 4.0 after: configure 4 s, build of 2194 edges 90 s (the case-study slice: libHalide and its runtime, the build tools, the Adams2019 and Anderson2021 autoschedulers, and the 32-test sample with its generators; not Halide's full test suite, apps, Python bindings or tutorials), 32-test sample 64 s, comparison 2 s (about 2.7 minutes in all). An earlier run on a busy host took 134 s to build; two manual builds took about 17 minutes of wall time for the same 11 CPU-minutes, with the host load unrecorded and so unexplained; treat build time as host-dependent.

What Linux needed, and where it went:

- **Conditions, not edits of the macOS spec.** `-Wl,-headerpad_max_install_names` and
  `-Wl,-search_paths_first` apply on `platform.os == macos` only; the Clang-only warnings
  (`-Wvla-extension`, `-Winconsistent-missing-override`,
  `-Winconsistent-missing-destructor-override`) apply to the `clang` family and GCC gets
  `-Wsuggest-override`, as upstream's `$<CXX_COMPILER_ID:...>` does (`libhalide/rules.dl`). The
  two ENABLE_EXPORTS tests (`correctness_extern_stage`, `correctness_callable_generator`)
  link with `-Wl,--export-dynamic -rdynamic` on Linux; Mach-O executables export already.
  `example_cpp` depends on `example.runtime`, which GNU ld needs for the static link order
  (ld64 does not care).
- **Export list per object-file format (core).** libHalide's `exported_symbols.ldscript` is
  stated beside the `.osx` list (`native_version_script`); the GCC and Clang drivers spell it
  `-Wl,--version-script=`. See "Export lists per object-file format" in
  `docs/spec-reference.md`.
- **CMake export reader.** CMake comments out one of the two check lines of an export block
  (`#list(... _cmake_import_check_targets ...)` for libraries, the files line for
  executables), and apt.llvm.org's LLVM ships only `*-relwithdebinfo.cmake`; the reader
  accepts the first and applies RelWithDebInfo and MinSizeRel blocks where no
  Debug/Release block is mapped, as it does NOCONFIG.
- **Object-file reader (core).** glibc's `libm.so` is a GNU ld script and `libdl.a` an empty
  archive; both are read (`READER_VERSION` 2) instead of failing coverage.

Not covered on Linux: Clang as the host compiler (not run since the pin moved past
#9548, which fixed the `getauxval` noexcept error that Clang 18 hit; see the 7c notes), the Debug, ASan, x86_64, universal2 and cross
variants, and the Homebrew-specific phase tests (`tests/test_halide_phase*.py` skip without
`/opt/homebrew`; Linux is exercised by `linux/cases.sh` and the comparison above).

## Phase 7c on buildlog: the cross build, built and tested (one tree)

macOS arm64 builds libHalide and the 32-test sample for aarch64-linux-gnu against the 7b sysroot
(Homebrew llvm@22 clang and lld@22), runs the generators on the host, and runs the tests in the
`buildlog-halide-linux:22` container. CMake needed two trees and six workarounds
([docker/README.md](docker/README.md)); buildlog needs one tree and no workaround. (Before #9548, at
`f370931c79`, each needed one more, for the upstream `getauxval` bug.)

```sh
docker/run-buildlog-cross.sh            # setup configure build test; logs in build/halide-cross/
BUILDLOG_HALIDE_CROSS=1 uv run --frozen pytest tests/test_halide_cross.py   # the same, as a test
```

The configuration is [libhalide/presets/cross-linux-aarch64.toml](libhalide/presets/cross-linux-aarch64.toml):
`config_decl` arm64-linux, the sysroot and linker (`cross_sysroot`, `cross_linker`), the
launcher that runs Linux tests (`platform_runs_on`), and per-platform package locations.
Host tools and generators are built with the macOS toolchain *because they run during the
build* (execution-platform inference); nothing says so.

### Result

| | buildlog | CMake 7c |
|---|---|---|
| build trees | **1** | 2 |
| workarounds | **0** | 6 (+3 standard rows) |
| tests | **30 passed, 2 skipped, 0 failed** | 30 passed, 2 skipped |
| skipped | `correctness_multi_gpu_gpu_multi_device`, `correctness_gpu_allocation_cache` | same |
| libHalide | `libHalide.so.22.0.0`, ELF aarch64 | same |
| generators/host tools | Mach-O arm64 (same tree) | Mach-O arm64 (tree 1) |

Timings (single run, shared machine, load average 17–67, so upper bounds): configure 7 s;
`ninja -j8` 123 s; `buildlog test -j6` 165 s, which builds the test files then runs them (summed
test time 221 s, dominated by `correctness_simd_op_check_arm`, 64 s). CMake: 6.4+4.9 s configure,
44+87 s build, ~66 s ctest, measured on a quieter machine, so not directly comparable.

### Effort ledger (buildlog)

| # | What | Class |
|---|---|---|
| 1 | `config_decl`/`config_default` for arm64-linux | fact (target platform) |
| 2 | `cross_sysroot`, `cross_linker` | fact (the sysroot and linker of the target; the pair is stated, not probed) |
| 3 | `platform_runs_on` → `docker/cross/run-in-container.sh` | fact (how Linux binaries run; CMake's emulator, plus a hack, needed it for generators too, here only tests) |
| 4 | `toolchain_pin` arm64-macos → apple-clang | fact/preference (which compiler builds the host tools; could be inferred) |
| 5 | per-platform package locations (FlatBuffers, WABT, pkg-config, LLVM/Clang/LLD exports for macOS and for the sysroot, `package_namespace`, `package_file_install`) | fact (where the dependencies are on each platform) |
| 6 | `-D HALIDE_SRC`, `-D SYSROOT`, `BUILDLOG_LLVM_BIN` | fact (paths) |
| – | *(history)* an `auxv` group, `-includesys/auxv.h` for `Target.cpp` on Linux/clang | was a workaround for Halide#9480, the same as CMake's row 10; fixed upstream in #9548, removed at pin `4e272cbfcc` |
| – | second tree, toolchain file, host-twin emulator hack, shadow packages, per-target host/target split | **none** |

### Core and plugin changes (each justified)

- `L2_cross.dl` (buildlog-cpp): a clang toolchain can target Linux from another OS given a
  sysroot (`--target`, `--sysroot=`, `--ld-path=`). No probes are run for that pair
  (`cross_mismatch`), because probing a foreign target on the host would mis-describe it.
- Core `L3_discovery.dl`: (a) `import_same_package` holds across platforms when both
  targets' packages state the same version, so one `LLVM::LLVM` import serves the host
  and the target (the two installs are the same package at different paths); (b) new input
  `package_file_install(file, prefix)`: files of one install share an origin, so Clang
  and LLVM exports count as one package (before, each file was its own origin).
- `llvm-install`: `LLVM.dl`/`LLD.dl` gained platform-keyed facts (`llvm_define_on`, etc.) and an
  overlay `cross-22` is used because rule packages load once per name, and the macOS and Linux
  overlays share names.
- Lock hashes and `test_why` goldens refreshed (counters and rule numbers only).

### Comparison with the CMake-built product

Test outcomes, skip set, and the product file (libHalide ELF) agree. The normalized graph
comparison (`phase1/compare_libhalide.py --config arm64-linux.release.none --sysroot …`;
output in `build/halide-cross/compare-all.txt`) pairs the product edges one-to-one by
output id, but argv matches are few: the differences are systematic and flag-level, not
missing work.

- `-isystem` (buildlog) vs `-I` (CMake) for build-tree and runtime include directories;
  CMake's `@…modmap` (C++20 module scanning) is absent in buildlog.
- buildlog adds `-Wshadow-header -Wno-error=shadow-header` (its default for upstream clang).
- buildlog adds `-isystem $SYSROOT/usr/local/include` and `-isystem /usr/include`; the
  latter comes from a pkg-config file's absolute `includedir` not being sysroot-prefixed
  (harmless here, known gap).
- The libHalide link line differs in order: buildlog puts `libLLVM.so.1`, lld and
  flatbuffers in a different order and embeds `-Wl,-rpath,$SYSROOT/usr/lib/llvm-22/lib`
  (CMake: `/usr/lib/llvm-22/lib`). The rpath is a real difference: it works in the test
  container only because the volume is mounted at the same path.
- The comparison tool was written for the macOS builds; its cross adaptation only filters
  to one configuration and adds the sysroot root, so the argv-level result is a diagnostic,
  not a fidelity claim.

### Open items

pkg-config `includedir` and the ELF rpath should be sysroot-relative; the platform-keyed LLVM
facts are a convention, not yet a core feature; the `LLVM:Config` interface target still reads
global facts; the `toolchain_pin` could probably be inferred. The 7c run needed the sparse
case-sensitive volume (`Elf.h` vs `<elf.h>`), as CMake's did.

### Sysroot leaks fixed (follow-up)

Two leaks of the host's sysroot path into the product, now closed as rules, not project facts:

- **rpath.** libHalide's runpath was `$SYSROOT/usr/lib/llvm-22/lib` (a host path); it is now
  `/usr/lib/llvm-22/lib`, as CMake's. Core
  gained the open relation `platform_sysroot(p, root)` (buildlog-cpp states it from
  `cross_sysroot`); an imported file under a sysroot is imported with that root, and the CMake
  importer maps a path under it to its path on the target. The test run now mounts the volume
  at a different path in the container (`BUILDLOG_XVOL_MOUNT`) and passes 30/2. That run cannot
  by itself fail with the old rpath, since the image's loader finds `/usr/lib/llvm-22/lib` anyway;
  the rpath is checked directly (`tests/test_halide_cross.py`, `tests/test_importers.py`).
- **pkg-config.** A `.pc` under a sysroot has its absolute `-I`, `-L` and library paths prefixed
  with the sysroot (pkg-config's `PKG_CONFIG_SYSROOT_DIR`; a path already under it is kept), so
  libpng's `includedir=/usr/include` is no longer the host's `/usr/include`. Two consequences,
  both principled: a linker script in a sysroot (`libm.so`) names files in the sysroot, so
  the `file_archs` probe resolves them there; and the sysroot's own `/usr/include` (and its
  multiarch directory) are the clang's implicit include dirs for that pair (it is not probed),
  so they are not named again as `-isystem` (which broke `#include_next <stdlib.h>`).

Is the Apple-clang `toolchain_pin` inferable? No: Apple clang and Homebrew clang (`llvm`, a
candidate because the cross build needs it) both build for macOS, so choosing the compiler for
the host tools is a genuine ambiguity; it stays an error ("ambiguous toolchain (add a
toolchain_pin)"), and all three pins (c, cxx, native-link) are needed. It is a preference,
stated once in the preset.

Timings, after the follow-up (shared machine): configure 37 s (loaded), build 136 s,
tests 162 s, 30 passed, 2 skipped (historical run).

## Breadth: the whole test suite, the autoschedulers, the Python bindings and the tutorials

libHalide's build is extended to what Halide's own CMake build tests and ships on macOS arm64
(Release, Homebrew llvm@22, Python 3.10 with numpy, imageio and pybind11): every ctest test
the CMake build defines, except nine named below (eight at the earlier pin), runs from one buildlog configuration.

```sh
# the plain tree and the Python tree (the second adds presets/python.toml)
buildlog configure case-studies/halide/libhalide -C build/hl   --package-path case-studies/halide/packages --user …/presets/homebrew-arm64.toml
buildlog configure case-studies/halide/libhalide -C build/hlpy --package-path case-studies/halide/packages --user …/presets/homebrew-arm64.toml --user …/presets/python.toml
buildlog test -C build/hlpy -j 6 --json results.json
```

### Counts (passed / skipped / failed)

CMake: `ctest -j6` over the 950 tests of `build/cmake-release` (Halide at `4e272cbfcc`; 916 tests and 824 / 92 / 0 at `f370931c79`, see Repin to 4e272cbfcc),
reconfigured with the same Python 3.10 environment buildlog uses. buildlog: `buildlog test -C
build/hlpy`, 942 listed tests (one is listed twice, below), 0 not run (at `f370931c79`: 909 listed, 819 / 90 / 0). Areas are test-name prefixes; Halide's `test/error` directory
does not exist at this pin, so the error-reporting tests are the `correctness_*` tests that
check diagnostics (`correctness_*_errors`, `correctness_error_macro_unreachable`, …).

| area | buildlog | CMake |
|---|---|---|
| correctness | 591 / 70 / 0 | 591 / 70 / 0 |
| performance | 30 / 8 / 0 | 30 / 8 / 0 |
| generator (AOT, `generator_*`) | 86 / 12 / 0 | 86 / 12 / 0 |
| warning | 7 / 0 / 0 | 7 / 0 / 0 |
| fuzz | 7 / 0 / 0 | 7 / 0 / 0 |
| runtime (`runtime_*`) | 7 / 0 / 0 | 7 / 0 / 0 |
| other (`registration_test`, `rungen_test`, `test_perfect_hash_map`, and the new `sever` test; CMake adds `filter_dll_exports`) | 4 / 0 / 0 | 5 / 0 / 0 |
| autoschedulers (Adams2019, Li2018, Mullapudi2016, Anderson2021) | 31 / 2 / 0 | 31 / 2 / 0 |
| tutorials (C++) | 25 / 0 / 0 (24 distinct) | 26 / 0 / 0 |
| Python (correctness, tutorials, apps, runtime, symbols) | 60 / 1 / 0 | 64 / 1 / 0 |
| debugger | 1 / 0 / 0 | 1 / 2 / 0 |
| **total** | **849 / 93 / 0** (942 listed, 941 distinct) | **855 / 95 / 0** (950) |

Skips are the same tests on both sides: GPU backends, Hexagon, WebAssembly runtimes absent from
the machine, the Python `extern` test, and the two GPU autoscheduler demos (Anderson2021).
The plain tree (no interpreter) runs 756 / 89 / 0 (845 tests): the same sets without the
Python area and `debugger_natvis_xml`. The Python tree lists `tutorial_lesson_10_aot_compilation_generate`
twice, once per configuration it is built in (the Python tree's exec configuration also builds it), so
25 tutorials are listed for 24 distinct ones.

Flaky on the CMake side in an earlier run, passing in this one: `mullapudi2016_reorder`,
`tutorial_lesson_15_build_gens`. In that earlier run its Python tests failed (the Python it
found had no numpy or imageio); the table is the rerun.

### What was added (buildlog)

- Tutorials: 24 C++ lessons plus the AOT lessons 10, 15, 16 and 21 (a generator run into an
  object library; four and two libraries, the `Mullapudi2016`-scheduled one, and the run
  executables); `tutorials.{dl,toml}`.
- Python bindings: the `halide_` and `_runtime` modules; the correctness fixtures (generator
  stubs, AOT extension modules that export only `PyInit_<module>`, user-context and
  multi-method modules); the 25 Python tutorials; the 5 Python apps with 11 generated
  libraries; the standalone runtime's `load_aot` and `call_convention` tests; the symbol-table
  checks `test_export_single_symbol` and `python_runtime_no_libhalide`;
  `li2018_gradient_autoscheduler_test_py`; `debugger_natvis_xml`.
- Autoschedulers and the generator, correctness and performance suites came in earlier phases;
  here they are only re-counted.

### Not modelled (nine tests; CMake runs all nine)

| tests | why |
|---|---|
| `tutorial_lesson_15_build_gens`, `tutorial_lesson_15_check_files` | CMake drives a nested build and a directory listing from inside ctest, and lesson 15's Windows x86-32 target and STMT output. buildlog builds the lesson's libraries as targets; there is no test to run, and the win32 target is outside the host and `cmake` target kinds the library package supports. |
| `python_tutorial_lesson_10_compile`, `_15_build_gens`, `_16_compile`, `_21_compile` | The same pattern: ctest invoking `cmake --build` to produce the generated modules. Here the modules are ordinary targets (`py_lesson_10_halide`, `python_ext_*`, `python_ext_auto_schedule_*`), built before the lesson's run test. |
| `filter_dll_exports` | New (#9489): a `cmake -P` script test of the MSVC DLL-export filter (`cmake/FilterDllExports.cmake`); MSVC-only logic, nothing for a build graph on macOS. |
| `debugger_lldb`, `debugger_gdb` | CMake runs both and skips both (`[SKIP]`: Release libHalide has no debug info). They need an unoptimized fixture executable and a debugger driven by a harness; the same skip would result here. |

2 + 4 + 2 + 1 = 9; 950 - 9 = 941 distinct tests, the number buildlog lists (the ninth, `filter_dll_exports`, is new at `4e272cbfcc`). `python_correctness_extern`
is modelled and skips on both sides.

### Workarounds (named and justified)

| # | What | Why | What would remove it |
|---|---|---|---|
| 1 | Python script generators run as `env PYTHONPATH=<tree> <interpreter> <script>` (a launcher: words 0–3 of the action) | Core actions carry no environment, and the exec configuration (where generators run) has `python.interpreter = none`. 27 actions use it, from two rules (`python.dl`'s launcher for the libraries, `python_tutorials.dl` for lesson 10's generate step). | An action environment (`action_env(a, key, value)`, the counterpart of `test_config_env_path`), and the interpreter reachable from the exec configuration. With both, the launcher relations go away. |
| 2 | Python fixture extensions are staged in the package tree root `out:python/<cfg>/` | CMake puts each in its target's directory and sets `PYTHONPATH` per test; one tree on `PYTHONPATH` keeps the test environment a single path. | Not a defect; a layout choice. |
| 3 | Lesson 10's prebuilt `.a` is wrapped as an `object_library` | A `library` target would re-archive the generator's archive (nesting it). | A way to say "this file is the target's product" (an imported-from-action library). |
| 4 | Python tests write to the test's own directory (`TEST_TMPDIR`, relative output) | CMake's tests write in the binary directory. | None needed. |
| 5 | Symbol-table and XML checks (`test_export_single_symbol`, `python_runtime_no_libhalide`, `debugger_natvis_xml`) are inline interpreter programs | CMake writes a shell line and a `cmake -P` script around `nm` and `otool`; the same inspections run as `python -c` with `nm`/`otool`/`objdump`. These tests run where an interpreter is configured (CMake: where it finds Python 3). | A test kind that runs a named tool. |
| 6 | *(removed)* Halide#9480 (`auxv`) | Linux-clang cross build only; the same as CMake's. Fixed upstream in #9548, workaround removed at pin `4e272cbfcc`. | – |
| 7 | Machine-local LLVM and LLD hashes in `libhalide/buildlog.lock` | The lock pins the content of the installed LLVM's CMake export files (`LLVMExports.cmake` is a listed import of the package), so it records the toolchain on the machine that refreshed it. `tests/test_locks.py` does not check them (the LLVM package resolves only through discovery); a configure on another machine rewrites the two entries and nothing else. | Not pinning imported files of system packages would lose the guard that a different LLVM yields a different lock; that choice is the core's, not this study's. |

None of these changes what is built or run; none is a patch to Halide.

### Core and plugin changes

- Core: `test_cwd(name, dir)`, the working directory of a test as a rooted directory (`src:` or
  `out:`), at most one per test; `Python::Interpreter` and the fourth config setting
  `python.interpreter`; `configure_file` and `configure_file_text` are open; `module_library`
  and `module_spell` are exported. Tests: `tests/test_tests.py`. `tests/test_lean_core_rules.py`
  passes with these changes (no transcribed rule is touched).
- buildlog-cpp: module export-list spellings (`version_script` for GNU, `exported_symbols` for
  clang).
- buildlog-python: top-level extensions (`python_extension(t, "", m)`) and `python_extension`
  open.
- halide-build: launcher relations, `python_extension` output for libraries, `halide_build_pyext*`
  (`add_halide_python_extension_library`), `halide_build_test_plain`.

### Open

- The eight tests above.
- Anderson2021's GPU demos skip on this machine (no GPU target); CMake the same.
- Linux Python build, and Python on the cross build, are not configured.

## Repin to 4e272cbfcc

Upstream PR halide/Halide#9548 (the fix for #9480, the `getauxval` noexcept include-order problem
in `src/Target.cpp`) merged on 2026-10-13 as `4e272cbfcc`. The submodule moved from `f370931c79`
(17 upstream commits, 132 files). History: the port carried `source_group "auxv"` with
`-include sys/auxv.h` on `Target.cpp` (rules.dl), and the CMake baselines carried
`CMAKE_PROJECT_Halide_INCLUDE=cross/halide-project-include.cmake`. Both are removed; `Target.cpp`
now includes `<sys/auxv.h>` before `runtime/*`.

Upstream changes adapted (each in the TOML, not the rules, except the first):

| change | adaptation |
|---|---|
| #9548 `Target.cpp` include order | `rules.dl`: auxv source group, condition and comment removed. CMake cross: the project-include file and its docker README row 10 removed (6 workarounds remain, 7 before). |
| #9416 struct types | `src/LowerStructTypes.{cpp,h}` in `sources.toml` and `headers.toml` (libHalide compiles 187 -> 188); `struct_type.py` in `python.toml`; correctness test `gpu_struct_type`. |
| #9409, #9460, other new tests | 21 correctness tests, 4 `rfactor_aligned_split*` and the two `split_storage*` performance tests in `suites.toml`; runtime test `device_buffer_utils` in `tests.toml`. |
| #9484 `sever`, #9534 lost wakeup | generators and AOT libraries `sever`, `sever_dequantize`, `thread_pool_lost_wakeup` in `generators.toml`. |
| #9489, #9482 (MSVC) | none: MSVC-only CMake (export filter, `/INCREMENTAL:NO`); `filter_dll_exports` is the ninth unmodeled test. |
| `halide_ir.fbs` | none: flatc flags unchanged, the generated header matches CMake's. |

Results at `4e272cbfcc` (macOS arm64, Release, same commands as above):

- Native: `buildlog test -C build/hlpy -j 6`: 849 passed / 93 skipped / 0 failed (942 listed, 941
  distinct, 0 not run). Before: 819 / 90 / 0. The 33 added tests are 30 passed and 3 skipped
  (`correctness_gpu_struct_type`, `performance_split_storage*`: no GPU, same skips on CMake).
- CMake `ctest -j6`: 855 / 95 / 0 of 950 (824 / 92 / 0 of 916 before); 950 - 9 unmodeled = 941.
- Cross build (macOS to Linux/AArch64, `docker/run-buildlog-cross.sh`, no workaround): 30 passed /
  2 skipped / 0 failed; the CMake cross tree configures, builds and passes its 32 sampled tests
  (30 / 2 / 0) without the project-include file. Logs go to `build/halide-cross/`. The script no
  longer builds `all` by default and passes the CMake run's test regex to `buildlog test`:
  the port now includes tutorials, whose generators have no host twin in the cross scope
  (`WITH_TUTORIALS=OFF` on the CMake side), so `all` fails at `lesson_10_aot_compilation_run`.
- Spec size (`historical specification-size accounting`): CMake 5,293 -> 5,361 (project 3,729 -> 3,797 from the MSVC
  export block, the `filter_dll_exports` test and its 26th test file; helpers 1,564 unchanged);
  buildlog 2,259 -> 2,266 (TOML 1,438 -> 1,449, rules 254 -> 250, plugin 567).
- Baselines regenerated at the new pin: release, debug, asan, x86_64, universal2 and the cross
  graph. Not regenerated: `cmake-linux-gcc-release.jsonl` and `cmake-release-install.jsonl`
  (the Linux and install variants were not rerun; they still record `f370931c79`).
