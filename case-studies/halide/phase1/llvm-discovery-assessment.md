<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# LLVM discovery for the Halide case study: feasibility assessment

Status: assessment (M3, phase 1). This document adds nothing to `src/` or
`plugins/`. The prototypes live next to it:

| File | What it is |
|---|---|
| `llvm-probe/CMakeLists.txt` | A generic find-module probe project. It dumps the reached imported-target closure and the matching variables as JSON. |
| `option1_cmake_probe.py`, `inputs/*.json` | Option 1: toolchain file, probe configure with `--trace`, and dump → importer rows. |
| `option1-determinism.sh` | Eight scenarios for which LLVM the probe picks. |
| `option2_llvm_config.py` | Option 2 (llvm-config), the existing importer on the export files, and `--hybrid`. |
| `config_values.py` | A static reader for configured `set(NAME "literal")` lines in `*Config.cmake`, used by the hybrid. |
| `hybrid/halide_llvm.dl` | FindHalide_LLVM's derivation as a Soufflé program. |
| `llvm-probe-summary.json` | A trimmed ground-truth dump: the reached targets, edges, variables and genex census. |

Large outputs go to `raw/`, which is gitignored.

Everything below was run on this machine: macOS arm64, CMake 4.4.3,
Homebrew `llvm@22` 22.1.8 and `lld@22` 22.1.8, stock Soufflé 2.5. Times are
wall-clock from single runs, or the range over 2–3 runs. **Estimates are
marked as estimates.**

## 1. Ground truth: what FindHalide_LLVM builds

The probe is `find_package(Halide_LLVM 22 REQUIRED COMPONENTS WebAssembly X86
OPTIONAL_COMPONENTS AArch64 ARM Hexagon NVPTX PowerPC RISCV)`, called with
`Halide_LLVM_ROOT=/opt/homebrew/opt/llvm@22` and Halide's `cmake/` on the
module path. This is the call at Halide `CMakeLists.txt:194-198` with the
baseline's cache.

**Imported targets after the call:** 450 in total. By type: 306 STATIC, 116
EXECUTABLE, 14 SHARED, 10 INTERFACE, 3 UNKNOWN and 1 MODULE. The UNKNOWN
ones are ZLIB, zstd and LibXml2, which LLVMConfig's own find_package calls
create.

**Reached from the roots:** the roots are `Halide_LLVM::*`, `clang` and
`llvm-as`. The walk follows `INTERFACE_LINK_LIBRARIES` and
`IMPORTED_LINK_DEPENDENT_LIBRARIES_<CFG>`, and reaches **14 targets over 14
edges**:

```
Halide_LLVM::{X86,AArch64,ARM,Hexagon,NVPTX,PowerPC,RISCV} ──ILL──► Halide_LLVM::Core ──ILL──► LLVM (SHARED)
Halide_LLVM::WebAssembly ──ILL──► Halide_LLVM::Core
                         ──ILL──► lldWasm (SHARED) ──ILDL_RELEASE──► lldCommon, LLVM
                         ──ILL──► lldCommon (SHARED) ──ILDL_RELEASE──► LLVM
clang (EXECUTABLE, bin/clang-22)        llvm-as (EXECUTABLE)          [roots, no edges]
```

ILL is `INTERFACE_LINK_LIBRARIES`. ILDL is `IMPORTED_LINK_DEPENDENT_LIBRARIES`.

| Target | Kind | Where the facts come from |
|---|---|---|
| `Halide_LLVM::Core` | INTERFACE, created by Halide | FindHalide_LLVM.cmake:133-163. The defines are `__STDC_{CONSTANT,FORMAT,LIMIT}_MACROS;LLVM_VERSION=221`, the include dir is `$LLVM/include`, `INTERFACE_CXX_RTTI=ON` with `COMPATIBLE_INTERFACE_BOOL CXX_RTTI`, and ILL is `LLVM`. |
| `Halide_LLVM::<8 comps>` | INTERFACE, created by Halide | :166-180. ILL is `Halide_LLVM::Core`. WebAssembly adds include `$LLD/include` and ILL `lldWasm;lldCommon`. |
| `LLVM` | SHARED, imported | LLVMExports-release.cmake: `IMPORTED_LOCATION_RELEASE $LLVM/lib/libLLVM.dylib`, `IMPORTED_SONAME_RELEASE @rpath/libLLVM.dylib`. |
| `lldWasm`, `lldCommon` | SHARED, imported | LLDTargets-release.cmake: location, soname, ILDL_RELEASE. |
| `clang`, `llvm-as` | EXECUTABLE, imported | ClangTargets-release.cmake and LLVMExports-release.cmake: location only. |

