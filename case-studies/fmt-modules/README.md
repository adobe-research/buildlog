<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# {fmt} as a C++20 module

The first real test of buildlog's C++ modules support (`docs/spec-reference.md`).
{fmt} 11.2.0 ships `src/fmt.cc`, a module interface unit that includes every
header in its global module fragment and `format.cc`/`os.cc` in its private
fragment, so the whole library is one declared interface unit:

```toml
cxx_module_interface = [["fmt", "fmt/src/fmt.cc", "public"]]
dep = [["app", "fmt", "private"]]
```

`app` says `import fmt;`. No CMake and no module names in the spec: the
collator learns `fmt` from the scan of `fmt.cc` and `import fmt` from the scan
of `main.cc`. Run `./fetch.sh` (clones {fmt} into `upstream/`), then
`tests/test_fmt_modules.py` builds and runs it (macOS: Homebrew `llvm@22`).

What it needed from buildlog: nothing beyond the modules layer. `fmt.cc` warns
under clang (`-Winclude-angled-in-module-purview` from `fmt/os.h`, upstream's).
mp-units is the later, larger case study.
