<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# buildlog-cuda

NVIDIA CUDA as a plugin on buildlog-cpp.
**Provisional**: nvcc on Linux, compile and link; the first study is stdgpu
(`case-studies/stdgpu/`). It changes nothing in core or buildlog-cpp: the one
buildlog-cpp relation it adds facts to is `cc_include_before` (thrust's directory
before the toolkit's, as CMake orders them).

## Use

`uses = ["buildlog-cpp", "buildlog-cuda"]`. A source ending in `.cu` (or one the
author states with `cuda_source(t, path)`) compiles with nvcc; everything else
is buildlog-cpp's. The toolkit is one discovered package: `CUDAToolkit_ROOT`,
`CUDA_PATH`, `nvcc` on `PATH`, then `/usr/local/cuda`. A probe (`cuda_toolkit`)
finds it and runs `nvcc --list-gpu-code`, so what the toolkit supports is a
fact, not a table in the plugin. A project with no CUDA source needs no toolkit.

**GPU architectures** are named sets, a configuration setting `cuda.arch`:

```toml
gpu_arch_set_code = [["ampere", "sm_80"], ["ampere", "compute_80"]]   # real SASS + PTX
config_decl_setting = [["release", "cuda.arch", "ampere"]]
```

Each virtual architecture becomes one
`--generate-code=arch=compute_80,code=[compute_80,sm_80]`, and the set is part of
the configuration's name (`arm64-linux.release.none.ampere`). A code the toolkit
does not list is an error naming the toolkit's version, and a CUDA source in a
configuration with no set is an error. There is no `native` detection: a build's
GPU code is a decision, never the build machine's GPU. `cuda_arch_of(t, set)`
gives one target its own set. (Settings are ordered by their declared priority,
then by key name, so `cuda.arch` composes with the emsdk and python plugins'
settings, which share its priority.)

**The host compiler** is the configuration's C++ toolchain if that is uniquely
determined (nvcc `-ccbin` its path). A user's `cuda_host_pin(config|"*", toolchain)`
(a `--user` file; an author needs `[author_decides] cuda_host_pin` in
`buildlog.toml`) pins another. Two candidates and no pin is an error naming both,
never a tie-break. The host is checked against the toolkit by running nvcc on it
(probe `cuda_host_check`): a toolkit that refuses it is an error showing the
toolkit's own message. There is no knob to override a refusal.

**RDC and the device link.** `cuda_rdc(t)` builds `t`'s CUDA objects with
`-rdc=true`. The device link runs `nvcc -dlink` once per executable, shared
library or module over the RDC objects that reach it through static libraries,
stopping at shared libraries (CMake's default: a shared library carries its own
device link). Its object (`cuda_dlink.o`) joins the host link. An author states
`cuda_resolve_device_symbols(lib)` for a static library that resolves device
symbols (it is device linked itself), and `cuda_dlink_drops(t, code)` when a link
deliberately drops an architecture its objects carry; without that fact, dropping
one is an error (the "silently dropped architecture" of the design). RDC objects
reaching a link through an object library are an error.

**The CUDA runtime** is an imported package fact: `cuda::cudart_static` (the
default of a target with CUDA sources, with `-ldl -lpthread -lrt`), `cuda::cudart`
(shared, with an rpath) or none, `cuda_runtime(t, static|shared|none)`; a C++
target states its own in `deps`. Two runtimes in one link are an error.
`cuda::cudadevrt`, `cuda::thrust` (the toolkit's `cccl` directory), `cuda::toolkit`
(the headers) and the libraries the toolkit ships (cuBLAS and so on) are packages
too.

Per-target property: `cuda.compile_flag`. nvcc's include directories are
buildlog-cpp's (`cc_include`): its classes, `cc_include_before` priority and the
filter for the host compiler's implicit directories.

## Limits

- nvcc only. clang as the CUDA compiler is deferred; two CUDA toolchains with no
  pin are core's ambiguity error.
- The device link has no `target_action`: which code a resolving static library
  covers is a negation over the dependency graph, which includes the runtime
  edge derived from the target's sources, and those depend on what is needed,
  which `target_action` feeds. It is an ordinary action with its own object.
- The math libraries' static dependencies (cuBLAS static and `culibos`) are not modelled.
- No GPU run: `tests/test_cuda.py` skips its run test with that reason. Run it on a
  machine with a GPU.

## Tests and the image

`tests/test_cuda.py` uses a fake toolkit (a stub nvcc) and runs anywhere.
`tests/test_cuda_toolkit.py` uses a real nvcc (Linux; skipped without one) and
runs in the CUDA image:
`IMAGE=buildlog-cuda:13.4 linux/run.sh suite gcc tests/test_cuda_toolkit.py`.
The image (`linux/cuda/Dockerfile`) adds the CUDA 13.4 toolkit for linux-sbsa to
`buildlog-linux`.
