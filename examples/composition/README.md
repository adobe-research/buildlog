# Composing independently packaged generating libraries

From the release repository, with the patched Souffle and native compiler installed:

```sh
uv run --frozen python examples/composition/run.py
```

The runner copies the projects into a fresh directory under `build/composition`,
so running it does not write locks into the example sources. It pins the native
compiler (Apple clang on macOS, GCC on Linux; `BUILDLOG_LINUX_TOOLCHAINS` can select
Clang) and installs each producer into its own prefix.

`constgen` and `offsetgen` independently export a runtime header and consumer-side
rules. An ordinary dependency on each runtime enables its rule; the consumer does
not list either rule package in `uses`. The rules translate `answer.const` and
`bonus.offset` into separate C++ sources. The resulting executable prints `42`.
The generators use POSIX shell and sed rather than additional language runtimes.

`unusedgen` is a third installed rule package in the declared search roots. Its
use-gated request and warning remain dormant: it contributes no needed target,
warning, build action, or active lock entry. The runner compares the consumer's
action and needed facts before and after adding this root. Ninja regeneration
inputs are excluded: adding a search root necessarily changes the watched inputs.
An explicit request for the third runtime provides a positive activation control:
its warning, needed target and active lock entry then appear.

Availability still makes executable code subject to validation. A conflicting
second installation of `constgen`, and an invalid rule in otherwise dormant
`unusedgen`, both reject configuration. Root curation is therefore part of the
deployment contract, not an activation-dependent escape from checking code.

The third package's use guard introduces an answer-dependent path into its
self-request. Thus this accepted example does not satisfy the sufficient
full-program W5 certificate for answer-free requests, even when that package
is dormant. Isolation of unused effects is not a claim of answer-free requests
for every accepted extension.

Each run leaves `results.json`, installed packages and all configuration outputs
in its private run directory. Build diagnostics go to standard output/error.
The JSON records preparation and whole-configure observations and single-composition
counters. These are illustrative single-run costs, not repeated benchmarks.
