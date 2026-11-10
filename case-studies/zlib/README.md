<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: building zlib with buildlog

zlib 1.3.2 is small, and its `CMakeLists.txt` uses the configure-time
machinery the Halide case study never needed: `check_*` probes, generated
headers (`configure_file`), and one source set built as a shared and a static
library. This study builds it with buildlog and compares the result with
zlib's own CMake build on this Mac. It covers the build, the tests and the
generated `zlib.pc`; install and export were added in step 4 (phase 6).

## Layout

```
case-studies/zlib/
  README.md              this file (and the ledger)
  baseline/
    run-cmake.sh         CMake configure/build/test/install, writes the normalized files below
    environment.txt      tool versions and the zlib pin
    commands.txt         `ninja -t commands`, with $SRC/$BUILD for the source and build dirs
    checks.txt           the HAVE_* check results (CMakeCache.txt)
    zconf.h, zlib.pc     CMake's generated files ($PREFIX for the install prefix)
    exported-symbols.txt `nm -gU` of libz.1.3.2.dylib and libz.a
    dylib-metadata.txt   symlinks, install name, versions, the programs' load commands
    ctest.txt            ctest's summary (14/14)
    example-output.txt   what zlib_example and zlib_static_example print
    raw/                 gitignored: the build and install trees
  project/               the author's project: zlib as its CMakeLists.txt states it
    buildlog.toml        the spec list; [vars] ZLIB_SRC (the checkout) and INSTALL_PREFIX
    spec.toml            the configurations (release, release-static), the checks, the library
    configured.toml      the configured files: zconf.h (with the public headers) and zlib.pc
    tests.toml           test/CMakeLists.txt's programs and tests
    install.toml         the install and the ZLIB package (step 4)
    presets/apple-clang-arm64.toml   the user file for this Mac: the toolchain pins
```

## Pinned inputs

- **zlib v1.3.2**, commit `da607da7`, the submodule `case-studies/zlib/upstream` (https://github.com/madler/zlib.git).
  The `ZLIB_SRC` variable overrides the path (`-D ZLIB_SRC=...` or
  `BUILDLOG_ZLIB_SRC`). `run-cmake.sh` checks the commit and a clean tree
  first, and builds out of tree.
- **Tools:** CMake 4.4.3, Ninja 1.13.2, Apple clang 21.0.0 (`/usr/bin/cc`),
  macOS 26.5.2 on arm64 (`baseline/environment.txt`).
- **CMake configuration:** `-G Ninja -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_INSTALL_PREFIX=<staging dir> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`,
  with everything else at its default (shared, static and tests on).

## Running

```sh
case-studies/zlib/baseline/run-cmake.sh            # the CMake baseline
uv run --frozen buildlog configure --user case-studies/zlib/project/presets/apple-clang-arm64.toml \
  -C build/zlib case-studies/zlib/project
ninja -C build/zlib/souffle
uv run --frozen buildlog test -C build/zlib
uv run --frozen pytest -q tests/test_zlib_phase*.py
```

## UX pass port

The UX pass ports the remaining project Datalog and positional target rows to
the vocabulary while leaving `cc_check*` tables unchanged. `zlib` and its tests
are now `[target.*]` tables with `kind`, `sources`, `deps`, property maps,
`test = true` and infix conditions. `install.toml` uses `[export.ZLIB]`. The
library abstraction in `rules.dl` is gone (core's `library` and the target
tables replace it); two rules stay: zlib.pc's `zlib_VERSION` is the library's
`native_version`, stated once, and the configured `zconf.h` directory comes
before `src:` (`cc_include_before`).

| files | before (4a1ccb0) | after | Datalog before → after |
|---|---:|---:|---:|
| project | 126 | 102 | 24 → 2 |

Graph identity is `IDENTICAL` for the apple-clang-arm64 preset (both
configurations). Remaining gaps: configure checks wait for compact check
tables, and install.toml (step 4) names the configured `zconf.h` per
configuration because there is no configured-header install helper.

T2: the five checks are compact tables (`[cc_check_include]`, `[cc_check_function]`,
`[cc_check_type]`, `[cc_check_compiles]`, with their preludes and arguments in
`[cc_check_headers]`/`[cc_check_args]`); 102 → 93 lines. Graph `IDENTICAL`.

## Ledger

Each phase has an acceptance check against the baseline (its test) and the
differences that remain, each with its reason. The phases describe the
project as it was then; since step 4, zlib is one library whose linkage is
chosen, and CMake's static twins are the release-static configuration
("Step 4 changes" below).

### Phase 1: libz.a and the static programs

The acceptance test is `tests/test_zlib_phase1.py`.

**How zlib is stated.** `rules.dl` is a small library abstraction. `library`
is one source set with its usage requirements (`library_source`,
`library_prop`), and `library_variant` builds it as one or more kinds. The
spec names zlib's 15 sources and its properties once. `zlibstatic` is a
static variant, and its `library_output_name` "z" names it `libz.a` through
core's `native_output_name` (CMake's `OUTPUT_NAME`). The test programs are
ordinary targets (`tests.toml`), each with the compile definitions that
`test/CMakeLists.txt` gives it.

