<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Qt moc as a library's rule package

**Claim.** A library can ship rules that apply to its consumers. Here Qt ships "a target that
compiles against QtCore has its headers run through moc (and its `.qrc` files through rcc), and
the generated sources compiled into it". CMake builds this into its core (AUTOMOC/AUTORCC, the
`cmQtAutoGen*` sources: 8,510 lines of C++). In buildlog it is a
rule package Qt installs next to its libraries (`share/buildlog/Qt6Core`, 37 lines of Datalog), with
Qt's own `Qt6*Targets.cmake` imported rather than transcribed. The
consumer's spec says nothing about moc. qbs is the closest precedent: a product's
`Depends { name: "Qt.core" }` activates the module's `Rule` and `Scanner`. Here activation is
the dependency graph itself.

## Layout

- `share/buildlog/`: what Qt would install under its prefix, one manifest per Qt6 CMake package
  (hand-written; they stand in for what Qt would ship), each importing that package's own export
  file (`imports = [{ format = "cmake", file = "../../../lib/cmake/Qt6Core/Qt6CoreTargets.cmake", namespace = "Qt6" }]`).
  - `Qt6`, `Qt6CoreTools` (moc, rcc), `Qt6Gui`, `Qt6Widgets`: manifest only, 5 lines each.
  - `Qt6Core`: the manifest (55 lines with the extends list) and `Qt6Core.dl`: the activation and the
    moc/rcc actions, and QtCore's `QT_NO_DEBUG` (`Qt6CoreConfigExtras.cmake`'s generator expression).
  - `Threads`, `WrapAtomic`, `WrapOpenGL`: the targets Qt's exports name that a find module makes
    (`Threads::Threads`, ...), 9-10 lines each.
- `project/`: the consumer. A `QObject` with a property, a signal and slots (`src/counter.h`), a
  header without `Q_OBJECT` (`src/version.h`), and a headless test (`tests/counter_test.cpp`:
  `QT_QPA_PLATFORM=offscreen`, a widget, a string-based connect, a property, a resource from
  `tests/greeting.qrc`).
- `cmake/CMakeLists.txt`: the same consumer in CMake.

## Setup and run

Qt 6.11.3 from Qt's own binaries (aqt's `clang_64`: universal2 frameworks and tools), no
Homebrew, no system state:

    uvx --from aqtinstall aqt install-qt mac desktop 6.11.3 clang_64 -O DIR
    export BUILDLOG_QT_PREFIX=DIR/6.11.3/macos

aqt's tree has no `share/buildlog` (Qt does not ship it yet), so install the packages as Qt
would: copy `share/buildlog/*` into the prefix, or into an overlay prefix whose `lib`, `libexec`,
`plugins`, `include`, `mkspecs` and `bin` link to Qt's (what `tests/test_qt_moc_case.py` does; copy,
not link: a package is located at its real path). Then:

    uv run --frozen buildlog configure --package-path PREFIX -D QT_PREFIX=PREFIX \
      --user case-studies/qt-moc/project/presets/apple-clang.toml -C BUILD case-studies/qt-moc/project

The project `uses` Qt6Core (its spec says `qt_moc_exclude`, a relation Qt6Core accepts, so Qt6Core is
loaded as a rule package); the preset's `package_file` rows bind each Qt manifest, which is what
follows its `imports` (a name like `Qt6::Core` finds the directory `Qt6`, so the names are bound
explicitly).
    ninja -C BUILD -j2 && uv run --frozen buildlog test -C BUILD

