<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: nanobind_example, a Python extension module, with buildlog

[nanobind_example](https://github.com/wjakob/nanobind_example) is a minimal
Python package with one C++ extension module, written with
[nanobind](https://github.com/wjakob/nanobind). Upstream builds it with
scikit-build-core, which is a PEP 517 build backend that drives CMake. The
study isolates the boundary between a native build and Python packaging:

- which interpreter the module is built for, and what that interpreter is:
  its headers, `EXT_SUFFIX`, and ABI;
- the stable ABI (abi3);
- nanobind's own library, compiled from source into each extension;
- linking against the interpreter without linking libpython;
- the wheel: its tag, its file tree, and its metadata;
- the PEP 517 backend boundary.

buildlog builds the same module, and libnanobind, for interpreters that the
user names. The upstream tests pass for 3.13 (abi3) and 3.11 (full ABI).
buildlog's command lines match the baseline's up to the differences in the
ledger. The Python API symbols the module leaves undefined are identical to
the baseline's. The wheels have the same file list as scikit-build-core's,
and a byte-identical METADATA. Only the platform tag differs, and why is
ledger item L1.

## Layout

```
baseline/run-baseline.sh      scikit-build-core's build, per interpreter, normalized
baseline/{313,311}/           its commands, CMake cache/init, wheel name/files/WHEEL/METADATA/RECORD,
                              extension.txt (exports, undefined _Py symbols, minos, size), pytest
plugins/buildlog-python/      tier-1 plugin (uv workspace member): probes, L4_python.dl,
                              wheel.py (wheel assembler), backend.py (PEP 517 shim)
share/buildlog/nanobind/      rule package: nanobind-config.cmake's logic (nanobind 3.1.x)
project/                      buildlog.toml + spec.toml (the author's), presets/ (the user's)
pyproject/pyproject.toml      the same project with [tool.buildlog] in pyproject.toml, buildlog as backend
pep517/run-pep517.sh          a wheel through `python -m build` with the buildlog backend
pep517/{313,311}/             its wheel name, files, WHEEL and pytest result
why-py-limited-api.txt        `buildlog why` for -DPy_LIMITED_API on the module's object
```

## Pinned inputs

- **nanobind_example**: commit `03bd872fdb8f34f7ddb68ced81e4dc2ce08283c7`
  (2025-10-27; the repository has no tags). Checkout:
  the submodule `case-studies/nanobind/upstream` (https://github.com/wjakob/nanobind_example.git), or `BUILDLOG_NANOBIND_EXAMPLE_SRC`
  (which also overrides the project's `NANOBIND_EXAMPLE_SRC` var).
- **nanobind**: 3.1.0 (latest release; upstream requires `>=1.3.2`).
- **scikit-build-core**: 1.1.0, with CMake 4.4.3, Ninja 1.13.2 and build
  1.6.1 (all from pip).
- **Interpreters**:
  - `$NANOBIND_VENVS/venv313` (default `build/case-nanobind`, `BUILDLOG_NANOBIND_VENVS` overrides): Homebrew CPython 3.13.15;
  - `$NANOBIND_VENVS/venv311` (default `build/case-nanobind`, `BUILDLOG_NANOBIND_VENVS` overrides): uv CPython 3.11.15.

  Each venv holds the packages above plus pytest 9.1.1.
- **Toolchain**: Apple clang 21.0.0 on macOS 26.5 arm64.

## Running

```sh
# baseline (scikit-build-core), per interpreter
case-studies/nanobind/baseline/run-baseline.sh 313 311
# buildlog
uv run --frozen buildlog configure --package-path case-studies/nanobind \
  --user case-studies/nanobind/project/presets/apple-clang-arm64.toml \
  --user case-studies/nanobind/project/presets/interpreters.toml \
  -C build/case-nanobind/scratch/project case-studies/nanobind/project
ninja -C build/case-nanobind/scratch/project/souffle -j4
PYTHONPATH=build/case-nanobind/scratch/project/souffle/python/arm64-macos.release.none.cp313 \
  build/case-nanobind/venv313/bin/python -m pytest case-studies/nanobind/upstream/tests
# a wheel from that build directory
uv run --frozen python -m buildlog_python.wheel -C build/case-nanobind/scratch/project \
  --config release-cp313 --pyproject case-studies/nanobind/upstream/pyproject.toml --out DIR
# a wheel through a PEP 517 frontend
case-studies/nanobind/pep517/run-pep517.sh 313 311
# tests
uv run --frozen pytest -q -n 4 tests/test_nanobind_case.py
```

## How it is stated

**Roles.**

| Role | States | Here |
|---|---|---|
| Author (nanobind_example) | the module, its source, its package directory, the pure-Python files, the build requirement, the stable-ABI policy | `project/spec.toml`: `nanobind_module`, `python_extension`, `python_package_file`, `python_requires nanobind >=1.3.2`, `python_py_api cp312` (scikit-build-core's `wheel.py-api`) |
| Upstream of a dependency (nanobind) | how to build libnanobind and a module | `share/buildlog/nanobind/L5_nanobind.dl`, a port of `nanobind-config.cmake`. nanobind would ship it, as it ships the CMake file |
| Platform plugin (buildlog-python) | what an interpreter is, what an extension is called, the ABI decision, the wheel tag | `plugins/buildlog-python` |
| User / packager (a developer, cibuildwheel) | which interpreters, toolchain, platform | `presets/interpreters.toml` (`python_interpreter`, one configuration per interpreter with the setting `python.interpreter`), `presets/apple-clang-arm64.toml` (toolchain) |
| PEP 517 frontend (pip, build) | the interpreter, by running the backend in it | `buildlog_python.backend` turns `sys.executable` into a generated user file |
| Downstream (who `pip install`s) | nothing to buildlog | consumes the wheel. The tag says where it applies |

cibuildwheel is a packager. Its matrix (`CIBW_BUILD=cp311-* cp312-*`,
`MACOSX_DEPLOYMENT_TARGET`, `archs`) is exactly the set of user decisions
here: interpreters, deployment target and platform. Upstream's
`[tool.cibuildwheel]` puts those packager decisions (and a test command)
in the author's file. buildlog keeps them in user files. Its pytest step
is a test the author states, run by the packager.

**Discovery.** buildlog-python has two probes:

- `python_interp` runs each interpreter the user names, with `-I`. It
  reports the implementation and version; the SOABI and `EXT_SUFFIX`;
  the abi3 suffix its importer accepts (or `none`); the include dir,
  machine and prefixes; whether the GIL is disabled; and sysconfig's CXX.
- `python_package` asks each interpreter where a build requirement is
  (`nanobind`: its version, its package dir, and whether the version
  satisfies the specifier).

The results are cached. The fingerprints are the interpreter's realpath
and stat, its `pyvenv.cfg`, and its site-packages dirs. So they are
facts: `why` explains every flag they feed. `why-py-limited-api.txt`
traces `-DPy_LIMITED_API=0x030C0000` on the module's object back to
`spec.toml:16` (`python_py_api("cp312")`) and to the
`res_python_interp` rows for implementation, `gil_disabled`,
`version_minor` and `abi3_suffix`. Which interpreter a configuration
uses is the user's `python_interpreter`, with a `~/`-relative or
absolute path.

**The interpreter is a configuration setting.** buildlog-python adds the
setting `python.interpreter` (position 2, default `none`; exec
configurations get `none`). Configurations are named after it, for example
`arm64-macos.release.none.cp313`. Declaring a configuration for an
interpreter requests the project's extension modules (the plugin derives
`request`), so the user file needs no `request_all`. One build directory
builds every interpreter; the baseline needs one build per interpreter.

**The stable ABI is one decision.** The limited API applies when all of
these hold: the project states `python_py_api cp3NN`; the interpreter is
CPython `>= 3.NN`; it is not free-threaded; and its importer lists an abi3
suffix. That single fact (`python_limited_api`) decides:

- the `Py_LIMITED_API` define, on `Python::Headers`, so libnanobind and the
  module agree;
- the extension's suffix;
- the wheel's python and abi tags;
- the attribute `python.abi = abi3 | full`.

They cannot disagree. That class of bug is scikit-build-core discussion
#1470: the limited-API version in
the wheel metadata versus the one in CMake. nanobind 3.1 mitigates it on
its side: `nanobind-config.cmake` reads the backend's
`SKBUILD_SABI_VERSION` (scikit-build-core's `wheel.py-api`). It refuses
values below 3.12 in linked mode, and it uses the value as the target of
`STABLE_ABI_VERSION` in split mode. That is two tools agreeing through a
cache variable. Here it is one fact. nanobind's own `STABLE_ABI`
option is accepted. The rule package makes it an error without a
project-wide `python_py_api`, instead of a second, independent switch.

**libnanobind from source.** nanobind ships its C++ sources in its Python
distribution. The rule package finds them through `python_package_dir`
(per configuration: each interpreter's environment has its own nanobind).
It builds `nanobind::nanobind-static` per configuration, with the flags
of nanobind-config.cmake:

- private: `NB_BUILD`, `NB_COMPACT_ASSERTIONS`, `-fno-strict-aliasing`,
  `-fno-stack-protector`, `-fvisibility=hidden`, and robin_map as a system
  include;
- public: nanobind's include dir, and a dependency on `Python::Headers`,
  whose `Py_LIMITED_API` reaches both.

The module adds `-Os`, `-fno-stack-protector` and `-fvisibility=hidden`.
It links with `-Wl,-dead_strip -Wl,-x -Wl,-S` and with nanobind's
`darwin-ld-cpython.sym` (1010 `-U _Py…` lines, one per Python API symbol
that may stay undefined). The `.sym` file is an implicit input of the link.
The port refuses a nanobind outside 3.1.x, and options other than
`STABLE_ABI` and `NB_STATIC`.

**Linking without libpython.** Neither build links libpython (`otool -L`
shows only libc++ and libSystem). nanobind lists the symbols that may stay
undefined. buildlog-python's `Python::Module` target offers CMake's
`-undefined dynamic_lookup` for extensions that do not use nanobind.
Nothing uses it here.

## Results

| | scikit-build-core (baseline) | buildlog |
|---|---|---|
| Interpreters per build | one (the backend's) | any set, one configuration each |
| cp313 module | `nanobind_example_ext.abi3.so`, 110496 B | `nanobind_example_ext.abi3.so`, 110472 B |
| cp311 module | `.cpython-311-darwin.so`, 109928 B | same name, 109896 B |
| Exports | `_PyInit_nanobind_example_ext` | same |
| Undefined `_Py*` symbols | 100 (cp313) / 91 (cp311) | identical sets |
| minos | 26.0 (host) | 13.0 (the configuration's platform) |
| Wheel (cp313) | `cp312-abi3-macosx_26_0_arm64` | `cp312-abi3-macosx_13_0_arm64` |
| Wheel (cp311) | `cp311-cp311-macosx_26_0_arm64` | `cp311-cp311-macosx_13_0_arm64` |
| Wheel files | 6 | the same 6. METADATA byte-identical |
| Upstream tests | pass (both) | pass (both): on the staged tree, from the assembled wheel, and through the PEP 517 backend |
| abi3 module in other interpreters | | loads in CPython 3.12 and 3.14 |

Per object (the module's source and libnanobind's 11), the defines and the
`-f`/`-O` flags equal the baseline's. The one exception is CMake's
`-Dnanobind_example_ext_EXPORTS`.

## Ledger

**L1. Deployment target and platform tag.** CMake passes `-arch arm64` and
no deployment target, so the module's minos is the host's (26.0), and so is
the wheel's tag, `macosx_26_0`. buildlog compiles for its configuration's
platform (`--target=arm64-apple-macos13`, the inventory's). The minos is
13.0, and buildlog-python derives the tag from the platform:
`macosx_13_0`. A wheel that says 26.0 is installable on fewer machines than
it could be. Upstream's cibuildwheel settings set
`MACOSX_DEPLOYMENT_TARGET=10.14`, which is a packager decision. buildlog
has no user-facing knob for the minimum OS version yet; see design input 4.

**L2. The compiler's path.** scikit-build-core sets CXX from the
interpreter's sysconfig: `/usr/bin/clang++` for Homebrew 3.13 and
`/usr/bin/c++` for uv 3.11. buildlog uses the toolchain the user pinned
(Xcode's clang++ with `-isysroot`). It is the same compiler. With
buildlog, the toolchain does not follow the interpreter. The probe
reports sysconfig's `cxx`, but nothing consumes it.

**L3. Language standard spelling.** CMake uses `-std=gnu++17` (from
`cxx_std_17` and CMake's extensions default); buildlog uses `-std=c++17`.
This is the same as zlib's and nlohmann's ledgers.

**L4. Include classes.** In the module's compile, CMake passes nanobind's
and Python's include dirs as `-I`. buildlog passes them as `-isystem`,
because they are dependencies' dirs (buildlog-cpp's include-directory order).
In libnanobind's own compile, nanobind's include dir is `-I` in both, and
robin_map's is `-isystem` in both. Python's is `-I` in CMake and
`-isystem` in buildlog.

**L5. `-Wl,-headerpad_max_install_names` is absent.** CMake adds it to every
Apple link so that `install_name_tool` can rewrite install names later. A
bundle has no install name, and buildlog rewrites nothing. It is one of
the sources of the 24/32-byte size difference.

**L6. Link option order.** CMake passes the options in nanobind's order:
headerpad, `@sym`, `-dead_strip`, `-x`, `-S`. buildlog sorts equal-rank
link options lexically. These options are order-insensitive.

**L7. Archive.** CMake runs `ar qc` + `ranlib` and names the archive
`libnanobind-static-abi3.a` (a name per ABI flavour). buildlog runs
`ar rcs` and writes `lib/<cfg>/libnanobind-static.a`. The configuration
already carries the interpreter and so the ABI, so one target name serves
both.

**L8. nanobind's sources are copied (workaround).** CMake compiles
`$NB/src/*.cpp` in place. buildlog compiles only sources under the
project's one source root (`cxx_source`, `src:`) or generated ones under
`out:` (`cxx_generated_source`). nanobind's live in each interpreter's
site-packages, so the rule package copies the 11 sources and 4 private
headers to `out:nanobind/<cfg>/src/` (`copy_if_different`, restat) and
compiles the copies. The objects' debug info names the copies. See design
input 2.

**L9. The module file is named by copying (workaround).** CMake's
`nanobind_add_module` sets the target's PREFIX and SUFFIX, so the link
writes `nanobind_example_ext.abi3.so` directly. buildlog's module library
is always `out:lib/<cfg>/lib<name><platform suffix>`. buildlog-python
copies it into the staged tree under its Python name
(`out:python/<cfg>/nanobind_example/nanobind_example_ext.abi3.so`),
together with the package's `__init__.py`. That tree is what goes on
`sys.path` and into the wheel. See design input 1.
Since step 4 the link writes the Python name itself
(`out:lib/<cfg>/nanobind_example_ext.abi3.so`, core's `native_file_name`,
derived by buildlog-python). The copy into the tree remains, because the
tree also holds the package's sources, but it no longer renames.

**L10. `-Dnanobind_example_ext_EXPORTS`.** This is CMake's DEFINE_SYMBOL for
shared and module targets. Nothing in nanobind uses it.

**L11. Python discovery.** CMake's FindPython reruns inside CMake: it
finds `Development.SABIModule` from the interpreter that scikit-build-core
passes, and scikit-build-core passes `SKBUILD_SOABI`/`SKBUILD_SABI_VERSION`.
buildlog asks the interpreter directly (one probe, cached). CMake takes
the include dir from the base prefix; the probe uses
`sysconfig.get_path("include")`. For both venvs this is the same
directory.

**L12. Library versions (none here).** Neither build has a shared
library with a version:
- The module is a Mach-O bundle. CMake sets no VERSION or SOVERSION on it.
  It has no `LC_ID_DYLIB`, so no install_name and no
  compatibility/current version. Nothing links against it; Python
  `dlopen`s it by path.
- libnanobind is static here (`NB_STATIC`). In nanobind's shared mode
  (`nanobind_build_library(... SHARED)`, `-DNB_SHARED`),
  `nanobind-config.cmake` 3.1.0 sets no VERSION or SOVERSION either
  (only PIC, visibility, LTO and strip). The dylib's
  compatibility/current version would be CMake's default 0.0.0.
  nanobind's version is 3.1.0 (`NB_VERSION_*` in `nanobind.h`).

In nanobind, binary compatibility is keyed elsewhere:
- **Between modules in one process:** they share nanobind's state only when
  their internals key matches. The key is
  `__nb_internals_<NB_INTERNALS_KEY>_<domain>__` (`nb_internals.cpp`),
  built from the ABI version, the compiler/stdlib and `NB_DOMAIN`.
- **Between a module and its interpreter:** the wheel tag and `EXT_SUFFIX`
  / abi3 suffix express it, not a library version.

buildlog models none of these (see design input 11).

## pyproject.toml as the entry point (driver prototype)

The PI's question was whether a Python project with a native part could
state buildlog's project keys in `pyproject.toml` instead of a
`buildlog.toml`. It is cheap, and it is implemented in core commit
"pyproject.toml [tool.buildlog] as the project entry point":

- A project directory without `buildlog.toml` may have a `pyproject.toml`
  with a **`[tool.buildlog]`** table holding the same keys (`uses`,
  `spec`, `source_root`, `include`, `author_decides`), with
  `[tool.buildlog.vars]` as the vars table.
- The table is `[tool.buildlog]`, not a top-level `[buildlog]`: PEP 518
  reserves every top-level table except `tool` for the Python packaging
  specifications, and tools own `tool.<name>`.
- Having both files is an error: one entry point, no precedence rules.
- Every reader of the entry point goes through one helper
  (`project_config_file` and `read_project_config`, in `facts.py`): uses,
  vars, spec, source_root, and source projects. `pyproject.toml` is then
  a configure input, so editing it regenerates `build.ninja`.
  `buildlog.lock` stays next to it.

`pyproject/pyproject.toml` is upstream's file as an author adopting
buildlog would write it:

- the backend is `buildlog_python.backend`;
- `[tool.scikit-build]` is gone (its `wheel.py-api` is `python_py_api`);
- `[tool.buildlog]` points at the same `spec.toml`.

Configured with the same user files, it emits a `build.ninja` whose
commands are identical to `project/`'s (a test checks this).

Two things are *not* read from pyproject yet, and state facts twice:

- `[build-system].requires` also appears as `python_requires`;
- `[project]` name and version (buildlog does not need them; only the
  wheel assembler reads them).

Deriving `python_requires` from `build-system.requires` would be a
buildlog-python probe or reader. Design input 5.

## Wheels

**What a wheel needs, and where each fact comes from:**

| Wheel fact | Source here |
|---|---|
| python tag, abi tag | derived: `python_wheel_abi` (the interpreter probe and `python_py_api`; `cp3NN` + `t` for a free-threaded build) |
| platform tag | derived from the configuration's platform (`arm64-apple-macos13` → `macosx_13_0_arm64`) |
| the module file and its name | the module library + the interpreter's abi3 suffix or `EXT_SUFFIX` |
| the package's other files | `python_package_file` (the author's) |
| `Root-Is-Purelib: false` | it has an extension |
| METADATA | pyproject's `[project]` (PEP 621); buildlog does not model it |
| license files | pyproject's directory (`LICEN[CS]E*`, `COPYING*`, …) |
| RECORD | hashes of the final files, at pack time |

buildlog-python exports `python_wheel(decl, cfg, py, abi, plat)` and
`python_wheel_file(decl, archive path, built file)`.

**The assembler (prototype).** `buildlog_python.wheel` reads those two
relations from a configured and built build directory, and pyproject's
`[project]`. It writes a reproducible wheel: sorted entries, fixed
timestamps, METADATA 2.1 as scikit-build-core writes it, WHEEL, the
licenses and RECORD. The cp313 wheel has the baseline's six files and a
byte-identical METADATA. Installed into a fresh venv with pip, upstream's
tests pass (cp313 and cp311). Only static metadata is supported (no
`dynamic`), and there is no sdist.

**As a PEP 517 backend (prototype).** `buildlog_python.backend` implements
`build_wheel`:

1. It runs in the interpreter the frontend chose, and writes that choice
   as a buildlog user file: `python_interpreter = [["pep517",
   sys.executable]]`, plus one Release configuration on the host platform.
2. It runs `buildlog configure` with the packager's user files and package
   paths (config settings `buildlog.user`, `buildlog.package-path`),
   then Ninja.
3. It assembles the wheel.

`pep517/run-pep517.sh` builds both wheels with `python -m build --wheel
--no-isolation`, from a scratch copy of the checkout carrying
`pyproject/pyproject.toml`. The tests pass. The shim is stdlib-only and
runs buildlog as an **external tool** (`buildlog.command`), the way
scikit-build-core runs cmake. This is forced: buildlog needs Python ≥ 3.12,
while a backend runs in the interpreter being built for (3.11 here). With
build isolation, the frontend would install the build requirements into a
fresh environment. `python_package` would then find nanobind there, which
is right. But buildlog-python is not published, and buildlog would have to
come as a self-contained tool (with Soufflé), like the `cmake` and `ninja`
wheels. That is not verified.

**Install/export fit.** buildlog's install machinery (the nlohmann design)
maps layout directories under a prefix, and CPS/pkg-config/CMake exports
describe a library to a *compiler*. A wheel is a different layout:

- `platlib` is the root;
- `<dist>.data/{scripts,headers,data}` are the other schemes;
- `.dist-info` holds the metadata.

The design, not built:

- a "wheel" install layout (`install_dir_default` values for
  platlib/purelib/scripts/headers/data), installed into a staging prefix
  for one configuration;
- `.dist-info/METADATA` and `WHEEL` as an export format of the package
  (`export_format_file`), with the metadata from `[project]`;
- RECORD and the zip as a pack builtin, since it hashes the final files.

Three things are missing today:

- installing a file **under another name** (the module's Python name), so
  the staged copy exists (L9);
- a per-configuration install of a *set* of configurations: a wheel is one
  configuration, but a packager builds several in one build directory;
- the METADATA reader.

CPS itself does not apply to the wheel: a wheel's consumer is an importer,
not a compiler. It does apply to nanobind as a C++ dependency, where it
falls short: see design input 2.

## Design input (ranked)

1. **Per-target output file name (CMake's PREFIX/SUFFIX/OUTPUT_NAME per
   configuration).** A Python extension's file name is a discovered,
   per-configuration fact (the abi3 suffix or `EXT_SUFFIX`). Core names a
   module `lib<name><platform suffix>`, and `native_output_name` is
   neither per-configuration nor able to drop the `lib` prefix. The
   workaround is a copy into the staged tree (L9). A `native_file_name(t,
   c, name)` that the link honours would remove the copy and give the
   staged tree and the install step one name. The same need exists for
   plugins (`.so` modules without `lib`), for Node addons (`.node`), and
   for Windows `.pyd`.
   *Step 4:* implemented. `native_file_named(t)` says a target's file name
   is decided per configuration, and `native_file_name(t, c, f)` gives it.
   Both are core open relations, and buildlog-python derives them in 3
   rule lines (L4_python.dl). The first is declaration-level because
   artifacts share `needed`'s stratum.

2. **Sources that live outside the source root, per configuration.**
   libnanobind is a *source package*: its C++ is shipped to be compiled in
   the consumer's configuration (ABI, `Py_LIMITED_API`, flags). Its
   location is a probe result that differs per configuration. `cxx_source`
   is static and `src:`-rooted, and `source_project` rejects compiled
   sources. The workaround copies the sources under `out:` (L8). A way to
   compile a discovered absolute path, per configuration, would remove the
   copies and keep debug info pointing at the real files. Two options:
   - `cxx_generated_source` accepting a probe-rooted path;
   - a rooted namespace for discovered trees.

   CPS cannot describe a source package either. This is the gap that
   `nanobind-config.cmake`'s functions fill in CMake, and a rule package
   fills it here.

3. **An interpreter as a configuration setting works.** Keep the pattern:
   - a plugin adds a setting (`python.interpreter`);
   - the user relation names the choices, and probes describe them;
   - derived attributes (`python.abi`) and requests follow.

   One build directory builds for many interpreters, and each output path
   carries the interpreter. One fact deciding define, suffix and tag rules
   out the #1470 class of mismatch. It generalizes to any "build for this
   runtime" axis: a Node ABI version, a Ruby, a JDK.

4. **The minimum OS version should be a user decision.** It feeds the
   wheel's platform tag. Today it is fixed by the inventory's platform
   triple (`macos13`). cibuildwheel's `MACOSX_DEPLOYMENT_TARGET` is a
   packager decision that must reach both the compile (`--target`) and
   the tag, as one fact (L1).

5. **pyproject.toml.** `[tool.buildlog]` as the entry point is done
   (cheap, and one file for a Python project). Next steps:
   - read `[build-system].requires` into `python_requires`, and
     `[project]` for the wheel (and for `export_package`/`export_version`),
     so nothing is stated twice;
   - map the stable-ABI policy from a `[tool.buildlog]` key.

6. **buildlog as a PEP 517 backend is a shim plus a tool.** The shim is
   small and stdlib-only. The interpreter decision maps cleanly to a
   generated user file. The blocker is distribution: buildlog (Python
   ≥ 3.12, Soufflé) must install as a self-contained build tool, runnable
   from an older target interpreter. Missing, in order:
   - `build_sdist`;
   - `prepare_metadata_for_build_wheel`;
   - editable installs (PEP 660): the staged tree is almost an editable
     layout already;
   - `dynamic` metadata.

7. **Probe results and stratification.** The first encoding of "full ABI
   unless limited" negated a relation derived from probe results. That
   put a negation inside the `needed` → request → result cycle, and it
   failed to stratify. The fix was to have the probe report the negative
   case explicitly (`abi3_suffix = none`) and to derive `python_full`
   positively. Probes should report absences as values. This belongs in
   the plugin-author guide.

8. **Cascading errors from a failed probe.** An interpreter that does not
   run first produced three errors: the cause, a "not found" from the
   package probe, and "no interpreter". Gating the dependent request on
   `python_found` left one. A general "request only when the prerequisite
   probe succeeded" idiom (or a core helper) would help plugin authors.

9. **Missing generic abstractions.** Symbol visibility (`-fvisibility=hidden`),
   strip/dead-strip, and optimize-for-size are all stated as raw flags.
   nanobind needs every one; so do most shared libraries and modules. A
   `native.visibility`, `native.strip` or `native.opt = minsize` would be
   spelled per toolchain.

10. **Link-option files are not inputs.** A `-Wl,@file` in
    `native.link_option` does not make the file an input of the link. The
    rule package adds the `.sym` file as an implicit input itself. A link
    option could carry its file, as `cc.generated_header` does.

11. **Versioning (recorded, not implemented).** buildlog does not model
    SOVERSION, install_name or compatibility_version. This study has no
    versioned shared library (L12), but it shows three kinds of "version"
    a downstream `requires` could mean:
    - **The interpreter ABI.** A cp312-abi3 module works on CPython ≥ 3.12
      (`any_newer` from 3.12). A `cp311-cp311` module works on 3.11 only
      (`exact` on major.minor). Here the wheel tag is the export. If
      buildlog exported a Python extension (CPS or otherwise), its
      `compat_version`/`export_compat` would be exactly `python_wheel_abi`,
      and a downstream requirement would be matched against the
      interpreter probe, not a library version.
    - **nanobind's internals ABI.** Modules that exchange nanobind types
      must agree on `NB_INTERNALS_KEY` and the domain. That is an ABI tag
      stricter than nanobind's semver, and nanobind states it nowhere a
      build tool can read.
    - **libnanobind as a shared library.** In `NB_SHARED` mode, upstream
      sets no SOVERSION, so a CPS export of it could only claim `exact`
      on 3.1.0. A downstream `requires nanobind >= 3.1` would then be
      unsound for a prebuilt shared libnanobind. It is only sound for the
      source package, which is compiled in the consumer's configuration.

    So `export_compat` should be able to name the axis the version is on:
    library semver, a runtime ABI, or an internals key. It should not
    assume one package version.

## UX input

These are authoring and usability frictions from this study, recorded for the
UX pass. They are separate from Design input: none of them blocks a build.
Line counts are non-comment, non-blank lines.

**Size.** The spec comes to 16 lines, not counting source lists or comments.
`project/spec.toml` has 11 (12 minus the `cxx_source` line), and
`project/buildlog.toml` has 5. The user presets add another 15: 4 in
`apple-clang-arm64.toml` and 11 in `interpreters.toml`. Upstream has
16 lines on the same basis:
- 12 in `CMakeLists.txt`: 35, minus the 22-line "use scikit-build" warning
  block, minus the source line;
- 4 in pyproject's `[tool.scikit-build]`.

Upstream never names its interpreters. scikit-build-core builds for the one
it runs in, and cibuildwheel sets `CIBW_BUILD` on its command line.
Authoring the nanobind rule package and the Python plugin took 186 lines of
Datalog (63 + 123) and 94 lines of manifests (23 + 71). CMake's equivalents
are `nanobind-config.cmake` and FindPython, which a user never writes.

1. **The target name is repeated on every row (the spec, 5 lines).**
   ```toml
   target = [["nanobind_example_ext"]]
   cxx_source = [["nanobind_example_ext", "src/nanobind_example_ext.cpp"]]
   nanobind_module = [["nanobind_example_ext"]]
   nanobind_module_option = [["nanobind_example_ext", "STABLE_ABI"],
                             ["nanobind_example_ext", "NB_STATIC"]]
   python_extension = [["nanobind_example_ext", "nanobind_example", "nanobind_example_ext"]]
   ```
   The name appears 7 times. I wished for a per-target table (about 4 lines),
   where `module` and the options come from the rule package and the
   module name defaults to the target name:
   ```toml
   [target.nanobind_example_ext]
   sources = ["src/nanobind_example_ext.cpp"]
   nanobind = { options = ["STABLE_ABI", "NB_STATIC"] }
   python = { package = "nanobind_example" }
   ```
   `nanobind_module` also implies `target`, yet both rows are required.

2. **Two tables per configuration and one row per setting
   (`interpreters.toml`, 7 lines for 2 configurations).**
   ```toml
   config_decl = [["release-cp313", "arm64-macos"], ["release-cp311", "arm64-macos"]]
   config_decl_setting = [["release-cp313", "native.opt", "release"],
     ["release-cp313", "python.interpreter", "cp313"], ...]   # 4 rows
   ```
   What a cibuildwheel user means is "Release × {cp313, cp311}". I wished
   for 1–2 lines:
   ```toml
   [configs]
   platform = "arm64-macos"
   matrix = { "native.opt" = ["release"], "python.interpreter" = ["cp313", "cp311"] }
   ```
   The configuration names come out as `arm64-macos.release.none.cp313`.
   `none` is the sanitize default, which nobody set. It shows up in every
   path and in `why`.

3. **Stable-ABI intent is stated twice (spec, 2 lines).** One statement is
   `nanobind_module_option ... "STABLE_ABI"`, the other is
   `python_py_api = [["cp312"]]`. The rule package reports an error if the
   first appears without the second. They are one decision, "abi3 from 3.12",
   the same as upstream's `STABLE_ABI` + `wheel.py-api`. I wished the option
   were omitted, or implied by `python_py_api`. That would save 1 line.

4. **A scikit-build-core default restated by hand (spec, 1 line).**
   `python_package_file = [["src/nanobind_example/__init__.py",
   "nanobind_example/__init__.py"]]`. scikit-build-core finds
   `src/<name>` by itself. I wished for `python_packages = ["src/nanobind_example"]`,
   or a default derived from the project name. That is still 1 line, but
   it names a directory rather than listing each file.

5. **File sprawl for the user (3 files, 20 lines, plus 2 CLI flags).**
   The files are `buildlog.toml`, `presets/apple-clang-arm64.toml` and
   `presets/interpreters.toml`. They were split so the toolchain preset
   could be reused, but that means two `--user` flags on every configure.
   On top of that, the rule package is not installed, so every command also
   needs `--package-path case-studies/nanobind`:
   ```
   buildlog configure --package-path case-studies/nanobind \
     --user .../apple-clang-arm64.toml --user .../interpreters.toml -C BUILD case-studies/nanobind/project
   ```
   I wished for a `[user] include = [...]` in one user file, or a
   `presets = [...]` default. I also wished for `package_path` in
   `buildlog.toml`'s `[vars]`/`[paths]`, since the vendored package lives
   next to the project.

6. **pyproject duplicates buildlog.toml (5 lines).** `[tool.buildlog]`
   repeats `uses`, `source_root`, `spec` and `[vars]`, because the
   `buildlog.toml` of the same project cannot be referenced. Also, tombi
   (pre-commit) reorders the tables and detaches the header comment. I
   wished for `[tool.buildlog] project = "../project"`, or reading `uses`
   from `[build-system].requires`.

7. **The rule-package manifest lists every core relation it writes (23 lines,
   14 of them `extends`).** I had to find out by trial which relations
   `target`, `dep`, `prop_in`, `action_arg` and so on need to be declared. I
   wished for `extends = ["@targets", "@actions"]` groups, or for the
   declarations to be inferred from the layer's heads. The same is true of
   the plugin's 71-line manifest.

8. **Datalog that should be a TOML table (nanobind rule package, about 25
   lines).**
   - The list of nanobind sources and private headers takes 6 lines of
     `nanobind_source("…").` facts.
   - The fixed per-target flags are 10 lines of
     `prop("nanobind::nanobind-static", "cc.compile_flag", …)`.
   - The Release-only flags use `prop_in(…) :- needed(t,c),
     config_setting(c, "native.opt", "release")`.

   A package's own `spec.toml` would cover all of this:
   ```toml
   [target."nanobind::nanobind-static"]
   kind = "library"
   linkage = { require = "static", why = "compiled into each extension module" }
   sources = ["src/nb_internals.cpp", ...]   # relative to a discovered dir
   compile_flags = ["-fno-strict-aliasing", "-fvisibility=hidden"]
   [target."nanobind::nanobind-static".when."native.opt=release"]
   defines = ["NB_COMPACT_ASSERTIONS"]
   ```
   Only the discovered root (`python_package_dir`) and the `.sym` choice
   need rules.

9. **The copy workarounds are Datalog boilerplate (about 12 lines).** Each
   external source becomes a
   `nanobind_copy` → `action`/`action_builtin`/`action_arg` ×2/`action_in`/
   `action_restat` → `cxx_generated_source`. The module name is also a copy
   (L8, L9; Design input 1, 2). I wished for `sources_root = <discovered
   dir>` and `output_name`/`suffix` on the target.

10. **`why` is long and structural (91 lines for one `-D`).**
    `why-py-limited-api.txt` answers "why `Py_LIMITED_API=0x030C0000`?".
    The useful chain (spec `python_py_api` → probe `abi3_suffix` → define)
    is about 8 of those lines. The rest is noise:
    - configuration-name plumbing (`decl_suffix` recursion, cons lists
      such as `[["native.opt","release"],[…, nil]]`, `3 > 0`, `2 = 2`);
    - `settings_name` repeated.

    I wished for a default view that folds core bookkeeping (the
    configuration-name derivation, arithmetic) and shows only user, spec,
    probe and rule-package leaves. The full tree would move behind
    `--full`.

11. **Errors: stratification and cascades.** The negation of a
    probe-derived fact failed to stratify, and the error names only Soufflé
    relations. There was no hint that probes should report absences
    (Design input 7). A broken interpreter path produced 3 errors for 1
    cause (Design input 8). I wished the stratification error pointed to
    the probe/`needed` cycle and suggested the fix.

12. **Lockfile churn while authoring.** Every edit to a plugin or rule-package
    layer means reconfiguring both `project/` and `pyproject/` with
    `--update-lock` and committing two `buildlog.lock` files. I wished for a
    development mode that does not lock packages coming from
    `--package-path` or a workspace path.

13. **Finding outputs.** The built module is at
    `<builddir>/souffle/python/arm64-macos.release.none.cp313/nanobind_example/`
    (the `souffle/` level is the `out:` root). I wished for a
    `buildlog outputs <target>`, or a stable `<builddir>/python/<cfg>`
    symlink, so users can run `PYTHONPATH=… pytest`.

## UX pass port

The project spec now uses vocabulary tables where they reduce repetition:
`[target.nanobind_example_ext]` owns the source, nanobind options, Python
extension row and C++ standard; `[python_requires.nanobind]` states the build
requirement. The nanobind rule package moves static target facts for
`nanobind::nanobind-static` and `nanobind::robin_map` into a package
`spec.toml`; only per-configuration discovery/copy/link rules stay in Datalog.

| files | before (4a1ccb0) | after | Datalog before → after |
|---|---:|---:|---:|
| project + pyproject entry | 23 | 22 | 0 → 0 |
| nanobind rule package | 86 | 87 | 63 → 53 |
| total | 109 | 109 | 63 → 53 |

Graph identity is `IDENTICAL` for `project/` and `pyproject/` with the
interpreter preset. Remaining gaps: unary facts such as `nanobind_module` still
need subject tables, external/discovered source roots still require copy
Datalog, and manifest `extends` remains verbose. The rule package is billed
separately from the project.

## Tests

`tests/test_nanobind_case.py`, 17 tests.

Unit tests (always run):
- the interpreter probe on buildlog's own interpreter, and on a missing
  one;
- the package probe's specifier check;
- METADATA from a synthetic `[project]`, including the rejection of
  `dynamic`;
- the backend's generated user file.

Integration tests (marked `integration`; skipped without the checkout, the
two venvs, Soufflé or Ninja):
- compiles against `baseline/313` (the same 12 objects, defines and
  flags);
- link flags: nanobind's `.sym` file as an input, and no libpython;
- extension names and wheel tags;
- the undefined `_Py*` sets against the baseline, for both interpreters;
- upstream pytest on the staged tree, per interpreter;
- the abi3 module in buildlog's own interpreter;
- the assembled wheels against the baseline's files and METADATA, and
  upstream pytest on their contents;
- the pyproject entry point producing identical commands;
- `why` for `Py_LIMITED_API`;
- a missing interpreter producing exactly one error.

Core's `tests/test_facts.py` has two tests for the `[tool.buildlog]` entry
point.

## Linux (GCC 13 and Clang 18, tier 2)

The image (`linux/Dockerfile`) has the two uv-managed CPythons the macOS study
builds for, each in a venv with the baseline's pinned requirements
(scikit-build-core 1.1.0, nanobind 3.1.0, build 1.6.1, pytest 9.1.1;
`/opt/nanobind-venvs`, `BUILDLOG_NANOBIND_VENVS`): CPython 3.13.15 (abi3) and
**3.11.16** (uv offers no 3.11.15 for Linux, so the full-ABI interpreter is one
patch release newer than macOS's). `baseline/run-baseline.sh` runs there too
(`linux/run.sh baselines`; `CC`/`CXX` are gcc/g++ or clang/clang++, which
scikit-build-core otherwise takes from sysconfig) and writes `baseline/linux/` and
`baseline/linux-clang/`, with `nm -D` and `readelf -d` for the extension's report.
`tests/test_nanobind_case.py` picks the baseline by host and passes (17 tests) with both
families; `linux/cases.sh nanobind` configures, builds and runs the example's tests for
both interpreters.

Names and flags come from the interpreters and from nanobind's own rules, not from
spec lines:

- **Names (probes):** `nanobind_example_ext.abi3.so` for cp313 and
  `nanobind_example_ext.cpython-311-aarch64-linux-gnu.so` for cp311, from the interpreter's
  EXT_SUFFIX and abi3 suffix (the `buildlog-python` probe). The probe reports the machine
  as buildlog names it (`aarch64` is `arm64`).
- **Link:** no `-undefined dynamic_lookup` and no symbol list on ELF (undefined
  symbols are allowed in a shared object); `-Wl,-s` replaces Mach-O's
  `-dead_strip -x -S` (nanobind_strip), and libnanobind gets
  `-ffunction-sections -fdata-sections` with `-Wl,--gc-sections` (public, as
  nanobind_build_library does off macOS and Windows). All are conditions on the
  platform's `os`.
- **Wheel tag:** `cp312-abi3-linux_aarch64` and `cp311-cp311-linux_aarch64`, equal to
  scikit-build-core's (it does not claim manylinux either).
- **Agreement with scikit-build-core:** the 12 compiles (module plus libnanobind's 11) have
  equal defines (but `-Dnanobind_example_ext_EXPORTS`) and `-f`/`-O` flags; the abi3
  module's exports (the `PyInit` function and nanobind's weak exception symbols) and the
  undefined `Py*` symbols of both modules are identical sets; the wheels have the same 6
  files and byte-identical METADATA; the upstream tests pass on the staged tree and from
  the assembled wheel. Module sizes differ by under 1 KB (GCC: 135376 and 135544 B
  against 135392 and 135560; Clang: 135288 and 135344 against 135136 and 135192).
- **Residuals:** the Python is 3.11.16 for 3.11.15; PyPy and free-threaded builds are
  untested, as on macOS.

## Not verified

- **Free-threaded CPython (3.13t/3.14t).** The wheel's abi tag (`cp313t`)
  and the exclusion from abi3 are derived, but untested. The nanobind
  port does not add `nb_ft.cpp`/`NB_FREE_THREADED` and would build a
  wrong library.
- **PyPy.** The `darwin-ld-pypy.sym` branch and the non-CPython tags are
  untested.
- **Windows.** (Linux is in its own section below; its wheels carry the plain
  `linux_aarch64` tag, since a manylinux/musllinux policy is a packager
  decision plus auditwheel.)
  - Windows extensions link `python3.lib` (abi3) or `python3NN.lib`, and
    are named `.pyd`.
- **Debug configurations.** Without `native.opt = release`, the port drops
  NB_OPT's flags, like CMake. Only Release is compared.
- **Build isolation**, sdists, editable installs, `dynamic` metadata, and
  an installed (non-PYTHONPATH) buildlog-python in the backend.
- **nanobind versions other than 3.1.0.** The port refuses them.

## One library kind (step 4)

The rule package's `nanobind::nanobind-static` was `static_library`. It is `library` with
`linkage = { require = "static", why = "compiled into each extension module
(nanobind_build_library(nanobind-static)); a shared build is the separate nanobind target" }`.
The graph is identical.

| Change | File | Before | After |
|---|---|---:|---:|
| `nanobind-static`: `library` plus a static requirement with its reason | rule package spec.toml | 10 | 11 |