**Matches the baseline.**
- The same 17 objects: 15 library sources and one per program.
- Per object, the same defines (apart from the check-decided HAVE_HIDDEN
  and NO_FSEEKO, see phase 3) and the same include dirs in the same order:
  `-I<generated-header dir> -I<source dir>` on the library's objects in both builds
  (the programs differ, below).
- `libz.a` has the same 15 members and the same global symbols.
- The programs link `libz.a` with the C driver and load only libSystem.
- `zlib_static_example` passes, and `static_minigzip` round-trips.

**Differs, and why.**
- **Compiler spelling.** buildlog runs the Xcode toolchain's clang by path.
  CMake runs `/usr/bin/cc`, which is the same Apple clang 21 through the
  xcrun shim.
- **Optimization level: now the same.** buildlog-cpp's clang `native.opt=release` was
  `-O2 -DNDEBUG` and CMake's `CMAKE_C_FLAGS_RELEASE` is `-O3 -DNDEBUG`. buildlog-cpp
  now uses `-O3`, a core policy decision taken on 2026-09-29, not a zlib override.
- **`--target=arm64-apple-macos13 -isysroot <SDK>` vs `-arch arm64`.**
- **`-isystem` vs `-I` on the test programs** (buildlog-cpp's include-directory order). The programs see zlib's dirs as a dependency's, `-isystem`; CMake gives
  in-project dirs `-I`. The library's own objects are `-I` in both since 2026-09-30, when the
  library took ownership of zconf.h (below); before, a synthetic `zlib_headers` target owned
  it and every compile had both dirs `-isystem`.
  buildlog states the target triple, deployment target and SDK explicitly
  ("choice is the enemy"). CMake leaves them to the compiler's defaults. As
  a result the binaries' `LC_BUILD_VERSION` minos is 13.0 for buildlog and
  26.0 for CMake (the host OS).
- **Depfile flags.** buildlog passes `-MD -MF <o>.d`. CMake adds `-MT <o>`,
  which only restates the default target.
- **Archiving.** buildlog runs `ar rcs`. CMake runs `rm -f; ar qc; ranlib`.
  The archives have the same members and symbol index, but the member order
  differs: buildlog's is lexical (`infback` before `inflate`), CMake's
  follows `ZLIB_SRCS`. Link results do not depend on member order.
- **Paths.** Objects go to `obj/<cfg>/<target>/`, the library to
  `lib/<cfg>/libz.a` and programs to `bin/<cfg>/`.
- **Program links.** CMake adds `-Wl,-search_paths_first`, which is ld64's
  default behaviour, and `-Wl,-headerpad_max_install_names`, which leaves
  room for `install_name_tool` at install time. Install is out of scope.
- **Generated-header dir (until phase 4).** Phases 1 to 3 used `baseline/`,
  which holds a byte-identical copy of CMake's generated `zconf.h`, as the
  include dir in the role of CMake's binary dir. Since phase 4 buildlog
  generates `zconf.h` itself (below).
- **Link driver (a core/plugin change).** Before this study, buildlog
  linked every program with the toolchain's `link` tool (`clang++`), which
  loaded libc++ into C programs. CMake links with the linker language's
  driver. Core now has an open `link_role`, and buildlog-cpp uses the C
  driver when a target's whole link closure is C.

### Phase 2: libz.1.3.2.dylib and the shared programs

Acceptance test: `tests/test_zlib_phase2.py`.

`zlib` is the same library's shared variant. It gets `ZLIB_DLL` (CMake's
`DEFINE_SYMBOL`) and `native_version` 1.3.2 with soversion 1, both derived
in `rules.dl` from `library_define_symbol` and `library_version`. No rows
are repeated per target.

