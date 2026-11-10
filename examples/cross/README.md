# cross example

A cross-compilation scenario, written as TOML relation tables (`spec.toml`, `pkg.toml`). It needs no Halide install: `blurgen` (`project/gen/blurgen.cpp`) is a stand-in for a Halide-style generator, a host tool that emits a static library and header for the target by calling the target's C compiler. The project ships its own layer for it in `packages/blurgen/` (Datalog rules and a manifest; no Python: it asks `buildlog-cpp`'s `cc_capability` probe whether the generator host compiles C), showing how a project adds a capability. It exercises exec configurations for build-time tools, conditional dependencies, a link cycle, universal2 slices and discovery requested by derived facts.

Run:

```sh
uv run buildlog configure --user examples/cross/presets/apple-clang.toml \
  -C examples/cross/build examples/cross
ninja -C examples/cross/build/souffle
```