**Generator expressions:** **none of the 14 reached targets has any.**
Across all 450, 44 targets have genexes. Every one is `$<LINK_ONLY:…>` in
`INTERFACE_LINK_LIBRARIES` (90 occurrences). They sit on LLVMInterpreter,
LLVMJITLink, LLVMOrcJIT and the clang-tools-extra libraries (clangTidy*,
clangd, …).

**Two surprises in the ground truth:**

1. The generated export files carry **no include directories and no
   definitions** for LLVM or LLD. Every compile-side fact comes from the
   hand-written, configured `LLVMConfig.cmake` or `LLDConfig.cmake`, or from
   Halide's own module:
   - `LLVM_DEFINITIONS` :357, `LLVM_INCLUDE_DIRS` :340, `LLVM_ENABLE_RTTI`
     :279, `LLVM_LINK_LLVM_DYLIB` :30, `LLVM_TARGETS_TO_BUILD` :36;
   - `LLD_INCLUDE_DIRS` LLDConfig.cmake:16.

   In each case the value is a literal `set()` at top level, apart from the
   install-prefix idiom.
2. LLVMConfig.cmake's prefix goes through `REALPATH`. So all paths come out
   as `/opt/homebrew/Cellar/llvm@22/22.1.8/…`, not `/opt/homebrew/opt/llvm@22`,
   even though the user named the `opt` path.

Configuring also runs environmental probes inside LLVMConfig: a
`try_compile` for `HAVE_FFI_CALL`, and find_package for FFI, LibEdit, ZLIB,
zstd and LibXml2. It prints 138 CMake policy warnings.

## 2. The minimal fact list, traced to the baseline

The baseline is `baseline/cmake-release.jsonl`. `$LLVM` is the normalized
llvm@22 prefix, and `$LLD` is `$HOMEBREW/Cellar/lld@22/22.1.8`. These are
all the LLVM-derived facts in the in-scope graph. No phase other than 1, 2,
3 and 5 mentions LLVM.

| # | Fact buildlog must know | Shows up in the baseline as | Where CMake gets it |
|---|---|---|---|
| F1 | LLVM version 22.1 | `-DLLVM_VERSION=221`: 194 compiles (187 phase 1, 5 phase 2, 2 phase 5) | FindHalide_LLVM:152 from `LLVM_VERSION_{MAJOR,MINOR}` (LLVMConfig:13-14) |
| F2 | LLVM definitions | `-D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS`: same 194 | LLVMConfig:357, minus `_GLIBCXX_ASSERTIONS` (:150-152) |
| F3 | LLVM include dir | `-isystem $LLVM/include`: same 194 | LLVMConfig:340 → FindHalide_LLVM:153 |
| F4 | LLD include dir, *before* F3 | `-isystem $LLD/include`: same 194 | LLDConfig:16 → FindHalide_LLVM:177 (WebAssembly is the first component) |
| F5 | Found components | `-DWITH_{WEBASSEMBLY,X86,AARCH64,ARM,HEXAGON,NVPTX,POWERPC,RISCV}`: 194. Also gates the Windows ARM/AArch64 runtime modules (`src/runtime/CMakeLists.txt:214-216`). | `Halide_LLVM_COMPONENTS` ∩ `LLVM_TARGETS_TO_BUILD`, plus the lld check for WebAssembly |
| F6 | LLVM built with RTTI | `-DHALIDE_ENABLE_RTTI`: 260. This is Halide's own option, whose default DEPENDS on it; the plain `INTERFACE_CXX_RTTI` adds no flag. | LLVMConfig:279 |
| F7 | Shared LLVM | the libLLVM link item (1 link, phase 1) instead of per-component static libraries | `Halide_LLVM_SHARED_LIBS` (FindHalide_LLVM:30-39) from `LLVM_LINK_LLVM_DYLIB` and WebAssembly being built |
| F8 | libLLVM location | `$LLVM/lib/libLLVM.dylib`, on the link line and as an implicit input | LLVMExports-release.cmake:2742 |
| F9 | lld libraries | `$LLD/lib/liblldWasm.dylib $LLD/lib/liblldCommon.dylib`, in that order, before libLLVM | LLDTargets-release.cmake |
| F10 | rpaths | `-Wl,-rpath,$LLD/lib -Wl,-rpath,$LLVM/lib` | CMake's generator, from the imported SHARED locations |
| F11 | clang for the runtime | `$LLVM/bin/clang-22`: 408 phase-3 commands | ClangTargets-release.cmake (`clang`) |
| F12 | llvm-as for the runtime | `$LLVM/bin/llvm-as`: 423 phase-3 commands | LLVMExports-release.cmake (`llvm-as`) |
| F13 | Clang and LLD versions = LLVM version | not visible; it's a check (`EXACT`, FindHalide_LLVM:50 and :62) | ClangConfigVersion / LLDConfigVersion `PACKAGE_VERSION` |