**Matches the baseline.**
- The same 17 objects, with the same defines apart from the checks.
  `ZLIB_DLL` is on the library's objects only. `-fPIC` is on the library's
  objects in both builds.
- The files are `libz.1.3.2.dylib`, plus the symlinks
  `libz.1.dylib -> libz.1.3.2.dylib` and `libz.dylib -> libz.1.dylib`.
- The install name is `@rpath/libz.1.dylib`.
- The compatibility version is 1.0.0 and the current version is 1.3.2. The
  `otool -L` output of the dylib is identical.
- The four programs' `otool -L` output is identical: the shared ones load
  `@rpath/libz.1.dylib` with the same versions, the static ones load
  libSystem only.
- `libz.a` exports the same symbols.

**The versions bug (fixed in core).** buildlog used to pass the full version
as both `-compatibility_version` and `-current_version`, which would have
given 1.3.2/1.3.2. The compatibility version now comes from the soversion,
and each version is padded to X.Y.Z as CMake spells them. libHalide's graph
does not change: its version 22.0.0 with soversion 22 gives 22.0.0 both
ways, as in Halide's CMake baseline. `configure` of
`case-studies/halide/libhalide` before and after the fix writes a
byte-identical `build.ninja`.

**Differs, and why.**
- **Exported symbols (until phase 3).** The dylib exports the baseline's
  symbols plus zlib's internal ones (`_tr_*`, `inflate_fast`, `zcalloc`,
  …). Without the HAVE_HIDDEN check, `ZLIB_INTERNAL` is empty. Phase 3
  closes this.
- **Link flags.** CMake passes the compile flags (`-O3 -DNDEBUG -arch
  arm64`) and `-Wl,-headerpad_max_install_names` to the dylib link, and
  `-Wl,-search_paths_first` to program links. buildlog passes its target
  flags only. Neither `-O3` nor `-DNDEBUG` has any effect at link time.
- **Build-tree rpath.** buildlog uses
  `-Wl,-rpath,@loader_path/../../lib/<cfg>`, which is relocatable. CMake
  uses the absolute build dir. Both resolve `@rpath/libz.1.dylib` to the
  build tree's library.

### Phase 3: configure checks as discovery

The acceptance test is `tests/test_zlib_phase3.py`.

CMakeLists.txt has five checks: `check_type_size(off64_t)` with `-D_LARGEFILE64_SOURCE=1`,
`check_function_exists(fseeko)`, `check_include_file` for `stdarg.h` and for `unistd.h`,
and `check_c_source_compiles` for the hidden-visibility attribute, which runs with
`-Werror`. In `spec.toml` each of these is a buildlog-cpp `cc_check`, and each answer is a
probe result. Conditions on `cc.check.<name>` add the definitions:
- `HAVE_HIDDEN`: on the library variants and on `minigzip` and `zlib_static_example`, as in
  `test/CMakeLists.txt`;
- `NO_FSEEKO`: when fseeko is missing;
- `_LARGEFILE64_SOURCE=1`: public, when off64_t exists.

**Matches the baseline.**
- All five answers agree with CMakeCache.txt: off64_t no, and every other check yes.
- Every object's definitions equal CMake's, except `NO_FSEEKO` (below). `HAVE_HIDDEN` is on
  exactly the objects where CMake has it.
- The dylib's exported symbols now equal CMake's `nm -gU` exactly (`test_zlib_phase2`). The
  21 internal functions are hidden again.
- `buildlog why --flag -DHAVE_HIDDEN` leads to the probe's result for the whole question
  (toolchain, platforms, language, kind, program, prelude `stdlib.h`, arguments `-Werror`)
  and to the spec lines that asked the question and consumed the answer.
- A reconfigure runs no probe: the answers come from `probe-cache.json`.
- A user file that states the answer (`cc_check_known`) replaces the probe. The consuming
  rules do not change, and `why` then leads to the user file's line.

**Differs, and why.**
- **`NO_FSEEKO` (a CMake bug, not reproduced).** CMake writes
  `$<$<BOOL:NOT:${HAVE_FSEEKO}>:NO_FSEEKO>`. `$<BOOL:NOT:1>` tests the string "NOT:1", which
  is true, so CMake defines `NO_FSEEKO` everywhere. The spec states the intended condition,
  fseeko missing, which is false on this Mac. No file the build compiles reads `NO_FSEEKO`,
  so the output is unaffected.
