<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# buildlog

buildlog is a meta build system. A project describes its targets, options and
dependencies as facts in TOML files. A Datalog program (run by
[Soufflé](https://souffle-lang.github.io/)) derives the build graph from
those facts, and buildlog emits it as a [Ninja](https://ninja-build.org/)
build. The rules that turn facts into compile and link commands are ordinary
Datalog in layers that packages and plugins can extend.

Decisions that belong to the project author (what the targets are) are kept
apart from decisions that belong to whoever builds it (which compiler, where a
dependency lives). The second kind goes in separate "user" files and presets, so
a user never edits the author's files. Because the build graph is derived,
every flag on every command line has a proof, and `buildlog why` and
`buildlog why-not` show it.

The Python driver (`src/buildlog/`) runs under `uv`. It prepares available
dependency definitions within declared roots and composes their rules once.
Environmental probes then feed one fixed Datalog program until its discovery
fixpoint, after which buildlog writes `build.ninja`.

## Prerequisites

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- [Ninja](https://ninja-build.org/).
- A C++ compiler and `pkg-config` for the examples.
- The patched [Adobe Research Soufflé fork](https://github.com/adobe-research/buildlog-souffle).
  Its `main` branch contains the required production patches. Build it with
  `-DSOUFFLE_DOMAIN_64BIT=ON`, then point
  `BUILDLOG_SOUFFLE` at the binary:

  ```sh
  git clone https://github.com/adobe-research/buildlog-souffle.git souffle
  cd souffle
  cmake -S . -B build -DSOUFFLE_DOMAIN_64BIT=ON -DCMAKE_BUILD_TYPE=Release
  cmake --build build -j8
  export BUILDLOG_SOUFFLE=$PWD/build/src/souffle
  ```

  Stock Soufflé does not work: buildlog needs the patches described in
  [docs/proof-protocol.md](docs/proof-protocol.md#soufflé-requirement).

Check out the repository with its submodules if you want the case studies:
`git submodule update --init`. The quickstart does not need them.

## Quickstart

`examples/hello` links a small library and the system zlib. Where zlib's
`.pc` file lives is the user's decision, so the example ships a preset for
Homebrew on Apple silicon. On another machine, copy it and edit the toolchain
pins and the `.pc` directory.

```sh
uv sync --frozen
uv run --frozen buildlog configure \
  --user examples/hello/presets/homebrew-arm64.toml -C build/hello examples/hello
ninja -C build/hello/souffle
build/hello/souffle/bin/arm64-macos.release.none/hello      # hello 42 1.3.2
```

Then ask why a flag is there:

```sh
uv run --frozen buildlog why -C build/hello --output main.cpp --flag -O3 --leaves
```

See the [user guide](docs/user-guide.md) for more.

## Documentation

- [User guide](docs/user-guide.md): the CLI, the workflow, author and user files, presets, locks.
- [Spec reference](docs/spec-reference.md): the TOML vocabulary.
- [Rule authoring](docs/rule-authoring.md): packages, plugins, Datalog layers, probes, provenance.
- [Architecture](docs/architecture.md): components, the discovery loop, layering.
- [Proof protocol](docs/proof-protocol.md): how `why` and `why-not` work with Soufflé's provenance.
- [Artifact guide](docs/artifact-guide.md): environment variables, the test suite, the case studies, the Linux harness.

## Repository layout

| Path | Contents |
|---|---|
| `src/buildlog/` | The Python driver (`cli.py`, `facts.py`, ...) and the core Datalog layers in `layers/core/`. |
| `plugins/` | Packages that extend the core: `buildlog-cpp` (C/C++ rules) and other plugins. |
| `examples/` | Hello, cross-compilation, surface syntax, templates and linkage examples. |
| `tests/` | The pytest suite and its fixture projects. |
| `case-studies/` | Real projects built with buildlog and compared with their own build systems. |
| `linux/` | A Docker harness for verifying on Linux ([linux/README.md](linux/README.md)). |
| `scripts/` | Build verification and Ninja snapshot helpers. |
| `docs/` | The documentation above. |
| `formal/lean/` | Conditional discovery proofs and shipped-core invariants ([scope and build instructions](formal/lean/README.md)). |

The paper and research-only evaluation scripts live in the separate
`buildlog-artifact` repository (a sibling checkout for local reproduction).
They are not required to use or test buildlog.

## License

Copyright 2026 Adobe. All rights reserved.

Adobe-authored buildlog code is licensed under the [Apache License 2.0](LICENSE).
Third-party submodules and reference fixtures retain their own licenses and
notices; see [NOTICE](NOTICE) and [REUSE.toml](REUSE.toml).