**Not needed in the graph:**
- The other 436 imported targets.
- `IMPORTED_SONAME`: the export says `@rpath/libLLVM.dylib`, but the real
  install name (`otool -D`) is the absolute `/opt/homebrew/opt/llvm@22/lib/libLLVM.dylib`.
  Homebrew relocation made the metadata stale. The rpath to `$LLD/lib`
  *is* needed at run time, because liblldWasm links `@rpath/liblldCommon.dylib`.
- `IMPORTED_LINK_DEPENDENT_LIBRARIES`: only a runtime-search hint.
- `COMPATIBLE_INTERFACE_BOOL`.
- The llvm-config binary.

**Gap that isn't discovery:** buildlog's L4 renders every `cc.include_dir`
as `-I`, while the baseline uses `-isystem` for imported include dirs. This
needs a system-ness bit on imported include dirs, whichever option is used.

## 3. Option 1: wrap a probing CMake configure

**Pipeline:** `option1_cmake_probe.py inputs/native-arm64.json [--fresh]`
does the following.
1. It writes `toolchain.cmake` from the inputs: C/C++ compiler, sysroot,
   triple → `CMAKE_SYSTEM_NAME`/`PROCESSOR`/`OSX_ARCHITECTURES`.
2. It runs the probe project with `--trace-format=json-v1 --trace-expand`.
3. It converts the dump into `cx.TargetInfo` objects and calls the existing
   importer's `_emit`, so the prop/genex/config mapping is the importer's.
4. It renames target ids through the registration and turns the registered
   variables into `res_probe_setting` rows.

The registration is in `inputs/*.json`: the package, the args, the cache
pins, the root regex, extra roots `clang` and `llvm-as`, the variable→setting
map, and five renames.

**Results:**
- **Rows:** 14 targets, 11 deps, 9 props, 5 locations, 4 settings and 1
  condition (`$Eq(native.opt, release)`). They cover F1–F13 (F10 as a
  `native.link_flag` rpath prop).
- **Time:**
  - Fresh-tree configure: 1.79–1.88 s with trace, 1.47–1.57 s without.
  - Of that, `find_package(Halide_LLVM)` alone takes about 0.9–1.0 s cold
    and 0.16 s warm. The rest is compiler detection.
  - Warm reconfigure: 0.46–0.48 s. Conversion: 0.06–0.09 s.
  - The trace is 52k lines and 12 MB.
- **What the mapping needed beyond `_emit`:**
  - Empty `INTERFACE_*_SETS` properties had to be filtered, or they become
    "unsupported property" target errors.
  - `_emit` silently drops `INTERFACE_CXX_RTTI`, because it is named in
    `COMPATIBLE_INTERFACE_BOOL`, and it ignores ILDL. Neither matters for the
    graph (§2).