- **The type check's prelude is stated.** `check_type_size` includes `sys/types.h`,
  `stdint.h` and `stddef.h` if three further, implicit checks find them (CMake's
  `HAVE_SYS_TYPES_H`, `HAVE_STDINT_H` and `HAVE_STDDEF_H`). The spec states these three
  headers as the prelude, so there are no hidden checks and no choice between preludes.
- **Check commands.** A check compiles and links with the configuration's toolchain,
  `--target` and `-isysroot`, plus the check's own arguments. It sees no configuration
  setting and no target property. CMake's `try_compile` of the same check runs
  `/usr/bin/cc -DHAVE___ATTR__VIS_HIDDEN -Werror -arch arm64 … -c src.c`, then links
  (`baseline/raw/cmake-build/CMakeFiles/CMakeConfigureLog.yaml`). The two commands differ
  only in how they spell the target and in the result define CMake adds.
- **The 64-bit test programs** (`zlib_example64`, `zlib_static_example64`) exist in CMake
  only when off64_t exists. They are not stated here, because buildlog has no conditional
  request and off64_t is absent on macOS.

### Phase 4: generated zconf.h and zlib.pc

The acceptance test is `tests/test_zlib_phase4.py`.

Both files are core configured files (`configured.toml`). Their variables are properties of
the targets that own them, and `buildlog -E configure_file` substitutes them.
- The library `zlib` owns `zconf.h` (`[library.zlib]`; `rules.dl` maps each variant to it
  with `owner_member(l, t) :- library_variant(l, t, _).`). One file per configuration,
  `gen/<cfg>/zlib/zconf.h`, is shared by both variants, and its dir, its order-only edge and
  the `configure.*` variables are each variant's own (public) properties, so the variants
  compile it `-I` and consumers `-isystem`, as CMake's PUBLIC dirs. Its template is
  CMakeLists.txt's assembled `zconf.h.cmakein`, stated as pieces: lines 1-10 of `zconf.h`,
  the three `#cmakedefine` lines, a blank line (CMake's byte 244, read twice), and lines 11
  to the end. `HAVE_STDARG_H` and `HAVE_UNISTD_H` are the checks' answers
  (`cc_check_configure`). `Z_PREFIX` is undefined (the option `ZLIB_PREFIX`, off).
- `zlib_pc` generates `zlib.pc` from `zlib.pc.cmakein`, at-only. `zlib_VERSION` is the
  library's version (`library_version_variable` in `rules.dl`), the prefix is
  `[vars] INSTALL_PREFIX`, and the lib and include dirs are GNUInstallDirs' defaults.
- The source dir `src:` is a public `library_prop` of both variants.

**Matches the baseline.**
- `zconf.h` is byte-identical to `baseline/zconf.h`.
- `zlib.pc` is byte-identical to `baseline/zlib.pc` once the baseline's `$PREFIX` is replaced
  by the prefix passed with `-D INSTALL_PREFIX=...`.
- Every compile that includes zlib.h has the generated dir before the source dir, as CMake
  has `${zlib_BINARY_DIR}` before `${zlib_SOURCE_DIR}`. `static_minigzip` includes the
  source `zconf.h` through zlib.h's `"zconf.h"`, as it does under CMake.
- `buildlog why --flag -DHAVE_UNISTD_H=1 --output gen/<cfg>/zlib/zconf.h` leads to
  the `unistd.h` check's probe result. `why` on `-Dzlib_VERSION=1.3.2` leads to
  `library_version`.

**Differs, and why.**
- **Configure time vs build time.** CMake writes both files while it configures. Here they
  are build actions (restat, written only when the content changes), so `why` can explain
  them, and a template edit reruns only the action.
- **The install prefix** defaults to `/usr/local`, as CMake's does. It is a `[vars]` entry
  that the invoker overrides, because install is out of scope for this study.
- **Include order** is stated: `rules.dl` has `cc_include_before(d, "src:") :-
  cc_configured_header_dir("zlib", _, d).` (the generated dir before the source dir,
  in whatever class they share). Before 2026-09-29 it was right only because the emitter
  orders a class lexically and `gen/...` sorts first. `buildlog why --flag "-I
  gen/<cfg>/zlib" --output obj/<cfg>/zlibstatic/adler32.c.o` shows the rule and the
  library's `configure_file("zlib", "zconf.h")` fact (`tests/test_zlib_phase4.py`).


### Phase 5: tests

The acceptance test is `tests/test_zlib_phase5.py`.

`tests.toml` states ctest's `zlib_example` and `zlib_static_example` as `test` facts. There
are no arguments and no environment: `ZLIB_findTestEnv`, which would set `PATH`, is defined
in test/CMakeLists.txt but never called. `buildlog test -C BUILD` builds what the tests need
and runs each one in `test/<cfg>/<name>`.

**Matches the baseline.**
- Both tests pass, as they do under ctest (`baseline/ctest.txt`).
- Both programs print exactly what CMake's builds print (`baseline/example-output.txt`,
  which `run-cmake.sh` now records), including `compile flags = 0xa9` (zlibCompileFlags:
  the type sizes and options zconf.h and the defines give the library).