Four configurations: debug, release (arm64), x86_64, and universal2, which builds both slices
and merges `counter_test` with lipo (one `request_all` row; Qt's frameworks are universal2 too).
The test passes in debug and release; x86_64 is a cross configuration, "not run" (its binary,
and the universal2 binary's x86_64 slice, run under Rosetta). moc and rcc run in the exec configuration `arm64-macos.release.none`, inferred
like flatc's: `Qt6::moc` is an imported executable that an action runs, so no annotation says
"host tool". `tests/test_qt_moc_case.py` checks all of this and skips, naming the `aqt` command,
when `BUILDLOG_QT_PREFIX` (the Qt prefix, no default) is unset or has no Qt.

## Which headers: every header by default, exclusions as facts, no probe

**A role, not a kind.** A target that compiles against QtCore takes on the Qt role: Qt's
rules run moc over every header it lists (`.h`, `.hpp`, `.hxx`), at build time, and compile
each `moc_*.cpp` into it. The role is derived from the dependency graph (Activation, below),
so nothing marks a target "Qt": a `qt_library` kind would be a second statement of what the
`Qt6::Core` edge already says, and it is orthogonal to the native kind (a Qt static library,
shared library and executable all need moc). The author may exclude headers as facts, in the
target's table:

    qt_moc_exclude = ["src/version.h"]

An exclusion that names no source of its target is an error (a typo would otherwise silently
exclude nothing). Exclusions are never needed for correctness, only to save time:

**What moc does with a header that doesn't use the meta-object system** (checked with Qt
6.11.3's moc): it prints `note: No relevant classes found. No output generated.`, exits 0, and
writes an empty (0-byte) output file, created if absent; with `--no-notes`, as the package runs
it, it prints nothing. The depfile is still written. The empty file compiles to a 336-byte
object with no symbols, so the archive and the link are unchanged in content. Running moc over
such a header is harmless.

**The demo excludes nothing.** `version.h` has no `Q_OBJECT` and goes through moc. Its cost per
configuration is two Ninja edges: a moc run (about 9 ms) and an empty compile (about 26 ms), so
0.1 s of CPU for a clean build of the three single-platform configurations (universal2's slices
are those configurations). An incremental build reruns them only when `version.h` changes. The
tests exercise an exclusion on a separate target (`excl`), and the error for a bad one.

**Why not a probe.** A configure-time content probe (grep the headers for `Q_OBJECT`) would
violate the probe principle. Probes are for facts about the environment: the toolchain, the
platform, installed packages. They are cached by request and rerun when configure does. The
content of the project's own sources is the build's job: the files change between builds
without a reconfigure, so a probed answer goes stale after an edit (a header that gains
`Q_OBJECT` would fail at link time with a missing vtable until configure reruns). If scanning
ever paid off, it would belong in the build as a scanner action with a dyndep (what CMake's
`<target>_autogen` step does at build time), never in configure. Here moc itself is the
scanner, so neither is needed.

**Why not a declared list** (`moc_headers = [...]`). It states again what the header already
says, and is a choice that can be gotten wrong ("choice is the enemy"); the failure is the same
link-time vtable error. Exclusions invert that: forgetting one costs 35 ms, not a broken build.

**Activation** is the usage closure. A target is a Qt user in configuration c if `Qt6::Core` is
a direct dependency, or a dependency that passes `Qt6::Core` on through public or interface
edges (`Qt6::Widgets` does, and so does `counter` here). A target that doesn't reach QtCore
(the test's `plain` library) never runs moc. `uses = ["Qt6Core"]` loads the package. A `.pc`
`buildlog_rules=` variable or a CPS `x_buildlog_rules` attribute on Qt's own metadata would load
it implicitly (see Gaps).

## The rules

Condensed from `share/buildlog/Qt6Core/Qt6Core.dl` (the layout rules and the rcc action omitted):

```
qt_passes(d, c) :- eff_dep(d, c, "Qt6::Core", v), v != "private".
qt_passes(d, c) :- eff_dep(d, c, x, v), v != "private", qt_passes(x, c).
qt_user(t, c)   :- eff_dep(t, c, "Qt6::Core", _).
qt_user(t, c)   :- eff_dep(t, c, x, _), qt_passes(x, c).
qt_input(t, f, "moc", "moc_") :- target_sources(t, f), path_ext(f, e), (e = ".h" ; e = ".hpp"),
    !qt_moc_exclude(t, f).
qt_input(t, f, "rcc", "qrc_") :- target_sources(t, f), path_ext(f, ".qrc").
qt_gen(t, c, f, k, cat(g, "/", d, x, s, ".cpp")) :- qt_user(t, c), qt_input(t, f, k, x),
    gen_dir(t, c, g), qt_sub(f, d), path_stem(f, s).
cxx_generated_source(t, c, o), target_action(t, c, o), action_depfile(o, cat(o, ".d")),
action_arg(o, 1, "", cat("src:", f)), action_arg(o, 2, "", "-o"), action_arg(o, 3, "", o)
    :- qt_gen(t, c, f, _, o).
action(o, "MOC"), action_run(o, 0, "", "Qt6::moc", "run"), action_arg(o, 4, "", "--output-dep-file")
    :- qt_gen(_, _, _, "moc", o).
action_arg(o, 20, "-D", v) :- qt_gen(t, c, _, "moc", o), use(t, c, "cc.define", v, _).
action_arg(o, 30, "-I", v) :- qt_gen(t, c, _, "moc", o), use(t, c, "cc.include_dir", v, _).
```

`cxx_generated_source` is buildlog-cpp's open relation: the object is compiled with the target's
own flags and linked into it. `use` is the target's effective usage requirements, so moc sees
the defines and include dirs the compiler sees, including Qt's `QT_NO_DEBUG` in release.

## Size

Per the accounting policy (`historical source-line accounting`, non-blank non-comment lines, source lists
not billed):

| | lines | of which Datalog |
|---|---:|---:|
| consumer `project/buildlog.toml` (4 configurations, universal2) | 25 | 0 |
| consumer with one configuration, like the CMake one | 14 | 0 |
| presets (user file, not billed) | 6 | 0 |
| packages `share/buildlog/Qt6*`, `Threads`, `WrapAtomic`, `WrapOpenGL` (Qt6Core 55, the other seven 5-10 each; was 84 with a transcribed spec) | 103 | 37 |
| CMake consumer `cmake/CMakeLists.txt` (one configuration) | 13 | — |

The consumers are the same size; the Qt-specific part of each is one dependency edge in
buildlog against `set(CMAKE_AUTOMOC ON)`, `set(CMAKE_AUTORCC ON)` and `find_package` in CMake.
The difference is where the knowledge lives: 103 lines Qt ships, against 8,510 lines of C++ in
CMake's core plus `Qt6CoreMacros.cmake`/`Qt6*Targets.cmake` that Qt ships anyway.

## why

"Why is `moc_counter.cpp` compiled into counter?"
(`buildlog why --output moc_counter.cpp.o --flag moc_counter.cpp --config debug`), 15 lines:

```
action_arg("out:obj/arm64-macos.debug.none/counter/gen/arm64-macos.debug.none/counter/src/moc_counter.cpp.o", 41, "-c ", "out:gen/arm64-macos.debug.none/counter/src/moc_counter.cpp")  [buildlog-cpp:layers/L4_cxx.dl:34]
  cxx_object_input("out:obj/arm64-macos.debug.none/counter/gen/arm64-macos.debug.none/counter/src/moc_counter.cpp.o", "out:gen/arm64-macos.debug.none/counter/src/moc_counter.cpp")  [buildlog-cpp:layers/L4_cxx.dl:116]
    #n3 qt_gen("counter", ‹arm64-macos.debug.none›, "src/counter.h", "moc", "out:gen/arm64-macos.debug.none/counter/src/moc_counter.cpp")  [Qt6Core:Qt6Core.dl:55]  (via cxx_generated_source)
      eff_dep("counter", ‹arm64-macos.debug.none›, "Qt6::Core", "public")  [buildlog:L1_targets.dl:410]  (via qt_user)
        eff_ref("counter", ‹arm64-macos.debug.none›, "Qt6::Core", "public")  [buildlog:L1_targets.dl:408]
          #n7 needed("counter", ‹arm64-macos.debug.none›)  [exists here because it is built in this configuration; --demand explains why]
          #n57 target_deps("counter", "public", "Qt6::Core")  ← case-studies/qt-moc/project/buildlog.toml:28  (via dep)
        dep_resolves("counter", "Qt6::Core", "Qt6::Core")  [buildlog:L1_targets.dl:507]  ↑ #n57 (via dep_ref ← dep)
          !has_own("counter", "Qt6::Core")
          imported("Qt6::Core")  ← QT_PREFIX/share/buildlog/qt/spec.toml:11 (package qt)  (via name_candidate ← target_id)
      qt_input("counter", "src/counter.h", "moc", "moc_")  [Qt6Core:Qt6Core.dl:43]
        target_sources("counter", "src/counter.h")  ← case-studies/qt-moc/project/buildlog.toml:26
        !qt_moc_exclude("counter", "src/counter.h")
      gen_dir("counter", ‹arm64-macos.debug.none›, "out:gen/arm64-macos.debug.none/counter")  [buildlog:V_vocabulary.dl:92]  ↑ #n7
    cxx_object("counter", ‹arm64-macos.debug.none›, "gen/arm64-macos.debug.none/counter/src/moc_counter.cpp", "out:obj/arm64-macos.debug.none/counter/gen/arm64-macos.debug.none/counter/src/moc_counter.cpp.o")  [buildlog-cpp:layers/L4_cxx.dl:114]  ↑ #n3 (via cxx_generated_source), #n7
```

It reads: the compile exists because `qt_gen` derived the generated source (`L5_qt.dl:55`); that
holds because counter depends publicly on `Qt6::Core` (`buildlog.toml:28`), which Qt's package
defines (`Qt6CoreTargets.cmake:59`), and counter lists `src/counter.h` (`buildlog.toml:26`) and doesn't exclude it. "Why does moc
run on counter.h" (`--output moc_counter.cpp --flag counter.h`) is 12 lines. The bare existence
question (`--output moc_counter.cpp`, about 28 lines) also explains why counter is built at all
(counter_test is a test and needs it): existence answers keep target demand, as
docs/proof-protocol.md specifies.

## Against CMake

`cmake/CMakeLists.txt` builds and its test passes with Qt 6.11.3 (`-DCMAKE_PREFIX_PATH` its
prefix), with one bug: with a build directory under `/tmp` (a symlink to `/private/tmp`), the
`moc_counter.cpp` AUTOMOC writes includes `counter.h` by a wrong relative path and the compile
fails. buildlog's moc in `/tmp` is fine. (Qt 6.8.3's CMake package also linked `-framework AGL`,
which the macOS 26 SDK no longer has; 6.11.3 doesn't.)

What CMake does internally: `AUTOMOC` is a target property that the core reads. Qt's package
supplies `QT_MAJOR_VERSION` on its targets, and the core finds `moc` by name, branching on the
Qt version. Per target it adds a `<target>_autogen` utility target and a timestamp, a JSON info
file, and a scan step that reads each source and header at build time for `Q_OBJECT` and
includes. The `moc_*.cpp` files go into hashed directories and are folded into one
`mocs_compilation.cpp` per target. `qrc_greeting.cpp` goes under another hash directory. For one
configuration this consumer's graph is 31 build statements in CMake (including its phony and
utility edges), against 10 in buildlog: per header a MOC and a compile, rcc and its compile, the
consumer's two compiles, the archive and the link.
CMake also passes `-isystem mkspecs/macx-clang` and `-isystem include` (Qt6::Platform), which
buildlog's facts omit (see Gaps).

## Gaps

- A `.cpp` with `Q_OBJECT` that includes `foo.moc`, and headers that are not listed among the
  target's sources (CMake's same-name-header rule). Both need the include scan CMake does at
  build time; the package could add it as a build-time scanner action with a dyndep.
- uic (`.ui` files), `moc_predefs.h`, and the Qt6::Platform include dirs.
- Headers in source groups: the rules read `target_sources`, not buildlog-cpp's private
  `source_lang`.
- Frameworks only: Linux and Windows layouts are more rules in the same package.
- Qt's own exports are imported, not restated. What was missing is now done: the importer ignores
  `INTERFACE_SOURCES` that only name `.json` metatypes, accepts `namespace = "Qt6"` for targets that
  already are `Qt6::*`, and the `Threads`/`WrapAtomic`/`WrapOpenGL` find-module targets are small
  stub packages. Qt's `RelWithDebInfo`-only configuration is still bound by the user's
  `package_config_map` (global, not per package).
- Imports are not followed from a static `uses`/`--package-path`; the preset names each manifest with
  `package_file`, because `Qt6::Core` looks for a directory `Qt6`.
- A static library with a `.qrc` needs `Q_INIT_RESOURCE` in its user (as in CMake).
- Loading is explicit (`uses = ["Qt6Core"]`). Qt's `.pc`/CPS metadata naming the package would make
  it implicit through the existing outer loop.
- The lockfile records the package's path and is machine-specific (ignored here).

## Core changes

- `${PACKAGE_PREFIX}` in a package's facts files, for a package under
  `<prefix>/share/buildlog/<name>`. It was needed here, since Qt's facts are paths below the
  prefix it is installed under, but it is general: CPS's `@prefix@`.
- Quoted table headers (`[imported."Qt6::Core"]`) now cite their own line, not line 1. A general
  provenance fix.
- Core's path helpers (`path_stem`, `path_dirname`, ...) are elided from `why` answers, like
  other derived string plumbing. General; no golden answer changed.
- buildlog-cpp's CMake-export importer accepts FRAMEWORK targets (it rejected them): the
  location is the binary in the bundle, linked by path, and an include dir naming a `.framework`
  becomes `-iframework <parent>`, as CMake passes it. Not needed by this package, which states
  Qt's facts, but found by trying to import Qt's own CMake files; general.

## One library kind (step 4)

`counter` was `static_library`. Built libraries have one kind, `library`, so it is
`linkage = { require = "static", why = ... }`, with the reason "the CMake build it mirrors is
add_library(counter STATIC); its Q_OBJECT symbols are not exported for a shared build". The
graph is identical.

| Change | File | Before | After |
|---|---|---:|---:|
| `counter`: `library` plus a static requirement with its reason | buildlog.toml | 25 | 26 |

## Linux (GCC 13 and Clang 18, tier 3)

Same project, same specs and packages; `linux/run.sh cases gcc|clang qt` and the Linux run of
`tests/test_qt_moc_case.py` (7 passed).

- **Which Qt: 6.11.3 from aqt (`linux_gcc_arm64`, archives `qtbase icu`), not Ubuntu's Qt 6.4.2.** The
  manifests pin `==6.11.3` and import `../../../lib/cmake/Qt6*/…` relative to the prefix, which is aqt's
  layout (`6.11.3/gcc_arm64/{lib,libexec,include,…}`); the distribution splits Qt over
  `/usr/lib/<triple>/cmake`, `/usr/libexec` and `/usr/include/<triple>/qt6`, at 6.4.2 (different
  `moc` options and `Qt6CoreConfigExtras.cmake`). Using the same version keeps the study's facts
  identical and the comparison with macOS about the platform. The image installs it in
  `/opt/qt/6.11.3/gcc_arm64` (`BUILDLOG_QT_PREFIX`), plus apt's runtime libraries Qt's Widgets
  needs (libEGL, libGL, X11, fontconfig, …).
- moc and rcc run as host tools in `arm64-linux.release.none`, inferred from the import (nothing says "host
  tool"). Two configurations (debug and release); the x86_64 and universal2 ones are macOS's.
  `buildlog test` passes headless (`QT_QPA_PLATFORM=offscreen`): 2 passed, 0 not run.
- **Two findings, both fixed in rules/importer, not in the spec.** (1) The `WrapOpenGL` package wrote
  `-framework OpenGL`; it now states `-lGL` on Linux (a platform condition in the package).
  (2) The built `counter_test` could not find `libQt6Core.so.6` at run time: Qt's targets are imported
  as ELF shared libraries without an rpath, which CMake supplies as the build RPATH. The
  CMake-export importer now adds `-Wl,-rpath,<dir>` for an imported ELF shared library (system
  directories excepted). macOS is unchanged.
- Against CMake: `cmake/CMakeLists.txt` builds and `ctest` passes on both compilers. CMake's
  link line has `libGL.so` by full path and an rpath to Qt's lib; buildlog's has `-lGL` and the same
  rpath. CMake also passes `-isystem mkspecs/linux-g++` and `-isystem include` (Gaps, as on
  macOS). Counts are the macOS ones: the graph is the same shape (per header a MOC and a compile, rcc
  and its compile, the consumer's compiles, archive, link).
- Not covered: uic (a gap in the study on every platform).