- **Generic vs specific:** 228 SLOC of probe CMake plus 229 SLOC of driver,
  all generic (nothing names LLVM). The project side is about 25 lines of
  registration JSON.
  - **But the registration is where the LLVM knowledge went.** The renames
    exist because LLVM's CMake targets are un-namespaced (`LLVM`, `clang`,
    `lldWasm`). The extra roots and the variable map are hand-picked by
    reading FindHalide_LLVM.
- **Determinism** (`option1-determinism.sh`, range `21...99`; a run on the
  same inputs is byte-identical, `rows.json` md5 `f44c7411…` fresh ×2 and
  warm ×1):

  | Scenario | Picked |
  |---|---|
  | root=llvm@22 | 22.1.8 |
  | *same build tree*, then root=llvm@21 | **22.1.8**: `LLVM_DIR` is stale in the cache |
  | root=llvm@21, fresh tree | 21.1.8 |
  | no root | 22.1.8, but `Clang_DIR` is spelled Cellar instead of opt |
  | no root, `/opt/homebrew/opt/llvm/bin` on PATH | **23.1.2**, with `LLD_DIR=/opt/homebrew/lib/cmake/lld` |
  | no root, llvm@21 bin on PATH | 21 |
  | no root, `CMAKE_PREFIX_PATH=llvm@21` | 21 |
  | no root, env `LLVM_DIR=llvm@21` | 21 |

  The result depends on PATH, on the environment variables `LLVM_DIR`,
  `CMAKE_PREFIX_PATH` and `*_ROOT`, and on the probe's cache. A pin plus a
  fresh tree plus a scrubbed environment makes it deterministic, but only
  because the driver enforces that. CMake doesn't. Nothing reports that 4
  other LLVMs matched the range.
- **Provenance:** works, via the JSON trace. Examples:
  - `cc.define LLVM_VERSION=221` ← FindHalide_LLVM.cmake:152, which reads
    `LLVM_DEFINITIONS`@:150;
  - include ← :153, which reads `LLVM_INCLUDE_DIRS`@LLVMConfig.cmake:340;
  - the libLLVM location ← LLVMExports-release.cmake:2742;
  - rtti ← LLVMConfig.cmake:279.

  Limits:
  - The trace goes back one hop only, and further hops need a def-use walk
    over 52k trace lines.
  - `foreach` loop variables show as `?`.
  - Granularity is the command line; the importer reports the argument
    line, 2743.
  - The provenance lives outside CMake's own output. File API codemodel
    ≥2.9 has `abstractTargets` backtraces for imported targets, but not for
    their compile definitions or include dirs.
- **Cross-compilation** (x86_64-apple-macos target on an arm64 host): the
  configure succeeds and the rows are **identical to native**, with no
  warning. Yet `lipo -archs` says libLLVM, liblldWasm, clang-22 and
  llvm-config are all arm64-only. CMake never checks the architecture of an
  imported location.

  So the probe answers the wrong question, because it asks one question for
  two configurations:
  - libLLVM and lld are needed in the **target** configuration (x86_64),
    where this LLVM is unusable;
  - clang and llvm-as are needed in the **exec** configuration (arm64),
    where it is fine.

  A correct answer takes two probes, one per configuration, plus an
  architecture check that buildlog adds itself.

## 4. Option 2: ask llvm-config

**Pipeline:** `option2_llvm_config.py inputs/native-arm64.json` does the
following.
1. It runs `llvm-config` with 10 queries (`--version`, `--bindir`,
   `--cppflags`, `--includedir`, `--libdir`, `--cmakedir`, `--shared-mode`,
   `--has-rtti`, `--targets-built`, `--link-shared --libfiles`), plus
   `ld.lld --version` and `clang-22 --version`.
2. It runs them with `PATH=/usr/bin:/bin`.
3. It applies Python ports of the Halide rules.

The code is split into sections: GENERIC (40 SLOC), LLVM KNOWLEDGE (33) and
HALIDE RULES (56).

**Results:**
- **Time:** 12 queries, 0.07–0.09 s in total (each llvm-config call is about
  3.5 ms; clang and ld.lld about 14–24 ms). The first run of the session,
  with a cold page cache, took 1.1 s.