**Differs, and why.** ctest runs 14 tests; buildlog runs 2.
- **Out of scope (12).** `zlib_install` installs zlib. `zlib_find_package_*` (5) and
  `zlib_add_subdirectory_*` (6) configure, build and test consumer projects against the
  installed or embedded zlib, with CMake. They needed install and export, and the
  consumer projects are CMake projects. Since step 4, phase 6 runs upstream's
  `find_package_no_components_test` against buildlog's install (see "Step 4 changes").
- **Not built on this Mac under CMake either.** `zlib_example64` and `zlib_static_example64`
  need `HAVE_OFF64_T` (phase 3). `zlib_coverage` and `zlib_coverage-summary` need a compiler
  whose id is `Clang` or `GNU`, and CMake calls this compiler `AppleClang`.

## Step 4 changes

Spec lines are measured with `historical source-line accounting` (non-blank, non-comment, without
single-path source-list lines).

| Change | Files | Before | After |
|---|---|---|---|
| §1(d): one `library` target replaces `library_variant` {zlib, zlibstatic}; the static twins become the release-static configuration | spec.toml, configured.toml, tests.toml, rules.dl | 120 (49 + 25 + 22 + 24) | 95 (54 + 25 + 13 + 3) |
| §10 and install decision 8: install and export of the library as the ZLIB package (new; was out of scope) | install.toml | 0 | 16 |

**One library, linkage chosen.** `zlib` has `native_kind = "library"` and the author's
`default_linkage = "shared"`, CMake's primary `zlib` target. The static flavour is not a
second target but a second configuration, `release-static`, whose `native.linkage` is
`static`: the user's global default, CMake's `BUILD_SHARED_LIBS`. The PI's rule is that
both flavours are two configurations. `ZLIB_DLL` (CMake's `DEFINE_SYMBOL`) is
`linkage_prop(zlib, shared, …)`, so only the shared flavour has it. `native_version` is
ignored when the library is static. rules.dl's library abstraction (the `library_*`
relations and their 12 rules) is gone. What remains is zlib.pc's version variable, now
read from `native_version`, and the include order. `buildlog why --output
lib/<cfg>.static/libz.a` shows the precedence level that chose static:
`library_linkage_by("zlib", …, "static", "user global")`, from `config_decl_setting`.

