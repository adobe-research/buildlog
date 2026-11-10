<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# halide-build vocabulary

`share/buildlog/halide-build/` is an ordinary rule package (no discovery) for
building with Halide. Keys under `[target.X]` (named rows, see
`docs/spec-reference.md`); each is sugar over a raw relation, which stays valid:

- `halide_build_generator = true`: `add_halide_generator`. The target is an
  executable, depends privately on `Halide::Generator` and gets
  `-Wdeprecated-declarations`, as the CMake helper does under GNU and Clang.
- `halide_build_library = { from = "<generator target>", ... }`:
  `add_halide_library`. Optional `generator` and `function` (both default to
  the target name, as in CMake) and `runtime` (default: the library's own
  `<lib>.runtime`, from `halide_build_runtime_defaults`). The other keys state
  the relations `halide_build_library_<key>`:
  - `registration = "<consumer>"`: write the registration source where the
    consumer is built and compile it into the consumer (this implies emitting it);
  - `emit = ["function_info_header"]`: extra generator outputs;
  - `params = { "<condition>" = ["p=v"] }`: generator params in the
    configurations where the condition holds (also usable from a profile).
- Halide target features follow from the configuration: a platform plugin states
  `config_cpu_feature(c, f)` and `halide_build_cpu_feature` maps it to Halide's
  name (`simd128` to `wasm_simd128`, `threads` to `wasm_threads`).
- `halide_build_install`, `halide_build_runtime_defaults`, and the other raw
  relations (`halide_build_library_target`, `_feature`, `_c_backend`, ...) are
  described in `case-studies/halide/README.md`.

Example: `case-studies/halide/apps/hellowasm/spec.toml`.