- **Against Option 1:** 42 of 43 rows are equal. The one difference: the
  location from `--libfiles --link-shared` is `lib/libLLVM-22.dylib`, a
  symlink to `libLLVM.dylib`. The baseline links `libLLVM.dylib`. It's the
  same file, but not the same edge.
- **What llvm-config cannot give:**
  - anything about **lld**, which is a separate keg: its root (Halide's hint
    `${LLVM_DIR}/../../../../lld@<maj>` works only with the opt spelling),
    its version, include dir and library names;
  - the **clang driver name**: `clang-<major>` is a layout rule, not a query;
  - **Clang's version**, except by running clang and parsing its banner;
  - the **dylib spelling** CMake uses;
  - the **rpath** policy, which is CMake's, not LLVM's.
- **What needs LLVM-version-specific knowledge:**
  - `--cppflags` must be filtered: keep the `-D` words, drop the `-I`
    (Halide takes the include dir separately), and drop
    `_GLIBCXX_ASSERTIONS` (Halide's rule).
  - `--cxxflags` must *not* be used: it adds `-std=c++17 -stdlib=libc++
    -fno-exceptions`, which Halide doesn't pass.
  - `--has-rtti` returns `YES`/`NO`, not `ON`/`OFF`.
  - The static-LLVM path, `llvm_map_components_to_libnames`, would need
    `--libnames <components>` plus knowledge of the extra components Halide
    adds (it isn't prototyped; the script refuses it).
  - LLVM 21 adds a definition, `EXPERIMENTAL_KEY_INSTRUCTIONS`. Both options
    pick it up automatically, but it shows that the flag set moves between
    versions.
- **Determinism:** the result is a function of the llvm-config binary's path
  and of the PATH you scrub. There is no cache. Picking the path is still a
  choice, and it is the same one as in Option 1.
- **Provenance:** coarse. The origin is the command line
  (`llvm-config --cppflags`), and there is no file:line. The answer is baked
  into the binary at LLVM's build time.
- **Cross-compilation:** llvm-config is an exec-configuration tool
  describing *itself*. It runs only if it matches the host, and says nothing
  about another architecture. For the x86_64 run it returns the same rows,
  again with no error. `--host-target` exists but only reports.

### The existing importer on the generated export files

`LLVMExports.cmake`, `ClangTargets.cmake` and `LLDTargets.cmake` (with their
`-release` companions) all go through `cmake_export.import_file` **with
status ok, zero errors and zero target errors**:

| File | Targets | Deps | Props | Locations | Time |
|---|---|---|---|---|---|
| LLVMExports.cmake | 303 | 1306 | 5 | 303 | 0.063 s |
| ClangTargets.cmake | 124 | 540 | 4 | 124 | 0.017 s |
| LLDTargets.cmake | 7 | 0 | 6 | 7 | 0.002 s |

It accepts every construct in them. There are no rejected constructs to
list. The `$<LINK_ONLY:…>` genexes are handled by the existing genex code.
Four observations would need importer or design changes. They are described
here, not made:
1. **Naming.** LLVM's targets are un-namespaced (`LLVM`, `clang`,
   `lldWasm`). `find()` requires `Ns::name`, so they can only be reached
   through `package_file`. Their ids would be bare names, which collide with
   buildlog's convention that a bare name is a pkg-config or CPS abstract
   name.
   - Suggested change: an import-time namespace, like `NAMESPACE` in
     `install(EXPORT)`. The package, `LLVM` / `Clang` / `LLD`, gives
     `LLVM::LLVM`, `Clang::clang`, `LLD::lldWasm`.
   - The prototypes use a rename table instead.
2. **`IMPORTED_LINK_DEPENDENT_LIBRARIES_<CFG>` is ignored.** That's why
   LLDTargets gives 0 deps. It is harmless here, because the Halide rules
   name both lld libraries and libLLVM directly. A transitive consumer of
   `lldWasm` alone would, however, lose the `lldCommon`/`LLVM` edges.
   - Suggested change: emit them as `native.runtime_dep`-style deps, or as a
     rpath-only relation.
3. **Configurations.** Only `Release` exists in these files, so a buildlog
   Debug build is an error. That is correct under "no choice", but the
   project will need a pinned configuration map (`Debug → Release`), which
   is `MAP_IMPORTED_CONFIG_*` in CMake terms.
4. **Path spelling.** Importing through `/opt/homebrew/opt/llvm@22/...`
   yields opt paths, while CMake's REALPATH yields Cellar paths. The
   prototypes pass realpaths so that the rows match. Whether buildlog should
   realpath binding roots is a design question (see Q3).

## 5. The hybrid

`option2_llvm_config.py --hybrid [--source llvm-config|cmake-config]` works
as follows.
- **Artifacts:** the three export files go through the existing importer
  (above), and their rows become `imp_*` inputs.
- **LLVM's configured values** become `tool_word` rows. They come either
  from llvm-config (6 queries) or, with `--source cmake-config`, **read
  statically** by `config_values.py` from LLVMConfig.cmake plus
  ClangConfigVersion/LLDConfigVersion `PACKAGE_VERSION`. `config_values.py`
  reads only top-level literal `set()` lines and the REALPATH prefix idiom.
  Anything conditional, repeated or computed is an error, not a guess.
- **Halide's derivation** is `hybrid/halide_llvm.dl`: 72 SLOC of Soufflé,
  stock 2.5. It covers:
  - `SHARED_LIBS` = dylib ∧ WebAssembly built;
  - an error unless the Clang and LLD versions equal the LLVM version;
  - found components = requested ∩ built, with WebAssembly also needing lld;
  - an error on a missing required component;
  - Core's defines (minus `_GLIBCXX_ASSERTIONS`, plus `LLVM_VERSION`), its
    include dir and its dep on `LLVM`;
  - the component targets;
  - the needed-closure over the imported rows;
  - the settings.

**Results** (native arm64, llvm@22):
- **With llvm-config:** 43 of 43 rows equal to Option 1, 0 missing, 0
  extra. It gets `libLLVM.dylib` right because the location now comes from
  the export file.
- **With cmake-config:** 43 of 43 equal. It also matched Option 1 **for
  llvm@21** (44 of 44, including `EXPERIMENTAL_KEY_INSTRUCTIONS` and
  `LLVM_VERSION=211`) and for the cross input (43 of 43, which is equally
  wrong about the architecture; see below).
- **Time:**
  - Importer: 0.08 s.
  - Static config read: 0.001–0.003 s (llvm-config instead: 0.02 s warm).
  - Soufflé `-F`, interpreted, including start-up: 0.05–0.12 s.
  - Total: **about 0.15–0.2 s**, with no process except Soufflé.
- **Provenance:** every value has a file:line.
  - `config_values.read` returns `(value, line)`, for example
    `LLVM_DEFINITIONS`@LLVMConfig.cmake:357 and
    `LLD_INCLUDE_DIRS`@LLDConfig.cmake:16.
  - The importer gives file:line for the locations.
  - The rules have their own `.dl` lines.
  - **Not yet threaded through:** the prototype collects the config origins
    but doesn't attach them to output rows. In buildlog they would become
    the probe result's source-map origins; that is plumbing, not a
    feasibility question.
- **Cross-compilation:** it is still wrong in the same way, but the
  structure is right for fixing it. Every input is a file read, so the
  library/tool split can bind two roots: one for the target configuration
  (the libraries) and one for the exec configuration (clang, llvm-as). An
  architecture check on the bound Mach-O/ELF artifacts is then one generic
  probe (`lipo -archs` / reading the header). This is an estimate: not
  prototyped.

## 6. Comparison

SLOC means non-blank, non-comment lines in the prototypes. The production
figures are estimates.

| | Option 1: CMake probe | Option 2: llvm-config | Hybrid (importer + config read + rules) |
|---|---|---|---|
| Generic code | 228 CMake + 229 Py (probe + driver); reuses `_emit` | 40 Py (tool query, flag → prop) | 59 Py `config_values` + the existing importer (0 new) + about 8 `.dl` closure lines |
| LLVM-specific code | about 25 lines of registration (renames, roots, variable map) | 33 Py: queries, layout rules, lld | about 20 Py `config_words` (which variable names), plus the rename/namespace table |
| Halide-specific code | 0 (CMake runs Halide's module) | 56 Py: a port of FindHalide_LLVM | about 64 `.dl`: a port of FindHalide_LLVM |
| Rows vs ground truth | 43/43, by construction | 42/43 (`libLLVM-22.dylib`) | 43/43; also 44/44 on llvm@21 |
| Time, fresh | 1.5–1.9 s (compiler detection + LLVMConfig's own probes) | 0.07–0.09 s | 0.15–0.2 s |
| Time, warm | 0.46–0.48 s | same | same |
| Determinism | depends on PATH, env and CMake cache; needs a scrubbed environment and a fresh tree to be a function | a function of the binary path + PATH | a function of the file contents at the bound roots |
| Provenance | file:line via `--trace`, one hop, 12 MB trace | command line only | file:line for every input value |
| Hidden effects | try_compile, 5 nested find_packages, compiler detection | runs 3 binaries | none (reads 9 files: 3 configs, 3 exports + 3 `-release`) |
| Cross-compilation | silently wrong (identical rows); one question for two configurations | silently wrong, and only if the tool runs on the host | silently wrong today; splitting target and exec roots is natural |
| Multiple LLVMs (llvm@19–23) | CMake's search picks one; the probe cache can pin the wrong one | whoever picks the binary picks | the binding picks; a unique match or an error is easy (files) |
| Upstream drift | absorbed: CMake runs the current module | the Halide port and LLVM knowledge must follow | the Halide port must follow (72 lines); LLVM exports are absorbed |

**Against the design principles:**

| Principle | Option 1 | Option 2 | Hybrid |
|---|---|---|---|
| No choice (unique or pinned, else error) | ✗ CMake's search is newest-first, which is a choice; buildlog only sees the winner | ~ a pin on the binary, but no uniqueness check | ✓ the binding chooses; the importer and config reader error on anything ambiguous |
| Discovery is the only effectful stage | ~ one probe, but a huge one, and its inner effects are invisible | ✓ small probes | ✓ file reads only; the rules are pure |
| Names, not locations | ✗ locations enter as CMake cache pins (`Halide_LLVM_ROOT`); the names are CMake's un-namespaced ids | ~ a location (the binary) is the input | ✓ a name → one binding root; the importer supplies the names (given the namespace fix) |
| Thin translator | ✗ buildlog wraps a whole second meta-build system | ✓ thin, but lossy (lld, clang, spelling) | ✓ the existing thin translator, plus a literal reader |
| Provenance | ~ possible through the trace, outside CMake's contract | ✗ command-level only | ✓ file:line throughout |

## 7. Beyond Halide

**Option 1 for other projects.** The generic library is "run any CMake find
module, dump the reached imported targets". It works for any
FindFoo.cmake (FindPython, FindCUDAToolkit, FindBoost, …). That makes it the
most general, but also buildlog's least principled component: the project
brings CMake as a dependency of discovery, and every find module's own
search order and cache semantics come with it. The registration grows a
rename table per package and a variable map per consumer.

**Option 2 for other projects.** The generic library is "run a `*-config`
tool, map flags to props". pkg-config is already this, and buildlog already
has a better version of it (the pc importer). `llvm-config`, `python3-config`,
`sdl2-config`, `icu-config` and so on each have their own flag vocabulary
and their own omissions. Each tool needs a hand-written adapter, and the
tool describes only its own build.

**Hybrid for other projects.** The generic libraries are:
- the **existing** export-file importer, unchanged apart from the namespace
  option;
- `config_values`: a strict reader of configured literals in
  `<Pkg>Config.cmake`, which works for any configure_file()-generated
  config. It fails loudly on the Boost/Qt-style computed ones.
- the rule layer: the **project** writes its own derivation logic, and the
  question "what did your Find module compute?" becomes ordinary buildlog
  rules.

This generalizes as a pattern: *generated facts go through importers,
configured literals through a strict reader, project policy through rules.*
It does not generalize to find modules that search (FindZLIB and friends).
Those are pkg-config/CPS territory already.

**Multiple LLVMs.** Homebrew has llvm@19, @20, @21, @22 and `llvm` (23.1.2),
plus lld@19–22 and a linked lld. Every one of them satisfies Halide's
version range. This is the "multiple editions" situation from the notes:
several editions of one package are present, and choosing among them is
policy, not discovery.
- Option 1 hides the choice in CMake's search order: the newest version
  first, then PATH, then env. It remembers the choice in a cache that
  outlives a change of inputs (the stale-22 scenario).
- Option 2 moves the choice to whoever picks the binary.
- The hybrid makes it explicit. The project names `LLVM` (and `Clang`,
  `LLD`), the user binds each name to one root, and more than one candidate
  without a pin is an error.

  There is one wrinkle, which is real data: LLD is a *separate* edition axis
  (`lld@22` vs the linked `lld`, 23). The Clang = LLD = LLVM check has to be
  a rule over three bindings, not an assumption.

**Design tradeoffs:**
- Option 1: "buildlog can delegate to CMake". It shows interoperability, but
  undercuts the thesis: the logic stays in CMake and buildlog records the
  answer.
- Option 2: "tool queries are a binding source". It is fine, but
  unremarkable, and it loses information.
- Hybrid: "a 180-line imperative Find module reduces to 72 lines of
  declarative rules over facts from generated files, with the effectful part
  shrunk to reading nine files". This is the case study's actual claim:
  meta-build logic is a logic program. The honest caveats:
  - Halide's module must be *ported*, and the port can drift; §3's Option 1
    output is the oracle to test it against.
  - Only the shared-LLVM path is covered.

## 8. Recommendation

**Use the hybrid.** Specifically:
- the existing CMake-export importer for `LLVMExports`, `ClangTargets` and
  `LLDTargets`, with a namespace option added;
- a strict configured-literal reader for the handful of values that exist
  only in `LLVMConfig.cmake`/`LLDConfig.cmake`, instead of llvm-config;
- Halide's FindHalide_LLVM logic as project-level rules;
- Option 1's probe kept only as a **test oracle** (in CI or the case-study
  compare), not as the discovery path.

Reasons, all from the data above:
1. It is the only option that meets all five principles.
2. It reproduced the ground truth exactly on llvm@22 and llvm@21, where
   Option 2 didn't (the dylib spelling).
3. It is about 10× faster than Option 1 fresh, and it has no environment or
   cache dependence (Option 1 picked 23.1.2 from PATH and a stale 22 from
   its cache).
4. It is the simplest reconstruction: the new generic code is about 60
   lines, and the project-specific code is about 85 lines (rules + config
   names), where Option 1 needs about 460 generic lines plus CMake itself.

llvm-config adds nothing the config files don't have, and loses provenance.

## 9. Open questions for the user

1. **Namespace for un-namespaced exports.** Should the importer take a
   `namespace =` per import (giving `LLVM::LLVM`, `Clang::clang`,
   `LLD::lldWasm`)? Or should buildlog accept bare CMake ids in a separate
   `cmake:` id space?
2. **Is `config_values` acceptable as a binding source?** It reads a
   hand-written file, strictly: only literal top-level `set()` lines, with
   an error otherwise. Or must configured values always come from a tool
   query or a generated file?
3. **Realpath binding roots?** CMake's configs realpath themselves, so the
   Cellar path is what appears in the baseline. Should buildlog canonicalize
   a binding root, or preserve the user's spelling? This affects the
   `compare.py` normalization for lld.
4. **Target vs exec LLVM.** Should the case study model the library-LLVM
   (target) and tool-LLVM (exec; clang and llvm-as for the runtime) as two
   bindings from day one? They coincide natively. An architecture check on
   bound artifacts would then be one generic probe.
5. **The static-LLVM path.** `Halide_LLVM_SHARED_LIBS=NO`, which uses
   `llvm_map_components_to_libnames`, is unported. Is it in scope for M3,
   or is shared-only acceptable with an explicit error?
6. **`-isystem`.** L4 needs a notion of system include dirs for imported
   targets before the phase-1 compile lines can match. Should that be
   tracked as a separate M3 item?
