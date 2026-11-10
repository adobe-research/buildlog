<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: zlib-ng, per-TU ISA flags and ISA compatibility

zlib-ng compiles each of its architecture-specific sources with its own ISA
flags: `set_property(SOURCE ... COMPILE_FLAGS ...)` in CMakeLists.txt
(~736-1185). A chain of `cmake_dependent_option`s and
`check_c_source_compiles` probes (cmake/detect-intrinsics.cmake) decides
which sources are compiled. At run time, `functable.c` dispatches to the
best kernel the CPU has. Upstream only ever looks at one architecture per
configure run: `BASEARCH_*_FOUND` comes from the first architecture of the
target. So a universal2 build either fails (#1085, `unknown target CPU
'armv8-a+crc'`) or, as with 2.3.3 here, quietly builds **no SIMD code in
either slice** (#1016, "CMake only detects one architecture"). Upstream's
accepted answer is to configure each architecture separately and `lipo` the
results by hand.

buildlog builds zlib-ng 2.3.3 for arm64, for x86_64 (cross) and as
universal2 from one spec. Each slice gets its own ISA source sets and
flags, identical TU for TU to CMake's single-architecture builds. The ISA
knowledge is stated as facts:

- which extensions an architecture has;
- which extensions a source set needs;
- how the toolchain spells them.

"Set S is compatible with platform P" is derived from those facts, not
hand-written as conditionals. Two combinations are configure errors:

- a set needed in a configuration whose CPU lacks its extensions (for
  example a NEON TU in an x86_64 slice);
- a set requiring extensions that no architecture has together (NEON with
  AVX2).

`buildlog why` explains why a TU has `-mavx2` in the x86_64 slice, and why
that TU does not exist in the arm64 slice.

## Layout

- `baseline/`: zlib-ng's own CMake build on this Mac, recorded by
  `baseline/run-cmake.sh` (Release, Ninja, defaults; source and build dirs
  normalized to `$SRC`/`$BUILD`). There are five variants:
  - `native` (arm64);
  - `x86_64-default` (fails: see below);
  - `x86_64` (`-DWITH_GTEST=OFF`);
  - `universal2-default` (fails);
  - `universal2` (`-DWITH_GTEST=OFF`).

  Each variant has `commands.txt` (the verbose Ninja log),
  `configure-checks.txt`, `cache.txt`, the generated headers and `.pc`,
  `exported-symbols.txt`, `dylib-metadata.txt`, `isa-functions.txt` (the
  ISA-specific functions each slice of `libz-ng.a` defines) and
  `ctest.txt`. `environment.txt` records the tools.
- `project/`: the buildlog spec.
  - `buildlog.toml`: the source root (`ZLIBNG_SRC`) and vars.
  - `spec.toml`: configurations, options, directory-wide flags and
    definitions, the library, and the generic fallback source sets.
  - `checks.toml`: the configure checks.
  - `isa.toml`: the ISA source sets (the ISA knowledge is buildlog-cpp's).
  - `configured.toml`: zconf-ng.h, zlib-ng.h, gzread_mangle.h,
    zlib_name_mangling-ng.h and zlib-ng.pc.
  - `tests.toml`: the test programs and smoke tests.
  - `rules.dl`: the library, source sets,
    directory-wide props.
  - `isa.dl`: the sets' gates, roles and defines (buildlog-cpp derives
    their fit and flags).
  - `presets/apple-clang.toml`: the user's toolchain pins.
- `compare.py`: compares a buildlog build with a baseline per TU (flags as
  sets; see the script's docstring).
- `tests/test_zlibng_case.py`: the automated checks, listed at the end.

## Pinned inputs

- zlib-ng **2.3.3** (latest stable tag), commit
  `12731092979c6d07f42da27da673a9f6c7b13586`, the submodule
  `case-studies/zlib-ng/upstream` (https://github.com/zlib-ng/zlib-ng.git). `BUILDLOG_ZLIBNG_SRC` overrides the path for
  the tests, and `-D ZLIBNG_SRC=...` overrides it for configure.
- macOS 26.5.2 arm64, Apple clang 21.0.0 (clang-2100.1.1.101), CMake 4.4.3,
  Ninja 1.13.2 (`baseline/environment.txt`).

## Running

```sh
# Baseline (writes build/case-zlib-ng/raw and baseline/<variant>/)
case-studies/zlib-ng/baseline/run-cmake.sh all

# buildlog: all three configurations
uv run --frozen buildlog configure --user case-studies/zlib-ng/project/presets/apple-clang.toml \
  -C build/case-zlib-ng/bl/all case-studies/zlib-ng/project
ninja -C build/case-zlib-ng/bl/all/souffle
uv run --frozen buildlog test -C build/case-zlib-ng/bl/all case-studies/zlib-ng/project

# Per-TU comparison with the baselines
case-studies/zlib-ng/compare.py case-studies/zlib-ng/baseline/native build/case-zlib-ng/bl/all/souffle arm64-macos
case-studies/zlib-ng/compare.py case-studies/zlib-ng/baseline/x86_64 build/case-zlib-ng/bl/all/souffle x86_64-macos

# The headline proofs
uv run --frozen buildlog why -C build/case-zlib-ng/bl/all --with-demand --leaves \
  --output obj/x86_64-macos.release.none/zlib-ng/arch/x86/adler32_avx2.c.o --flag=-mavx2 \
  case-studies/zlib-ng/project
uv run --frozen buildlog why -C build/case-zlib-ng/bl/all \
  'config_attr(["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]], "isa.avx2", "off")' \
  case-studies/zlib-ng/project

# Tests
uv run --frozen pytest -q -n 4 tests/test_zlibng_case.py
```

A fresh configure of all three configurations takes about 25 s: 88 probe
answers, 76 of them checks (38 checks × 2 platforms), then three Soufflé
rounds. It emits 246
commands.

## Baseline

| variant | result |
|---|---|
| native (arm64) | builds; ctest 70/70 (69 plus gtest_zlib). ISA sources: arm_features, crc32_armv8, 4 × NEON. |
| x86_64-default | **fails**: gtest_zlib links Homebrew's arm64-only libgtest.a. |
| x86_64 (`WITH_GTEST=OFF`) | builds; ctest 69/69 under Rosetta. ISA sources: x86_features, SSE2 ×4, SSSE3 ×2, SSE4.1, SSE4.2, PCLMUL, AVX2 ×4, AVX512 ×3, AVX512-VNNI, VPCLMULQDQ. |
| universal2-default | **fails** the same way (gtest). |
| universal2 (`WITH_GTEST=OFF`) | builds; ctest 69/69. It is **silently degraded** (see below). |

What the universal2 (`WITH_GTEST=OFF`) build gets wrong:

- CMake detects `arm64` only (`Arch detected: 'arm64'`, basearch `arm`).
- `HAVE_MARCH_ARMV8_CRC`, `NEON_AVAILABLE` and the ARMv6 probes fail,
  because the probe compiles for both architectures at once.
- The only architecture source is `arch/arm/arm_features.c`, compiled into
  **both** slices, the x86_64 one included.
- `isa-functions.txt` for both slices is only
  `_arm_check_features, _cpu_check_features`. There is no NEON or CRC32 in
  the arm64 slice, and no SSE or AVX in the x86_64 slice.

With 2.3.3 the #1085 hard error (`unknown target CPU 'armv8-a+crc'`) no
longer appears: the `HAVE_WA_MARCH_*` workaround probes route around it.
The failure mode is now silent loss of every SIMD kernel, plus an
arm-only TU in the x86_64 slice. That is exactly the combination the ISA
model below rejects.

## Result

| | CMake | buildlog |
|---|---|---|
| arm64 TUs (lib ×2 variants + programs) | 90 | 90, flags equal as sets (`compare.py`: 0 differences) |
| x86_64 TUs | 110 | 110, flags equal as sets (0 differences) |
| universal2 arm64 slice ISA functions | `_arm_check_features _cpu_check_features` | = native baseline (13 functions: NEON, ARMv8 CRC32) |
| universal2 x86_64 slice ISA functions | `_arm_check_features _cpu_check_features` | = x86_64 baseline (SSE2 … VPCLMULQDQ, `_x86_check_features`) |
| universal2 files | libz-ng.a, dylib, programs (all `x86_64 arm64`) | the same, plus the dylib's two links |
| generated headers, zlib-ng.pc (arm64, x86_64) | | byte-identical |
| dylib install name / versions / exports | `@rpath/libz-ng.2.dylib`, 2.0.0 / 2.3.3 | identical; the exported symbol set is identical |
| libz-ng.a members | | the same set |
| configure-check answers | | the same for every check both ask (see Checks) |
| tests | ctest 70 / 69 / 69 | `buildlog test`: 7/7 on arm64. The x86_64 and universal2 programs (both slices) run under `arch -x86_64` / `-arm64` in pytest. |

## The project

**The library** (`rules.dl`). `zlib-ng` is one target of kind `library`
with `default_linkage = "both"`: each configuration builds it static and
shared, as upstream's `zlib-ng-static` and `zlib-ng` (both named `z-ng`).
Core gives it two build instances: the static one is `zlib-ng`
(`obj/<cfg>/zlib-ng/`), the shared one `zlib-ng@shared`
(`obj/<cfg>/zlib-ng@shared/`, PIC, `ZLIB_DLL` from `linkage_prop`, the
versions). A user may choose one linkage (`library_linkage`), and the other
instance is then not built. Each test program pins the instance it links
(`dep_linkage`, with upstream's reason): minigzip the static one, the
others the shared one. `-fno-semantic-interposition` is the shared
build's only (`shared_prop_when`, a project rule over core's
`native_kind_in`). The library owns the configured headers. Until the
fixed kinds were removed (step 4), `library_variant` made two targets,
`zlib-ng` (shared) and `zlib-ng-static`.

**Source sets.** CMake appends sources to `ZLIB_ARCH_SRCS` inside `if()`s
and sets `COMPILE_FLAGS` per source. Since step 4, a source set is a
buildlog-cpp **source group** of the library (design input §5,
`source_group`, `group_source`, `group_when`, `group_prop(_when)`), and so
of both its instances. The set's sources compile as the library's own
objects, with its properties, where the set's condition holds, plus the
set's properties. The
generic fallbacks are in two sets with disjoint conditions; a source
compiles where any of its groups applies.

Until step 4, buildlog-cpp's `c_source` was unconditional and had no
per-source properties, so a source set became one **object library per
variant**, named `<variant>.<set>` (for example `zlib-ng-static.avx2`):

- It copied the variant's compile properties: usage requirements,
  `ZLIB_DLL`, PIC for the shared variant, `-fno-semantic-interposition`.
- It added the set's own properties.
- The variant depended on it under the set's condition (`dep_when`), and
  core's object library folded its objects into the variant.

The generic fallbacks are three plain source sets (`spec.toml`), gated by
the `x86_fallbacks` / `all_fallbacks` conditions.

**Directory-wide properties.** `dir_prop` and `dir_prop_when` model
CMake's `CMAKE_C_FLAGS`, `add_compile_options` and `add_definitions`. They
apply to every target with C sources, the test programs included.

### The ISA model (`isa.toml`, `isa.dl`; buildlog-cpp)

The facts, smallest set first. Since step 4 (design input §2) the first
five are buildlog-cpp's knowledge (L2 and the clang F layer), and the
project states only the last four:

```
isa_ext(cpu, e)              e is an ISA extension of architecture cpu      ("these extensions apply to this platform")
isa_baseline(cpu, e)         every cpu of the architecture has e (no flag)
family_isa_flag(fam, e, f)   additive spelling (-mavx2)
family_isa_march_version(fam, cpu, base, rank), family_isa_march_ext(fam, e, sfx, rank)
                             non-additive spelling: -march=<base>+<sfx>+…   (AArch64)
isa_requires(set, e)         every TU of the set needs e                    ("this TU requires ISA set S")
isa_requires_when(set, e, c) … under a condition (a flag only: x86_features' -mxsave)
isa_role(set, "kernel")      reached only through the dispatcher: -fno-lto where available
isa_define(set, d)           directory-wide definition where the set is built (X86_AVX2, ARM_NEON, …)
```

Each set is an ISA set of buildlog-cpp, attached to the library's source
group of the set (`isa_group(l, s, s)`). Everything else is derived, the
first two and the flags by buildlog-cpp:

- `isa_compatible(s, cpu)`: the CPU has every extension `s` uses
  ("S is compatible with platform P"). It is a count comparison, so it
  needs no negation over the cycle.
- `isa_coexist(a, b)`: some architecture has both extensions.
- Per configuration, the discovered attribute
  `isa.compatible.<s> = yes | no` comes from `platform.cpu`, which in
  universal2 is the slice's CPU.
- `isa.<s> = on | off` is compatibility **and** the set's own gate: the
  option, the probe and the prerequisite sets, read as `$Eq(isa.sse42, on)`
  like upstream's `cmake_dependent_option` chains.
- The variant depends on the set under `isa:<s>`, so the set's objects
  exist exactly where `isa.<s> = on`.
- Flags: one `-m<ext>` for each additive non-baseline extension, plus one
  `-march=<base>+<sfx>…` joined over the non-additive ones (core's
  `join_part`/`joined`), per configuration by its C toolchain's family. A
  kernel also gets `-fno-lto` where the toolchain has it (the project's).

The x86 flags and `-march=armv8-a+crc` / `-march=armv8-a+simd` are derived
this way. No `-m` flag is written in a source set.

**Rejections** (configure errors, tested):

```
error: ISA scope incompatible with the configuration's cpu: neon_user: arch/arm/adler32_neon.c (neon) needs neon, which x86_64 (x86_64-macos.release.none) does not have
error: ISA extensions cannot coexist in one TU (no architecture has both): mixed: avx2 and neon
```

- The first error is buildlog-cpp's `error_at` on the target that
  compiles one of a set's sources where the configuration's CPU lacks the
  set's extensions: in the set's group (the gate prevents it), or outside
  it (the test adds an x86_64 program that compiles
  `arch/arm/adler32_neon.c` itself).
- The second is a global `error`, fired for a set whose required
  extensions no architecture has together. AVX2 plus AVX512 is fine,
  because x86_64 has both.
- There are also errors for an unknown extension, an extension with no
  spelling, a conditional extension without an additive flag, and ISA
  facts about a set that is not a source set.

**`buildlog why` (headline).** Why `adler32_avx2.c.o` in the x86_64 slice
has `-mavx2`:

```
action_arg("out:obj/x86_64-macos.release.none/zlib-ng/arch/x86/adler32_avx2.c.o", 17, "", "-mavx2")  [buildlog-cpp:layers/L4_cxx.dl:216]
  group_obj(…, "zlib-ng", ‹x86_64-macos.release.none›, "avx2")  [buildlog-cpp:layers/L4_cxx.dl:214]
    group_source("zlib-ng", "avx2", "arch/x86/adler32_avx2.c")  [project:rules.dl:72]
      source_set("avx2", "zlib-ng")  ← isa.toml:101
      source_set_source("avx2", "arch/x86/adler32_avx2.c")  ← isa.toml:102
    … (group_on: the set's gate, isa.compatible.avx2, WITH_AVX2 and the probe)
  group_use("zlib-ng", ‹x86_64-macos.release.none›, "avx2", "cc.compile_flag", "-mavx2")  [buildlog-cpp:layers/L4_cxx.dl:332]
    isa_group("zlib-ng", "avx2", "avx2")  [project:isa.dl:17]
      isa_requires("avx2", "avx2")  ← isa.toml:109
    isa_family_of(‹x86_64-macos.release.none›, "clang")  [buildlog-cpp:layers/L4_cxx.dl:331]
      toolchain("apple-clang", "clang", "21.0.0")  [buildlog-cpp:layers/L2_toolchains.dl:35]
    isa_scope_flag("avx2", "clang", "-mavx2")  [buildlog-cpp:layers/L4_cxx.dl:298]
      isa_nonbaseline("avx2")  [buildlog-cpp:layers/L4_cxx.dl:274]
        isa_ext("x86_64", "avx2")  ← buildlog-cpp layers/L2_toolchains.dl:65
        !isa_baseline("x86_64", "avx2")
      family_isa_flag("clang", "avx2", "-mavx2")  ← buildlog-cpp layers/F_clang.dl:90
```

With `--with-demand --leaves` (52 lines: the root and 51 origin facts), the
leaves also show why the set is built in that slice at all:

- the set's row, `source_set_when("avx2", "WITH_AVX2 & cc.check.HAVE_AVX2_INTRIN & isa.sse42 == on")`
  (the condition it desugars to shares its line), `option_default("WITH_AVX2", "on")`
  and `request_all("zlib-ng")`;
- the check's rows and its probe answer
  (`res_cc_check(… _mm256_subs_epu16 …|-mavx2 -mbmi2 -fno-lto", "yes")`);
- the same for SSE4.2, SSE4.1, SSSE3 and SSE2, the prerequisite chain
  (`isa.sse42 == on`, …): 29 of the 51 leaves;
- `config_decl("x86_64", "x86_64-macos")`, the inventory
  (`res_platform_constraint(…, "x86_64-macos", "cpu", "x86_64")`, the
  toolchain, the build machine) and the `cc_capability` answer;
- `toolchain_pin("c", "arm64-macos", "x86_64-macos", "apple-clang")` from
  the preset;
- buildlog-cpp's `isa_ext("x86_64", "avx2")` and `family_isa_flag("clang", "avx2", "-mavx2")`.

Nothing about the arm64 or universal2 configurations, the shared build or
another set's condition is listed; a test bounds the answer at 52 lines
(`test_why_avx2_flag_leads_to_the_isa_facts_the_cpu_and_the_probe`; 53
until the library's `library_variant` leaf went with the fixed kinds). Before
the fix (2caa73e) it was 69: focused views did not elide domain guards
(`cond_sub`, `config_platform`, `settings_suffix`), whose subproofs are the
engine's choice of witness, and `--with-demand` also expanded the probes'
demand, where the C capability probe's witness was the shared variant.

Why the AVX2 set does not exist in the arm64 slice (universal2's arm64
slice is the `arm64-macos.release.none` configuration):

```
config_attr(‹arm64-macos.release.none›, "isa.avx2", "off")  [project:isa.dl:48]
  cond_holds(‹arm64…›, "isa:avx2=off")
    condition("isa:avx2=off", $Not($And($Eq("isa.compatible.avx2", "yes"), $And($Named("WITH_AVX2"), …))))
    fails(‹arm64…›, $Eq("isa.compatible.avx2", "yes"))
      config_attr(‹arm64…›, "isa.compatible.avx2", "no")  [project:isa.dl:44]
        config_attr(‹arm64…›, "platform.cpu", "arm64")
          platform_constraint("arm64-macos", "cpu", "arm64")  ← probe inventory
        !isa_compatible("avx2", "arm64")
```

`buildlog why-not 'group_on("zlib-ng", <arm64 cfg>, "avx2")'` walks
the same way in one step: of `group_on`'s two rules, the unconditional one
fails at `!group_conditional`, and the conditional one stops at
`✗ cond_holds(‹arm64-macos.release.none›, "isa:avx2")`, with the next
`why-not` to run. (With the step-3 object libraries this took three steps,
since why-not first bound `eff_dep`'s `d` to an unrelated set; ledger item 11.)

### Checks (`checks.toml`)

There are 38 checks, in these groups:

- compiler flags: `-fno-lto`, the three `-Werror=…`,
  `-fno-semantic-interposition`;
- headers;
- `off64_t` / `_off64_t`;
- `fseeko` / `strerror`;
- `__cpuid_count` / `__cpuid`;
- attributes and builtins;
- `ptrdiff_t`;
- the ISA probes of detect-intrinsics.cmake with upstream's flags.

They use buildlog-cpp's `cc_check` (the zlib mechanism), and every answer
matches CMake's for every check both ask. The ledger has the spellings.

**Which checks are really toolchain facts.** The PI's end state is a
toolchain-knowledge base, not probes. For each ISA probe, the answer is a
function of (compiler family and version, target CPU):

- **Toolchain facts outright**:
  - `HAVE_{SSE2,SSSE3,SSE41,SSE42,PCLMULQDQ,AVX2,AVX512,AVX512VNNI,VPCLMULQDQ,XSAVE}_INTRIN`;
  - `HAVE_CASCADE_LAKE` (`-mtune=cascadelake`);
  - `NEON_AVAILABLE`, `NEON_HAS_LD4`, `HAVE_ARMV8_INTRIN`,
    `HAVE_ARMV8_INLINE_ASM`;
  - `FNO_LTO_AVAILABLE`, `HAVE_NO_INTERPOSITION`, `HAVE_W_ERROR_*`;
  - the attribute and builtin probes (`HAVE_ATTRIBUTE_*`,
    `HAVE_BUILTIN_*`).

  The answer is "no" for every x86 probe on arm64 and vice versa, and
  those answers are asked anyway (`isa_ext` already implies them).
- **SDK / libc facts**: the header checks, `off64_t`, `fseeko`,
  `strerror`, `__cpuid_count`, `ptrdiff_t`. They depend on the platform's
  SDK, not on the compiler.
- **Dropped in favour of a fact**: `HAVE_MARCH_ARMV8_CRC`, a probe of
  whether `-march=armv8-a+crc` is accepted. Here `isa_march` /
  `isa_march_ext` state the spelling instead.

An earlier version of `isa.dl` derived `cc_check_known(... "no")` for the
ISA probes of foreign CPUs, so they would not run. It had to be dropped:
the plugin keeps `cc_check`, `cc_check_args` and `cc_check_headers`
private, and core keeps `toolchain_pin` private, so a project cannot
reconstruct a check's key (Design input 4).

### Tests (`tests.toml`)

The programs are example, minigzip (static), minideflate, switchlevels,
infcover (+ inftrees.c), makefixed, maketrees and makecrct, built in every
configuration and fat in universal2. The tests are:

- `example`
- `infcover`
- `minigzip-help`, `minideflate-help`, `switchlevels-help` (`--help`)
- `minigzip-invalid`, `minideflate-invalid` (`--invalid`; the usage message
  decides)

`buildlog test` runs them on arm64 (7 passed). It reports the x86_64 ones
as "not run" (foreign platform), and it has no tests for the universal2
configuration.

pytest also runs these under Rosetta and on both universal2 slices:

- the x86_64 `example`;
- the universal2 `example` under `arch -x86_64` and `arch -arm64`;
- a minigzip round trip.

## Ledger

**Same.**

- Every TU of both libraries and the test programs, for arm64 and x86_64:
  the same source set per target, and the same flags and definitions as
  sets, after normalizing `-O3` away (below). The comparison ignores
  `-I` dirs, output paths and `-arch`.
- The ISA sources and flags:

  | x86_64 set | flags |
  |---|---|
  | SSE2 | none |
  | SSSE3 | `-mssse3` |
  | SSE4.1 | `-msse4.1` |
  | SSE4.2 | `-msse4.2` |
  | PCLMUL | `-msse4.2 -mpclmul` |
  | AVX2 | `-mavx2 -mbmi2` |
  | AVX512 | `-mavx512f -mavx512dq -mavx512bw -mavx512vl -mbmi2 -mtune=cascadelake` |
  | AVX512-VNNI | AVX512's flags `+ -mavx512vnni` |
  | VPCLMULQDQ | `-mpclmul -mvpclmulqdq` plus AVX512's flags (upstream repeats `-mavx512f`) |

  Every set except SSE2 also gets `-fno-lto`. `x86_features` gets
  `-mxsave` without `-fno-lto`. On arm64, crc32_armv8 gets
  `-march=armv8-a+crc -fno-lto` and the NEON files get
  `-march=armv8-a+simd -fno-lto`.
- The definitions `X86_*`, `ARM_*`, `WITH_ALL_FALLBACKS`, `HAVE_*`,
  `ZLIBNG_NATIVE_API` (public on the library; makefixed, maketrees and
  makecrct lack it, as upstream) and `ZLIB_DLL` (the shared build).
- The generic fallbacks: all 7 on arm64, and 4 on x86_64.
- The generated zconf-ng.h, zlib-ng.h, gzread_mangle.h,
  zlib_name_mangling-ng.h and zlib-ng.pc, byte for byte.
- The dylib's install name, compatibility and current version and exported
  symbols; the static archive's member set; the ISA functions of every
  slice.
- The link driver is the C driver (after the plugin fix below).

**Differs, and why.**

- **Optimization flags.**
  - `-O3 -DNDEBUG` then `-O2`. buildlog-cpp's `native.opt=release` adds
    `-O3 -DNDEBUG`. zlib-ng rewrites CMake's Release `-O3` to `-O2` in
    `CMAKE_C_FLAGS_RELEASE`, so the project adds `-O2` as a directory-wide
    flag. It comes later on the line and wins. The compiled code matches;
    the command line has both.
  - CMake also passes `-O2 -DNDEBUG` to links; buildlog does not.
- **Flag order.** buildlog orders by property rank, then lexically. CMake
  orders by definitions, then includes, then flags. Only order differs.
- **`--target=<arch>-apple-macos13 -isysroot <SDK>` vs `-arch <arch>`.**
  The deployment target is explicit (minos 13.0 vs the host's), as in
  case-studies/zlib.
- **Link lines.**
  - CMake adds `-Wl,-search_paths_first -Wl,-headerpad_max_install_names`.
  - The build-tree rpath is `@loader_path/../../lib/<cfg>` rather than the
    absolute `$BUILD`.
  - Link and archive member order is lexical by object path (object
    libraries first) rather than CMake's list order. Link results do not
    depend on it.
- **Paths.** A source set's objects are
  `obj/<cfg>/<instance>/arch/...` (`zlib-ng` static, `zlib-ng@shared`;
  until step 4 `obj/<cfg>/<variant>.<set>/`), not `CMakeFiles/<variant>.dir/`.
- **universal2.** CMake's build compiles no ISA sources in either slice,
  and `arm_features.c` in the x86_64 one. buildlog builds each slice as its
  own configuration (`arm64-macos.release.none` is shared with the release
  configuration), then lipos:
  - `libz-ng.a`;
  - `libz-ng.2.3.3.dylib` with its links (a core change, below);
  - the eight programs.
- **Checks: spellings.**
  - A check's program is one line: a fact value cannot hold a line break
    (`facts.py`: "contains a tab or line break"). So the `#include`s are
    `cc_check_headers`, and upstream's preprocessor conditionals are
    resolved:
    - `HAVE_CPUID_GNU` calls `__cpuid_count(...)` instead of testing the
      macro with `#ifndef` and taking its address (check_symbol_exists).
      The answers are the same: yes on x86_64, no on arm64.
    - The visibility probes spell the attribute instead of `#define
      Z_INTERNAL`.
    - `HAVE_ARMV8_INLINE_ASM` keeps only its `__aarch64__` branch; the
      32-bit ARM branch is irrelevant on these platforms.
  - `check_c_source_compiles(... FAIL_REGEX "not supported")` and
    `check_c_compiler_flag` become `-Werror` in the check's args.
    buildlog's check has no FAIL_REGEX.
  - `ADDITIONAL_CHECK_FLAGS` (the three `-Werror=…` whose own checks answer
    yes) are written literally into the dependent checks' args: a check's
    args cannot depend on another check's answer.
  - `check_type_size(__off64_t)` is not asked: no rule reads its answer.
  - `HAVE_MARCH_ARMV8_CRC` is a fact, not a probe (above).
- **Checks: which are asked.** Upstream asks the x86 probes only when
  `BASEARCH_X86_FOUND`, and the arm ones only on arm. buildlog asks every
  check in every configuration, so there are 38 × 2 answers. The foreign
  ones answer no and are harmless, but they are wasted probes and show up
  in `why --demand '*'`.
- **Tests.** ctest runs 69 (70 with gtest) tests. Most are `cmake -P`
  scripts that pipe files through minigzip/minideflate and compare output
  (`minigzip-*`, `CVE-*`, `GH-*`, `minigzip-paper-*`), or compare a
  generated table with the source (`makefixed`, `maketrees`, `makecrct`).
  buildlog's test has no stdin, output file or exit-status assertion (the
  `--invalid` tests pass on the usage message; upstream also checks exit
  status 64). So 7 tests are in scope. gtest_zlib is out of scope; it
  needs an arm64+x86_64 GTest anyway.
- **`buildlog test` and foreign platforms.** The x86_64 tests are "not
  run" (built for x86_64-macos on an arm64 build machine). Rosetta runs
  them fine; pytest does so with `arch -x86_64`.
- **Configure-time options.** zlib-ng's 14 `WITH_*` options that decide the
  default build are `option_decl`s at upstream's defaults. The options of
  other architectures (POWER, s390x, RISC-V, LoongArch) and non-default
  modes (`ZLIB_COMPAT`, `WITH_NATIVE_INSTRUCTIONS`, `WITH_SANITIZER`) are
  not modelled.
- **`ZLIB_COMPAT=ON` is not a cheap second configuration.** It changes:
  - the library name (`z` vs `z-ng`) and SOVERSION (1 vs 2);
  - the version string (1.3.1.zlib-ng);
  - every generated header (`zconf.h`, `zlib.h`, `zlib_name_mangling.h`
    and `gzread_mangle.h` from different templates);
  - the definitions (`ZLIB_COMPAT` instead of `ZLIBNG_NATIVE_API`);
  - forces `WITH_GZFILEOP`;
  - the installed `.pc` name.

  In buildlog these are all option-conditioned facts: `library_prop_when`
  and `configure_file_*` under `$Eq(option.ZLIB_COMPAT, on)`. It is a
  second project-shaped configuration (roughly configured.toml doubled),
  not one attribute. Not built.
- **`${exec_prefix}`/`${prefix}` in zlib-ng.pc.** A spec value has no
  escape for `${`, so `buildlog.toml` defines the vars `exec_prefix` and
  `prefix` as themselves.
- **Shared-library versioning (upstream vs. here).**

  | | upstream (CMakeLists.txt 1455-1500, 1560-1575) | buildlog |
  |---|---|---|
  | SOVERSION | 2 (`ZLIB_COMPAT=ON`: 1) | `native_version(…, "2.3.3", "2")` via `library_version` (spec.toml:158) |
  | VERSION | `ZLIB_FULL_VERSION` = 2.3.3 (compat: 1.3.1.zlib-ng) | 2.3.3 |
  | macOS install name | `@rpath/libz-ng.2.dylib` | same |
  | macOS compatibility / current version | 2.0.0 / 2.3.3 | same, in all three configurations (checked with `otool -l`) |
  | file and links | `libz-ng.2.3.3.dylib`, `.2.dylib`, `.dylib` | same, fat in universal2 |
  | ELF symbol versions | `zlib-ng.map.in`: nodes `ZLIB_NG_2.0.0`, `ZLIB_NG_2.1.0`, … (not on Apple) | not modelled (not needed on macOS) |
  | package version file | `zlib-ng-config-version.cmake`, VERSION 2.3.3, `COMPATIBILITY AnyNewerVersion` | no package is exported |
  | pkg-config | `Version: 2.3.3` | same (byte-identical .pc) |

  So core already models the *build-side* versioning (`native_version` →
  file name, links, install name, `-compatibility_version`,
  `-current_version`). What is missing is any link from it to the
  *exported* version constraint; see Design input 13.

## Changes to buildlog core and plugins

1. **buildlog-cpp, `L4_cxx.dl` `link_not_c`** (bug fix). An object
   library's objects are C objects of the *object library* (`c_object(l,
   …)`), not of the target that folds them in, so every zlib-ng link used
   `clang++` (and the C++ runtime). The rules now read
   `!c_object(_, c, _, o)`.
2. **core, `L3_native.dl`: fat shared libraries.** `slice_artifact` now
   also comes from `shared_library`. The fat artifact is
   `out:lib/<u>/<file>` with the slices' shared file name. The universal
   configuration gets the same links (`libz-ng.2.dylib`, `libz-ng.dylib`)
   through `shared_link(t, u, …)`. Before this, requesting a shared library
   in universal2 was an error ("slice missing for multi-platform request").
   README.md's L3 summary is updated.
3. Lockfile hashes refreshed for both, in every `buildlog.lock`.

## Design input

Ranked by importance for step 4.

1. **Per-source conditions and properties (`c_source_when`, per-source
   flags) in buildlog-cpp.**
   - This is the capability under study, and buildlog has no direct
     spelling for it. CMake's `set_property(SOURCE … COMPILE_FLAGS)` and
     `list(APPEND SRCS …)` under `if()` became object libraries per
     (variant, set), with every variant property copied onto the set
     (`rules.dl` ~60-100) and 32 extra targets (16 sets × 2 variants).
   - It works, and the provenance is good (a set is a named thing
     `why` can show), but every project with per-TU flags would rewrite
     that abstraction.
   - A principled form: `c_source_when(t, src, cond)` and
     `source_prop(t, src, key, value)` in L4_cxx, where `c_object` is
     keyed by (t, src) and `action_arg` reads `use(t,…)` ∪
     `source_use(t, src, …)`. Or core-level "source groups" with their
     own props, of which an object library is the degenerate case.
   - *Step 4:* source groups in buildlog-cpp (the second form). See
     "Step 4 changes".
2. **An ISA knowledge package keyed by toolchain family, plus a generic
   ISA-compatibility relation.**
   - Only these relations are project facts: `isa_requires`,
     `isa_requires_when`, `isa_role`, `isa_define`. The rest of
     `isa.toml`'s first part is knowledge: `isa_ext`, `isa_baseline`,
     `isa_flag`, `isa_march`, `isa_march_ext`.
   - `isa_ext` and `isa_baseline` are knowledge about architectures,
     keyed by the platform's `cpu` constraint.
   - `isa_flag` and `isa_march*` are knowledge about the toolchain
     family: GCC and clang share them, MSVC would be `/arch:AVX2`.
   - The derived `isa_compatible`, `isa_coexist`, the `isa.compatible.<s>`
     attribute and both rejections are general. They belong next to
     `platform_constraint` (core L1/L2), with the spellings in
     buildlog-cpp's F layer.
   - The error "set needed where its cpu lacks the extensions" is the
     general form of the Halide case's "no <arch> slice of <lib>"
     (L3_native:160).
   - *Step 4:* done as design input §2 option (c): the knowledge and the
     spellings in buildlog-cpp (L2, F_clang), the ISA sets, their fit,
     flags and rejections in buildlog-cpp's L4. The `isa.compatible.<s>`
     attribute stays the project's: it names project sets and feeds their
     gates. See "Step 4 changes".
   - It should be a platform-compatibility check on *targets* (a target
     requires ISA extensions), not only on source sets, so that
     `requires_isa` propagates like a usage requirement.
3. **Probes that are toolchain facts.**
   - Of 38 checks, 26 are pure functions of (compiler, target CPU): the
     15 ISA probes and 11 compiler-feature probes. The 15 ISA probes
     answer "no" by construction on the foreign CPU (Checks, above).
   - `cc_check_known` is the right hook, but a knowledge package cannot
     derive its key: the check content is private (`cc_check`,
     `cc_check_args`, `cc_check_headers` in L4_checks), and so is
     `toolchain_pin`.
   - Either export a `cc_check_content(n, lang, kind, subject, headers,
     args)` view, or add a check kind `isa` whose answer is derived from
     `isa_ext` plus a per-family "supports extension e since version v"
     table and is probed only when the table is silent.
   - Separately, a check should be askable *where* it matters:
     `cc_check_when(name, cond)`, so x86 probes are not asked on arm64.
4. **Check spellings.**
   - Fact values need line breaks, or a check should take its program from
     a file (`cc_check_file`). Resolving `#ifdef`s by hand changes what
     is probed (HAVE_CPUID_GNU).
   - Add `FAIL_REGEX` or a `diagnostic_free` flag.
   - Add a `symbol` kind (`check_symbol_exists`: macro or declaration
     after the prelude).
   - Allow check args conditioned on other checks'
     answers (`ADDITIONAL_CHECK_FLAGS`).
5. **Fat shared libraries** were missing in core (fixed here, small). Tests
   of universal configurations: `buildlog test` has no universal2 tests
   and skips foreign-platform ones. A `platform_runs_on(p, host, via)` fact
   (Rosetta: x86_64-macos runs on arm64-macos via `arch -x86_64`) would
   let `buildlog test` run both slices. That is the natural test of a fat
   binary: each slice, on its own CPU.
6. **Directory-wide properties.** `add_definitions` and `CMAKE_C_FLAGS`
   reach every target of a directory. Here `dir_prop(_when)` is copied
   onto every target with a `c_source` (rules.dl, 2 rules), and the same
   has to be repeated for object libraries. A core notion of a
   project-wide (or directory) default property would do this once.
7. **Optimization level override.** A project that insists on `-O2` for
   release (zlib-ng rewrites `CMAKE_C_FLAGS_RELEASE`) ends up with both
   `-O3` and `-O2` on the line. buildlog-cpp could expose `native.opt` →
   flag mapping as an author-overridable fact
   (`opt_flag(tc_family, "release", "-O2")` with `[author_decides]`).
8. **Options are build-wide.** `option_decl` gives one value per build,
   and every configuration shares it. Per-configuration option values
   (`WITH_AVX512=off` only in universal2, for example) need a
   configuration-keyed `option_value`.
9. **`${` escape in spec values** (`$${exec_prefix}` or similar); today
   the self-referential vars are a trick.
10. **Tests need stdin, output files, exit-status expectations and output
    comparison** to cover the rest of upstream's 69 tests (the `cmake -P` ones). That is
    `test_stdin`, `test_expect_exit`, and `test_compare(out, file)`.
11. **why-not usability.** `why-not needed(set, cfg)` picked an irrelevant
    binding of `eff_dep`'s `d` (`zlib-ng-static.arm_features`) and needed
    manual chaining through `eff_ref` to reach `cond_holds`. Binding the
    variable from the goal (the goal's `d` is fixed) would give the answer
    in one step.
12. **NEON is architectural on AArch64**, but upstream passes
    `-march=armv8-a+simd` anyway. The model keeps upstream's flag
    (`isa_baseline` does not list neon). A knowledge base would mark neon
    baseline on arm64 and drop the flag; that is a deliberate upstream
    difference to decide.
13. **Tie the exported version constraint to the ABI version** (recorded
    only, not implemented; the facts are in the Ledger).
    - Today `native_version(t, v, so)` (build side) and
      `export_version`/`export_compat` (L3_install, the CPS
      `version`/`compat_version`) are independent facts. Nothing checks
      that one agrees with the other.
    - For zlib-ng the ABI says "same major". SOVERSION 2 is VERSION's
      major, and the dylib's compatibility_version is 2.0.0. Under
      `export_compat = same_major`, buildlog's CPS writer gives
      `compat_version = "2.0.0"`, exactly the dylib's compatibility
      version.
    - Upstream's own package file says `AnyNewerVersion` (CPS
      `compat_version = "0.0.0"`). That accepts a future 3.x, which by
      SOVERSION would be an ABI break. So upstream's CMake export is
      looser than its ABI. A buildlog export should warn when
      `export_compat` is looser than the ABI implies.
    - Proposal: when an exported component's target has
      `native_version(t, v, so)`, `export_version` defaults to `v` and
      `export_compat` defaults to the policy implied by `so`:
      - `same_major` when `so` is v's major;
      - `exact` when `so` is the full version;
      - otherwise an author decision.
    - An explicit `export_compat` looser than that is an error or
      warning, citing the `native_version` fact in `why`.
    - A downstream `requires zlib-ng >= 2.1` should mean both:
      - API: the provider's `version` ≥ 2.1, which is also the
        `ZLIB_NG_2.1.0` ELF symbol node;
      - ABI: the same `compat_version` line, `2.0.0`. At run time the
        macOS form is dyld's check that current_version ≥ the client's
        recorded compatibility_version; the ELF form is soname
        `libz-ng.so.2`.
    - For a static-only export only the API half applies.
    - `ZLIB_COMPAT=ON` has version `1.3.1.zlib-ng`, which is not a
      simple CPS version. That would need a CPS `version_schema` or a
      rule that strips the suffix, and SOVERSION 1 then maps to
      `compat_version 1.0.0`.

**Upstream issues.** #1016 and #1085 are solved: one configure yields a
universal2 build with correct per-slice ISA selection, and no manual
per-arch builds or hand-run lipo are needed. The silent universal2
degradation (no SIMD, arm_features.c in the x86_64 slice) cannot happen:
the arm set is incompatible with x86_64, and the error fires if anything
forces it there.

## UX input

Authoring and usability friction met while writing this project. Each
item gives what I wrote, what I wished I could write, and the lines
involved. Item 1 of Design input (per-source properties) and item 2 (ISA
knowledge) are the semantic gaps; the items here are about spelling.

**Spec size.** The rows below count non-comment, non-blank lines, with
source-list lines (one quoted `.c`/`.h` path per line) left out.

| buildlog project | lines | upstream CMake, same count | lines |
|---|---|---|---|
| spec.toml | 116 (+37 sources) | CMakeLists.txt | 1350 |
| isa.toml | 210 (+15 sources) | cmake/detect-intrinsics.cmake | 617 (x86 + arm macros: 361) |
| checks.toml | 200 | cmake/detect-arch.cmake | 90 |
| configured.toml | 60 | test/CMakeLists.txt | 203 |
| tests.toml | 69 | | |
| rules.dl | 66 | | |
| isa.dl | 75 | | |
| buildlog.toml + preset | 16 + 6 | | |
| **total** | **818** | **total** | **2260** |

- Upstream's line count also covers:
  - five other architectures (CMakeLists.txt 866-1045 plus their probes);
  - MSVC, `ZLIB_COMPAT`, sanitizers, install/export, gtest and fuzzers;
  - the `cmake -P` test scripts.
- The in-scope part of upstream is roughly 1,200-1,400 lines, an estimate
  (not counted).
- 141 of buildlog's lines are the Datalog, and about 40 of isa.toml are ISA
  knowledge. That is abstraction a plugin or knowledge package would carry
  (Design input 1-2), not project facts. Without them the project is about
  640 lines.
- The biggest single cost is checks.toml: 200 lines for 38 probes.

**Items, largest first.**

1. **Configure checks are 5 lines each, mostly boilerplate** (checks.toml,
   200 lines, 38 checks). All 38 say `lang = "c"`, and 13 have the
   same trivial `subject`:
   ```toml
   [cc_check.HAVE_CASCADE_LAKE]
   lang = "c"
   kind = "compiles"
   subject = "int main(void) { return 0; }"
   cc_check_args = "-Werror -mtune=cascadelake"
   ```
   Wished for:
   - a table-level default (`[cc_check_defaults] lang = "c"`);
   - a `flag` kind that supplies the trivial program;
   - one row per check:
   ```toml
   cc_check_flag = [["HAVE_CASCADE_LAKE", "-mtune=cascadelake"], ["FNO_LTO_AVAILABLE", "-fno-lto"], ...]
   cc_check_include = [["HAVE_ARM_ACLE_H", "arm_acle.h"], ...]
   ```
   Result: about 200 lines down to about 70.
2. **Condition aliases for check answers** (15 rows across spec.toml,
   isa.toml and configured.toml, 17 lines). Each one only names a check answer:
   ```toml
   ["arm_acle_h", "$Eq(cc.check.HAVE_ARM_ACLE_H, yes)"],
   ["no_fseeko", "$Eq(cc.check.HAVE_FSEEKO, no)"],
   ```
   Wished for: every `cc.check.X` is usable directly as a condition name,
   e.g. `when = "cc.check.HAVE_ARM_ACLE_H"` and `"!cc.check.HAVE_FSEEKO"`.
   This is the "two mechanisms" case: a check answer is already an
   attribute, but a `*_when` fact accepts only a condition name.
3. **Binary `$And`/`$Or` only** (38 `$And`s in isa.toml and spec.toml).
   Upstream's `cmake_dependent_option` chain becomes nested pairs:
   ```toml
   ["when_armv8", "$And($Eq(option.WITH_OPTIM, on), $And($Eq(option.WITH_ARMV8, on), $Or($Eq(cc.check.HAVE_ARMV8_INLINE_ASM, yes), $Eq(cc.check.HAVE_ARMV8_INTRIN, yes))))"],
   ```
   Wished for: an n-ary infix form, e.g.
   `"option.WITH_OPTIM & option.WITH_ARMV8 & (cc.check.HAVE_ARMV8_INLINE_ASM | cc.check.HAVE_ARMV8_INTRIN)"`,
   with on/yes as the implicit truth value. There are 16 gate conditions
   of 2-4 lines each (about 50 lines), roughly one line each after.
   - The first attempt spelled the macros without the `$` sigil and failed
     at configure; the fix came from reading other case studies, not from
     the error.
4. **The per-TU flags needed a Datalog abstraction** (rules.dl 44 lines
   for library variants and source sets, isa.dl 75 lines). "These files
   get these flags when X" should be a TOML table. What I wrote:
   ```toml
   [source_set.avx2]
   lib = "zlib-ng"
   source_set_source = ["arch/x86/slide_hash_avx2.c", ...]
   source_set_when = "when_avx2"
   isa_requires = ["avx2", "bmi2"]
   isa_role = "kernel"
   isa_define = "X86_AVX2"
   ```
   It sat on 18 rules copying variant properties onto each set. Wished for:
   ```toml
   [library.zlib-ng.sources.avx2]   # a per-source group of the library
   files = [...]
   when = "when_avx2"
   isa = ["avx2", "bmi2"]           # the flags derived from isa knowledge
   define = "X86_AVX2"              # directory/library-wide when built
   ```
   - `lib = "zlib-ng"` is on all 13 sets and `isa_role = "kernel"` on 11.
   - Both are defaults of the enclosing library.
5. **The same file list three times** (configured.toml, 3 lines × 4 names):
   ```toml
   configure_file = ["zconf-ng.h", "zlib-ng.h", "gzread_mangle.h", "zlib_name_mangling-ng.h"]
   cc_configured_header = [ ...same 4... ]
   configure_file_at_only = [ ...same 4... ]
   ```
   Wished for: one table per file with flags, e.g.
   `configure_file."zconf-ng.h" = { header = true, at_only = true, lines = [...] }`.
   - `configure_file_lines` needs the dict form
     `{"zconf-ng.h" = [[...]], ...}` inside a subject table; my first,
     list-shaped attempt failed at configure.
   - After tombi's formatting the dict is 8 lines and hard to read.
6. **Probe programs must be one line.** A fact value cannot hold a line
   break, so upstream's multi-line probes, `#ifdef`s included, were
   squeezed onto one line and their conditionals resolved by hand.
   Wished for: TOML `"""` multi-line strings, or
   `subject_file = "probes/cpuid_gnu.c"`. It affected 11 probes.
7. **`tests.toml` repeats every program three times** (26 lines):
   ```toml
   request_all = [["example"], ["minigzip"], ...]          # 8 rows
   library_include = [["example", "zlib-ng"], ...]          # 8 rows
   test = [["example", "example"], ...]
   ```
   Wished for: `[executable.example] sources = [...]; include_library =
   "zlib-ng"; test = true`, where declaring a test requests its program.
   - I requested the programs explicitly (`request_all`) so that a plain
     build builds them; declaring a test could imply that.
8. **Per-configuration settings repeated per configuration** (spec.toml, 8
   lines):
   ```toml
   config_decl = [["release", "arm64-macos"], ["x86_64", "x86_64-macos"], ["universal2", "universal2-macos"]]
   config_decl_setting = [["release", "native.opt", "release"], ["x86_64", "native.opt", "release"], ["universal2", "native.opt", "release"]]
   ```
   Wished for: `config_default_setting = [["native.opt", "release"]]`, or
   `[config.x86_64] platform = "x86_64-macos"` with settings inline.
9. **`toolchain_pin` needs a row per (language, host, target)** (preset, 4
   rows):
   ```toml
   ["c", "arm64-macos", "arm64-macos", "apple-clang"],
   ["native-link", "arm64-macos", "arm64-macos", "apple-clang"],
   ["c", "arm64-macos", "x86_64-macos", "apple-clang"],
   ["native-link", "arm64-macos", "x86_64-macos", "apple-clang"],
   ```
   Wished for: `toolchain = "apple-clang"`, meaning every language and
   target it supports. universal2 needs both targets pinned, and nothing
   says so until configure fails.
   - A 9-line file just for this is the sprawl case. The `--user`
     preset path is also long to type on every configure.
10. **Directory-wide properties via a hand-written rule** (rules.dl, 2 rules
    plus 2 declarations):
    ```datalog
    prop(t, k, v, "private") :- dir_prop(k, v), c_source(t, _).
    ```
    Wished for: a `[project] compile_flag = [...]`, `define_when = [...]`
    table in core or buildlog-cpp (Design input 6).
11. **`why` for the headline question takes flags you have to know**:
    ```
    buildlog why -C BUILD --with-demand --leaves \
      --output obj/x86_64-macos.release.none/zlib-ng/arch/x86/adler32_avx2.c.o --flag=-mavx2 PROJECT
    ```
    - Without `--with-demand` the tree omits the demand side: why the set
      is built at all.
    - Without `--leaves` it is several screens long.
    - The object path has to be copied from the Ninja file.

    Wished for: `buildlog why flag -mavx2 arch/x86/adler32_avx2.c
    --config x86_64`, taking a source path, not an output path.
    *Since the UX pass:* `why --output arch/x86/adler32_avx2.c --config x86_64
    --flag=-mavx2` takes the source and the declared configuration, and gives
    the same answer (53 lines with `--with-demand --leaves`, 30 Sep).
12. **`why` on a `config_attr` needs the configuration's internal ADT
    spelling**:
    ```
    'config_attr(["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], nil]]], "isa.avx2", "off")'
    ```
    (Since step 4's `native.linkage` key it has a third setting,
    `[["native.linkage", "default"], nil]`, and the old spelling answers
    only "Tuple not found".)
    - This is unreadable, and I got it only from a previous `why`
      output.
    - The output itself prints the name `‹arm64-macos.release.none›`.

    Wished for: accept the printed config name (or `--config release`) in
    goals.
    - In the output, `$Not($And(…, …))` condition trees are cut off with
      `…` just where the failing leaf is, so the reader has to find it
      by elimination.
13. **why-not picks the wrong binding.** It took three chained commands to
    get from `needed(set, cfg)` to `cond_holds`: the first step followed
    `eff_dep` for an unrelated set (Design input 11).
14. **Private plugin relations.** isa.dl first read `cc_check_args` and
    `toolchain_pin` (private to buildlog-cpp) to derive probe answers, and
    that design had to be dropped after configure refused it.
    - Nothing up front tells an author which plugin relations are public.
    - Wished for: the visibility named in the error, and a list of public
      relations (`buildlog relations --public buildlog-cpp`).
15. **Subject-table key gotcha.** Inside `[cc_check.X]` or
    `[library.X]`, *every* key becomes `key(X, value)`. A key like
    `request = "x86_64"` becomes `request(X, x86_64)`, not an option or
    an error.
    - Wished for: an unknown or ill-arity key under a subject table is an
      error listing that subject's relations.
16. **Errors surface as SystemExit with details only in metrics.json**
    (programmatic `configure_project(check_only=True)` in the tests).
    Wished for: an exception carrying the error list.
17. **`${` has no escape** (buildlog.toml, 2 lines):
    `exec_prefix = "${exec_prefix}"` just to emit a literal `${exec_prefix}`
    into zlib-ng.pc. Wished for: `$${exec_prefix}`.
18. **Build layout and CLI.**
    - The Ninja file is under `BUILD/souffle`, so the build command is
      `ninja -C BUILD/souffle`, not `ninja -C BUILD`.
    - The installed-looking artifacts are also under `souffle/lib/…`
      and `souffle/obj/<config-long-name>/`.
    - Wished for: `buildlog build -C BUILD -j2`, or Ninja at the build
      root.
    - A fresh configure of all three configurations runs 76 probe
      answers (about 25 s), including the ISA probes on the foreign CPU,
      which answer no by construction (Design input 3).
19. **`buildlog test` "not run" on foreign platforms gives no hint** that
    Rosetta could run them (Design input 5). Every universal2 test is
    reported for one slice only.
20. **Changing a `.dl` layer ripples into every `buildlog.lock` in the
    repository and the why goldens.**
    - The goldens changed only in Soufflé's renumbered `_` variables.
    - Refreshing meant a sed over every lockfile. Wished for:
      `buildlog lock --refresh-all`, and goldens that normalize `_N`
      names.

## Tests (`tests/test_zlibng_case.py`)

They skip without the checkout, Soufflé or ninja.

- Every TU and its flags match the baseline (arm64, x86_64).
- The generated headers and `.pc` are byte-identical.
- Each slice's ISA functions match the baseline.
- universal2 has both slices' ISA sources in `libz-ng.a` and the dylib,
  fat files and dylib links; CMake's universal2 has none.
- The dylib's install name, versions and exported symbols match.
- No link uses `clang++`.
- `why --flag=-mavx2` leads to the ISA facts, the CPU constraint, the
  probe and the preset's toolchain pin.
- `why` for `isa.avx2 = off` on arm64 leads to `isa.compatible.avx2 = no`
  and the platform's CPU.
- `buildlog test` passes 7 tests. The x86_64 and both universal2 slices
  run under `arch`.
- Rejections: a NEON set needed in x86_64; a set requiring neon and avx2.

## Step 4 changes

Spec lines are measured with `historical source-line accounting` (non-blank, non-comment,
without single-path source-list lines).

| Change | Files | Before | After |
|---|---|---|---|
| §5: a source set is a buildlog-cpp source group of each variant. The object library per (variant, set), its five property-copying rules, `owner_member`, the `dep`/`dep_when` edges and `source_set_target` go | rules.dl | 66 | 55 |
| the ISA flags and the rejection are stated on the set (`source_set_prop(_when)`) and the compiled TU (`src_on`) | isa.dl | 75 | 73 |
| | total | 757 | 744 |
| §2: the ISA knowledge (`isa_ext`, `isa_baseline`, the spellings) and the generic model (`isa_compatible`, `isa_coexist`, the flags and `-march` join, the errors, the per-TU rejection) move to buildlog-cpp; the project maps each set to its variants' groups | isa.toml | 138 | 99 |
| | isa.dl | 73 | 31 |
| | total (after the UX port) | 591 | 510 |
| One library kind (the PI's decision): `library_variant` goes; `zlib-ng` is one `library` with `default_linkage = "both"` (core's linkage twins), `ZLIB_DLL` a `linkage_prop`, the shared-only flag `shared_prop_when`; the source sets attach to the library | rules.dl | 55 | 50 |
| | spec.toml | 87 | 87 |
| each test program pins the linkage it links, with upstream's reason (`dep_linkage`) | tests.toml | 54 | 59 |
| | total | 510 | 510 |

- **Targets:** 32 fewer (16 sets × 2 variants). Object directories in the
  snapshot graph go from 40 to 10.
- **Graph:** equivalent. After renaming `obj/<cfg>/<variant>.<set>/` to
  `obj/<cfg>/<variant>/` and comparing each edge's tokens as a multiset, the
  same 246 edges have the same inputs and commands. Only the order of flags
  differs: a group's flags come after the variant's, where they were merged
  into one sorted set before. The CMake comparison (`compare.py`) is
  unchanged and passes.
- **The rejection** is now per TU. Any target that compiles a set's source
  where the cpu lacks the set's extensions is an error, not only the set's
  own object library. The test compiles `arch/arm/adler32_neon.c` in an
  x86_64 target outside the set.
- **One library kind.** The graph is identical after renaming the object
  directories: the static objects move from `obj/<cfg>/zlib-ng-static/` to
  `obj/<cfg>/zlib-ng/`, the shared ones from `obj/<cfg>/zlib-ng/` to
  `obj/<cfg>/zlib-ng@shared/` (graph_snapshot, `sed` both, `diff` empty).
  Every artifact keeps its name: `libz-ng.a`, `libz-ng.2.3.3.dylib` and its
  links, in each configuration and in universal2. `compare.py` maps the
  two instances to CMake's targets and passes.
- **§2 (ISA vocabulary).** The graph is identical to the one before the
  port (snapshot diff empty). `why` for `-mavx2` now ends at
  `family_isa_flag("clang", "avx2", "-mavx2")` and `isa_ext("x86_64",
  "avx2")` in buildlog-cpp's layers instead of isa.toml. The rejection's
  message says "ISA scope"; the test adds an unknown extension
  (`avx10.2`). buildlog-cpp's side is 220 lines (layers and manifest,
  comments included), shared with ggml.

## UX pass port

The port moved the fourteen options to an `[option]` table and every option and check condition
to infix syntax in the row that uses it: `source_set_when = "WITH_AVX2 & cc.check.HAVE_AVX2_INTRIN
& isa.sse42 == on"` replaces a named `when_avx2` and its `$And($Eq(...), ...)` text, and
`dir_prop_when` rows name the check (`cc.check.HAVE_SYS_SDT_H`) instead of a one-use condition.
One named condition is left (`x86_fallbacks`, used by three source sets). `[defaults.source_set]`
states `lib = "zlib-ng"` once per file, and `[defaults.target]` gives every test program its kind
and `library_include`. isa.dl's `-march` join uses `join_part`. The library/variant and
source-set abstractions (rules.dl) and the ISA model (isa.dl) stay project Datalog: the first is
the zlib case study's two-variant library, which core's `target_template` could replace only
together with the source-set and configured-file rules keyed on it; the ISA model is step 4's (§2).
`checks.toml` uses buildlog-cpp's compact check tables (T2): flag, header, type and function
checks are one `NAME = "..."` line each under `[cc_check_flag]`, `[cc_check_include]`, ... and
the programs are `[cc_check.X]` tables under `[defaults.cc_check] lang/kind`; the probes' content
is unchanged (the same 88 probe-cache keys).

| file | before (4a1ccb0) | after |
|---|---:|---:|
| buildlog.toml | 16 | 16 |
| checks.toml | 200 | 114 |
| configured.toml | 60 | 54 |
| isa.dl | 75 | 73 |
| isa.toml | 210 | 138 |
| rules.dl | 66 | 55 |
| spec.toml | 116 | 87 |
| tests.toml | 69 | 54 |
| total | 812 | 591 |
| Datalog | 141 | 128 |

(rules.dl's 66 → 55 is step 4's source-group port, before this one.) Graph identity: the
release, x86_64 and universal2 configurations compare `IDENTICAL` with `ninja_norm.py`.

## Linux (GCC 13 and Clang 18)

`linux/run.sh cases gcc|clang zlib-ng` builds the native `release` configuration and runs
the smoke tests. `linux/run.sh baselines` runs `baseline/run-cmake.sh native` (the
x86_64 and universal2 slices are macOS-only), with GTest from the image's
`/opt/googletest`, and writes `baseline/linux/native/` and `baseline/linux-clang/native/`.
On Linux the tests compare all 90 translation units' flags with upstream's (equal for GCC
and for Clang), the generated headers and `zlib-ng.pc` byte for byte, the ISA functions
the library defines, and the exported function names. The x86_64 and universal2 tests
(lipo, Rosetta) are macOS-only and skip: 10 pass, 5 skip.

The spec stated macOS facts; they are now conditions:

- **Checks by family.** CMake's `ADDITIONAL_CHECK_FLAGS` are the `-Werror=unguarded-availability`
  family where the compiler accepts them, which GCC does not. `HAVE_FSEEKO`,
  `HAVE_STRERROR` and `HAVE_NO_INTERPOSITION` state GCC's plain arguments with
  `cc_check_args_family` (buildlog-cpp; before, GCC answered no and the objects got
  `-DNO_FSEEKO -DNO_STRERROR` and lost `-fno-semantic-interposition`).
- **`HAVE_SYMVER`** on Linux (upstream: `NOT APPLE`), and **`ARM_AUXV_HAS_CRC32`** from a
  `getauxval` probe on Linux aarch64.
- **ISA symbols** have no leading underscore on ELF; the baseline's `isa-functions.txt`
  names the architecture `aarch64` (`arm64` here).

Residual: upstream links the shared library with a version script
(`-Wl,--version-script`, symbols `zng_*@@ZLIB_NG_2.0.0`), which buildlog cannot state;
the exported names are the same, unversioned.