**The twins.** `zlib_static_example` and `static_minigzip` are `zlib_example` and
`minigzip` in `release-static`. There is one `test` fact, which runs in both
configurations (2 cases, as ctest's 2). Upstream gives the twins different defines:
`HAVE_HIDDEN` is on `zlib_static_example` and `minigzip` but not on `zlib_example` or
`static_minigzip`. The port keeps that with two inline conditions on the configuration's
`native.linkage` (`hidden & native.linkage == static`). That reads the configuration's
setting, not zlib's chosen linkage. The two agree here, but not if a user pinned zlib's
linkage per library. The tests map buildlog's (target, configuration) to CMake's target
names (`tests/zlib_case.py`, `CMAKE_STATIC_NAME`). The comparisons are unchanged: 34
compiles, the same defines and include order, `libz.a`'s members and symbols, and the
dylib's names, versions and load commands.

**Paths.** The static flavour's files are under `<cfg>.static/`: `lib/<cfg>.static/libz.a`,
`bin/<cfg>.static/zlib_example`, and `gen/<cfg>.static/zlib/zconf.h`. There is now one
zconf.h per configuration, two in all. That is the configured-file caveat, and it is
harmless because the two are different builds. The graph changes intentionally: the
release configuration no longer has libz.a or the static programs.

**Install and export (§10).** install.toml exports one component, `ZLIB:ZLIB` = `zlib`,
and installs zlib.h and the configuration's zconf.h. The library's artifact installs
because it is an exported component, in the configuration's linkage. release installs
`lib/libz.1.3.2.dylib` with the links `libz.1.dylib` and `libz.dylib`, installed as links;
release-static installs `lib/libz.a`. The compatibility claim is not stated. It is derived
from `native_version` (1.3.2, soversion 1: `same_major`), so the CPS `compat_version` is
1.0.0, the same number as the dylib's Mach-O compatibility version, and the CMake version
file accepts 1.x at or above 1.3.2. Upstream's `write_basic_package_version_file` says
`AnyNewerVersion`, which would accept a libz.2. Stating that is an error unless
`[author_decides] export_compat` gives a reason. `tests/test_zlib_phase6.py` compares the
result with CMake's install:
- the dylib's names, links, install name and versions (1.0.0/1.3.2) are CMake's;
- zconf.h is byte-identical to CMake's;
- upstream's own consumer (`test/find_package_no_components_test.cmake.in`, which finds
  `ZLIB 1.3.2 CONFIG` and links `ZLIB::ZLIB`) configures, builds and passes its test
  against either install, unchanged: it loads `@rpath/libz.1.dylib` from the shared one
  and links libz.a from the static one;
- asking for `ZLIB 2.0` is refused.

Differences from CMake's install:
- There is one component, `ZLIB::ZLIB`, in the installed flavour. Upstream has
  `ZLIB::ZLIB` and `ZLIB::ZLIBSTATIC` with `COMPONENTS shared|static`, so
  `find_package_test` and `find_package_wrong_components_test` (components) are not run.
- zlib.pc is not installed. The CMake file set differs (`ZLIBTargets.cmake` instead of
  `ZLIB-shared*.cmake`, plus a CPS file), and there are no man pages or docs.
- zconf.h's install is stated per configuration (`install_map` names
  `out:gen/<cfg>/zlib`), because a configured file's install place has no relation yet.
  That relation belongs with generated files (§7).
- The installed dylib keeps the build's `-headerpad_max_install_names` and has no rpath
  rewriting. It needs none, since it has no dependencies of its own.

## Linux (GCC 13 and Clang 18, step 4)

`linux/run.sh baselines` runs `baseline/run-cmake.sh` in the Linux image and writes
`baseline/linux/` (Ubuntu 24.04, CMake 4.4.3 (the image's, as on macOS), GCC 13.3, with `readelf`/`nm -D` in place
of `otool`/`nm -gU`). The phase tests compare against it on Linux (`tests/zlib_case.py`
picks the baseline by host). No spec line changes; the differences are platform ones:

- **Names:** `libz.so.1.3.2`, soname `libz.so.1`, and the links `libz.so.1` and
  `libz.so`, as CMake's. The programs' NEEDED entries match. CMake's RUNPATH is the
  absolute build dir, ours `$ORIGIN/...`.
- **Checks:** off64_t is TRUE on glibc, so `_LARGEFILE64_SOURCE=1` is public, as
  upstream. GCC answers no to `HAVE___ATTR__VIS_HIDDEN` (its warning on a static
  declaration, with `-Werror`), as CMake's check does. Clang answers yes, and the tests
  compare that one answer with the family's own.
- **Not stated:** `zlib_example64` and `zlib_static_example64` (CMake builds them where
  off64_t exists) and `infcover` (GCC only). buildlog has no conditional request yet, as
  phase 3 notes.
- **Exports:** upstream links with `-Wl,--version-script,zlib.map` (CMakeLists.txt:187,
  `UNIX AND NOT APPLE`). spec.toml states `native_version_script = "src:zlib.map"`, which
  buildlog-cpp's GCC and Clang drivers spell `-Wl,--version-script=` and the link takes as an
  implicit input. Mach-O has no spelling for it, and CMake states no list there, so the library
  also states `native_export_list_optional` (otherwise a list that applies nowhere is an error).
  libz.so exports exactly upstream's symbols (`nm -D`, version nodes aside), as the phase 2
  test checks on GCC.
- **Install (phase 6):** both installs and upstream's consumer test pass. On Linux,
  CMake refuses `ZLIB 2.0` with its own wording.
