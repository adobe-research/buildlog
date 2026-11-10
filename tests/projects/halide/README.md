# Halide test projects

Projects for the Halide integration tests. The Halide rule package is a pure
(tier-2) package under `tests/fixtures/rule-packages/share/buildlog/halide`;
configure with `--package-path tests/fixtures/rule-packages` (or put that
prefix in `BUILDLOG_PACKAGE_PATH`) and opt into the local Homebrew preset:

    uv run buildlog configure --package-path tests/fixtures/rule-packages \
      --user tests/projects/halide/presets/homebrew-arm64.toml \
      -C build/bilateral_grid tests/projects/halide/bilateral_grid

`bilateral_grid` and `bilateral_grid_coverage` list `halide` in `uses`;
`linear_blur` does not, and gets it loaded by namespace. Halide names its
dependencies (`Halide::ImageIO` -> zlib, and libpng/libjpeg as optional
edges, rules on core's `defined`); `workspace.toml` declares the author
content and `presets/homebrew-arm64.toml` points `buildlog-cpp`'s importers
at Homebrew (`package_prefix` is the Homebrew prefix; `package_search_dir`
adds Homebrew's per-OS pkgconfig directory for the system zlib). The importer
that finds `libpng.pc` defines `pc:libpng`
and states the binding `alias("libpng", "pc:libpng")`, likewise libjpeg
and zlib. Optional edges do not start a search, so `package_want` asks for
libpng and libjpeg. Without a binding source configure fails with
"dependency zlib of Halide::ImageIO has no definition", and the optional
edges are absent. The
tests need a Halide install and the Halide source (the `case-studies/halide/upstream` submodule, which the tests use via `${REPO_ROOT}`).

The install location is a user decision, not an author one, so `HALIDE_PREFIX`
has no default in `workspace.toml`; pass `-D HALIDE_PREFIX=DIR` (or set it in a
`--user` file). The tests read `BUILDLOG_HALIDE_PREFIX` and forward it as that
`-D`; they skip, saying so, when it is unset or not a directory:

    export BUILDLOG_HALIDE_PREFIX=/path/to/halide-install
    uv run --frozen pytest -q tests/test_integration.py tests/test_why.py tests/test_importers.py

To produce an install, build Halide (the `case-studies/halide/upstream` submodule, CMake, macOS preset)
and run `cmake --install <build-dir> --prefix <DIR>`; the tests need
`lib/cmake`, `lib/libHalide*`, `lib/libautoschedule_mullapudi2016.so`,
`bin/gengen` and `share/tools`.
