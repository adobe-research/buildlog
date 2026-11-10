<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: a TinyCC bootstrap in one graph

A toolchain built and then used in the same build graph. One `buildlog
configure` produces one Ninja graph that:

1. builds `tcc` (stage 1) from source with the user's C toolchain (Apple
   clang, the preset's pin);
2. builds `tcc` again (stage 2) with stage 1 as its C compiler, linker and
   archiver;
3. builds stage 3 with stage 2, and a test checks that stage 2 and stage 3
   are the same file, byte for byte (the bootstrap fixpoint, tinycc's
   `make test3`);
4. builds an ordinary static library (`greet`) and a program (`hello`) with
   stage 2, and a test runs it.

Each stage's runtime, `libtcc1.a`, is built by that stage's compiler and
linked into everything that compiler links.

## Layout

```
case-studies/tcc/
  README.md                this file
  upstream/                submodule: tinycc (https://github.com/TinyCC/tinycc.git)
  project/
    buildlog.toml          uses buildlog-cpp and tcc; one configuration (release, arm64-macos)
    spec.toml              the stages, their runtimes, the demo, the tests
    config/config.h        the part of ./configure's config.h that tcc.c reads
    demo/                  greet (library), hello (program), same_file (cmp)
    packages/tcc/          the rule package: TinyCC described as a toolchain
    presets/apple-clang-arm64.toml   the user file: the host toolchain pins
```

## Pinned inputs

- **tinycc** `43c7708b85681a2fd4451c8a541af4494a8919b2` (mob branch,
  `VERSION` 0.9.28rc), the submodule `case-studies/tcc/upstream`.
- macOS arm64. The configuration is `arm64-macos`, the runtime sources are
  lib/Makefile's `OBJ-arm64-osx`, and `CONFIG_SYSROOT` is the platform SDK
  that buildlog-cpp's inventory probe finds (`xcrun --show-sdk-path`).

## Build

```
uv run --frozen buildlog configure --user case-studies/tcc/project/presets/apple-clang-arm64.toml \
  -C BUILD case-studies/tcc/project
ninja -C BUILD/souffle
uv run --frozen buildlog test -C BUILD
```

`buildlog test` runs `bootstrap_fixpoint` (`same_file stage2/tcc stage3/tcc`)
and `hello`, and both pass. `tests/test_tcc_bootstrap.py` does the same and
checks the graph: tc_of per stage, argv[0] and implicit inputs, tool_dep,
the exec configuration, that no probe asks a built tcc anything, `why`, and
the misuse errors.

## What the author and the user write

The toolchain's author (here the case study, as tinycc could ship it)
writes a pure rule package, `packages/tcc/L5_tcc.dl`. From one accepted
fact per toolchain, `tcc_toolchain(tc, compiler, runtime, tccdir,
version)`, it derives the facts that buildlog-cpp's inventory probe would
find for an installed compiler. Nothing is probed:

- `toolchain(tc, "tcc", version)`, `toolchain_runs_on` and
  `toolchain_can_target` (c and native-link) for the host platforms TCC
  generates code for;
- `toolchain_tool_target(tc, role, compiler)` for `cc`, `link` and `ar`
  (one program plays every role), with `toolchain_tool_arg(tc, "ar",
  "-ar")`;
- `toolchain_runtime(tc, runtime)`;
- `tc_platform_arg(tc, p, "-B ", tccdir)`, so tcc finds its own `include/`;
- the family's spellings of buildlog's settings (`-O2 -DNDEBUG`, `-g`);
- `CONFIG_SYSROOT="<SDK>"` on every target that builds a tcc
  (`tcc_compiler`), from buildlog-cpp's `platform_sdk`.

The project's author writes the stages (`spec.toml`):

```toml
tcc_toolchain = [
  ["tcc-stage1", "tcc_stage1", "libtcc1_stage1", "src:upstream", "0.9.28rc"],
  ["tcc-stage2", "tcc_stage2", "libtcc1_stage2", "src:upstream", "0.9.28rc"],
]
[target.tcc_stage2]
instance_of = "tcc"
toolchain = "tcc-stage1"
```

Each target that a built toolchain builds names it with `toolchain = "..."`.
A target that names none (`tcc_stage1`) is built by the user's selection.
Only `tcc_stage1` and the toolchain pins are about the host. The user
writes nothing about the TCCs: the preset only pins Apple clang for `c` and
`native-link`, as the zlib study's does.

## How it maps to the graph

Core L2 (`src/buildlog/layers/core/L2_toolchains.dl`, "built toolchains",
"a target's toolchain") binds a built toolchain. `tc_of(t, lang, c, tc)` is
the target's own toolchain if it names one, otherwise the configuration's
`tc_for`. `action_tc_tool(a, tc, role)` spells argv[0]. For a built tool,
argv[0] is the tool target's run artifact in the exec configuration, and it
is also an implicit input. `tool_dep(t, c, compiler)` makes the compiler
needed in the exec configuration. Every link of such a target takes the
runtime as a private dependency. With a second configuration (`debug`, whose
exec configuration is `release`), the debug stages are compiled by the
release-built compilers, and `tcc_stage1` is never built for debug
(`test_tools_run_in_the_exec_configuration`).

`buildlog why --flag bin/arm64-macos.release.none/stage1/tcc --output
obj/arm64-macos.release.none/tcc_stage2/upstream/tcc.c.o` explains stage 2's
compiler. The chain runs from `action_tc_tool(..., "tcc-stage1", "cc")` to
`tc_of("tcc_stage2", "c", ..., "tcc-stage1")`, then to
`target_toolchain("tcc_stage2", "tcc-stage1")` at `spec.toml`, then to
`toolchain_runs_on`/`toolchain_can_target` from `L5_tcc.dl` and the
`tcc_toolchain` row, and ends at `executable("tcc_stage1", ...,
"out:bin/.../stage1/tcc")`.

## Differences from tinycc's own build

- `config.h` is cut to what `tcc.c` reads (version, codesign, DWARF, no
  compiled-in predefs). The paths come as `-D`: `CONFIG_SYSROOT`,
  `CONFIG_TCCDIR="/nonexistent"` (every use passes `-B`), and
  `TCC_LIBTCC1=""`, so tcc names no runtime implicitly; the toolchain's
  runtime is linked as a dependency instead.
- libtcc1 is `OBJ-arm64-osx` without `runmain.o`, `tcov.o` and the
  backtrace and bcheck objects, so `tcc -run`, `-b` and `-bt` are not
  supported. It is compiled without the Makefile's `-fPIC
  -fno-omit-frame-pointer`.
- Stage 1 is built by clang with buildlog's release flags (`-O3 -DNDEBUG`).
  The later stages get tcc's `-O2 -DNDEBUG`.
- The compilers' files are `bin/<cfg>/stage<N>/tcc`, not `tcc_stage<N>`.
  tcc ad-hoc signs its Mach-O output with `codesign -s -`, whose identifier
  is the output's file name, so two compilers with different names differ in
  those bytes even when their code is the same. With the same name, stage 2
  and stage 3 are identical, and so are their objects.

## Limitations

- macOS arm64 only. On Linux, a native tcc also needs its crt and library
  paths (`CONFIG_LDDIR`, the multiarch triplet), and the runtime needs
  `armflush.o` and `dsohandle.o`. That would be more facts in this package,
  not new mechanism, but it is not done; the test module skips elsewhere.
- A configuration's `cc_check` questions are asked of the configuration's
  toolchain (`tc_for`), not of a target's own. The study has none.
- An installed toolchain's runtime is not modelled: its driver links its
  own, so `toolchain_runtime` is honoured only for built toolchains.
