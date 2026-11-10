<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: flatc as an exec tool, Bazel and buildlog

This study compares one slice of flatbuffers' upstream Bazel build with a small
buildlog project. The slice is `//:flatc`, which runs during the build, plus
one generated C++ library (`flatbuffer_cc_library(monster_fbs)` over
`samples/monster.fbs`) and its consumer `samples/sample_binary.cpp`.

Bazel is the second baseline next to CMake, and the one with a principled
host/exec story. In Bazel, a tool attribute declared `cfg = "exec"` in a rule
definition moves the tool into an *exec configuration*, and the platform that
configuration targets is chosen by execution-platform and toolchain resolution.
buildlog *infers* the same configuration: an action names a built program as the
thing it runs (`action_run`), and core derives `tool_dep` and `exec_config` from
that.

The question is where each system needs the author to say "this runs on the
build machine", and whether the action graphs agree once that is said.

Short answer:

- **Same graph shape.** Both systems build flatc once, for arm64 in an
  optimized exec configuration. They run it from the target configuration and
  compile the product for the target, which is x86_64 in the cross variant.
  Every buildlog action matches a Bazel action by (role, kind, unit), and the
  generated header is byte-identical.
- **Annotations: once per rule in both.** Bazel's exec annotation sits once in a
  rule definition (genrule's built-in `tools` attribute, used by
  `build_defs.bzl`). buildlog's sits once in the project rule (`action_run`).
  Neither system needs one in a BUILD file or spec entry. The honest difference
  is smaller than "annotated vs. zero annotations"; see [Annotations](#annotations).

## Layout

- `project/`: the buildlog project.
  - `spec.toml` declares the targets `flatbuffers`, `flatc`, `monster_fbs` and
    `sample_binary`, and three configurations.
  - `spec.toml` uses `[generator.fbs_cc]` for `flatbuffer_cc_library`; `rules.dl` only
    preserves Bazel's generated-header default output.
  - `presets/apple-clang.toml` is the user file with the toolchain pins.
- `baseline/consumer/`: the Bazel module the baseline builds.
  - It has `MODULE.bazel.in` (flatbuffers via `local_path_override`, after
    upstream's `tests/bazel_repository_test_dir`) and `BUILD.bazel`.
  - It also has `//:monster_misannotated`, the annotation experiment.
  - `.bazelversion` pins Bazel 8.8.1.
- `baseline/normalize_aquery.py`: normalizes `bazel aquery --output=jsonproto`
  into the case studies' JSONL records, the shape of
  `case-studies/halide/baseline/normalize.py`, plus Bazel's configuration and
  target fields. It uses the standard library only and is reusable.
- `baseline/compare.py`: compares a normalized Bazel graph with a normalized
  buildlog graph per (configuration role, kind, unit), including flags.
- `baseline/run-bazel.sh`, `baseline/run-buildlog.sh`: reproduce everything
  below.
- `baseline/bazel-<v>.jsonl`, `bazel-<v>-summary.txt`, `bazel-<v>-evidence.txt`:
  the Bazel side for v ∈ {native, opt, cross}. The evidence file holds the
  cquery and transitions output, `bazel config` diffs, toolchain resolution,
  file types and the experiment.
- `baseline/buildlog-<v>.jsonl`, `compare-<v>.txt`, `why-*.txt`: the buildlog
  side and the comparisons.
- `tests/test_bazel_flatc_case.py` (in the repo root).

## Pinned inputs

- **flatbuffers v25.12.19** (commit 7e163021e59cca4f8e1e35a7c828b5c6b7915953),
  the same tag as the CMake study (cs-flatbuffers). The checkout is
  the submodule `case-studies/bazel-flatc/upstream`, overridable with
  `BUILDLOG_BAZEL_FLATC_SRC`.
- **Bazel 8.8.1** (via bazelisk and `.bazelversion`). Upstream CI tests 7.x and
  8.x, and Bazel 9 fails in rules_cc 0.0.16.
- **apple_support 1.23.1**, a `bazel_dep` listed first in `MODULE.bazel`.
  Without it, rules_cc's autodetected toolchain picks Homebrew's `ld64.lld` and
  every link fails (`-fuse-ld=ld64.lld:`). With it, the C++ toolchain is Xcode
  clang through apple_support's wrappers.
- **Machine:** macOS on arm64, Xcode clang. Both systems use the same compiler.
- Bazel's output base is `--output_user_root=$BAZEL_FLATC_WORK/bazel-root` (default `build/case-bazel-flatc` in the repository),
  and every build uses `--jobs=2` (`ninja -j2`). The checked-in results were recorded with 4 jobs; the job count does not change the action graph.

## Running

```sh
case-studies/bazel-flatc/baseline/run-bazel.sh      # native opt cross: build, aquery, cquery, normalize
case-studies/bazel-flatc/baseline/run-buildlog.sh   # configure, ninja -j2, normalize, compare, why
uv run --frozen pytest -q tests/test_bazel_flatc_case.py
```

The three variants:

| variant | Bazel | buildlog |
|---|---|---|
| native | `-c fastbuild` (default) | `--config native`: arm64-macos, `native.opt=debug` |
| opt | `-c opt` | `--config opt`: arm64-macos, `native.opt=release` |
| cross | `--platforms=@build_bazel_apple_support//platforms:darwin_x86_64 --experimental_platform_in_output_dir` | `--config cross`: x86_64-macos, `native.opt=debug` |

In all three, `bazel build //:sample_binary` and buildlog's `request_all
sample_binary` request only the product. flatc is built because the genrule
runs it.

## Results

### Which actions exist, and in which configuration

The cross variant (`compare-cross.txt`). Native is identical up to the target
triple (`compare-native.txt`).

| role | kind | Bazel | buildlog | matched | only Bazel | only buildlog |
|---|---|---|---|---|---|---|
| exec | compile | 41 | 37 | 37 | 0 | 0 |
| exec | archive | 12 | 1 | 1 | 11 | 0 |
| exec | link (flatc) | 1 | 1 | 1 | 0 | 0 |
| target | compile | 7 | 7 | 7 | 0 | 0 |
| target | archive | 3 | 1 | 1 | 2 | 0 |
| target | custom (flatc run) | 1 | 1 | 1 | 0 | 0 |
| target | link (sample_binary) | 1 | 1 | 1 | 0 | 0 |

**Configurations.** Bazel (cquery `deps(//:sample_binary)`,
`bazel-cross-evidence.txt`):

- flatc and everything it links are in `af7c1be`, the exec configuration
  `darwin_arm64-opt-exec`, with `isTool`;
- sample_binary, monster_fbs and the genrule are in `0c8de79`, which is
  `darwin_x86_64-fastbuild`.

buildlog:

- flatc is `bin/arm64-macos.release.none/flatc`, the exec configuration of
  `x86_64-macos.debug.none`;
- everything else is in `x86_64-macos.debug.none`.

In both systems `file` reports flatc as `Mach-O 64-bit executable arm64` and
sample_binary as `x86_64`. Both sample_binaries print "The FlatBuffer was
successfully created and verified!" (under Rosetta).

**The generated header** `monster_generated.h` is byte-identical across both
systems and all three variants (sha256 `b735c911…`, checked by the test).

**In opt, the systems differ in count, by design.** Bazel's exec
configuration is never the target configuration. Even under `-c opt` it differs
in `is exec configuration`, `platform_suffix` and `-g0` (`bazel-opt-evidence.txt`). So
the six sources of the parser core (`//src:flatbuffers`, `code_generators`,
`generate_fbs`), which both flatc and sample_binary link, are compiled twice.
buildlog's exec configuration of `arm64-macos.release.none` is
`arm64-macos.release.none` itself (`exec_config(c, c)`), so there is one
instance. `compare-opt.txt` reports "buildlog actions serving both roles (one
instance where Bazel has two): 7": six compiles and one archive. The same six
sources are also built twice in native and cross, by both systems, because
there the configurations really differ (debug vs release, x86_64 vs arm64).

### Flags (matched actions; `compare-*.txt`)

| | Bazel | buildlog |
|---|---|---|
| exec compile | `-O2 -g0 -DNDEBUG -DNS_BLOCK_ASSERTIONS=1` | `-O3 -DNDEBUG` |
| target compile, native/cross | `-O0 -DDEBUG` (fastbuild) | `-O0 -g` (debug) |
| target compile, opt | as exec | as exec |
| target selection | `-target arm64-apple-macosx26.5` / `x86_64-apple-macosx26.5` | `--target=arm64-apple-macos13` / `x86_64-apple-macos13` |
| always, Bazel only | `-D_FORTIFY_SOURCE=1 -fstack-protector -Wall -Wthread-safety -Wself-assign -fno-omit-frame-pointer -fcolor-diagnostics -no-canonical-prefixes -pthread -Wno-builtin-macro-redefined -D__DATE__="redacted"` (×3) | |
| search paths per compile | 3–18 (`_virtual_includes`, `-iquote` per package) | 2–3 |
| link, Bazel only | `-lc++ -fobjc-link-runtime -framework Foundation -headerpad_max_install_names -Wl,-no_warn_duplicate_libraries`, exec also `-dead_strip` | |
| archive | `libtool -D -no_warning_for_no_symbols -static` | `ar rcs` |
| flatc run | `flatc -I ./ -I <bindir> -I <bindir> -I flatc.runfiles/flatbuffers+ <DEFAULT_FLATC_ARGS> -c -o <bindir> $f` in a shell loop | `flatc -I $SRC <DEFAULT_FLATC_ARGS> -c -o gen/<cfg>/monster_fbs $SRC/samples/monster.fbs` |

`<DEFAULT_FLATC_ARGS>` is the same in both: `--gen-object-api --gen-compare
--no-includes --gen-mutable --reflect-names --cpp-ptr-type
flatbuffers::unique_ptr`. Every flag difference is a toolchain default (apple_support's
feature set vs. buildlog-cpp's) or a layout choice. None comes from the
exec/target distinction, which both systems draw the same way (optimized
tools, product in the requested mode).

### Annotations

What the author of each side wrote to make flatc run on the build machine:

| | Bazel | buildlog |
|---|---|---|
| per use (BUILD file / spec entry) | nothing: `flatbuffer_cc_library(name, srcs)` | one generator instance row naming the schema |
| per rule (macro / rule package) | `tools = [flatc]` in the genrule call (`build_defs.bzl:124`, `:160`) | argv[0] is `flatc` in `[generator.fbs_cc]` |
| per attribute kind (rule class) | `tools` is `cfg = "exec"` in genrule's built-in rule class (Bazel's Java). A Starlark rule writes `attr.label(cfg = "exec", executable = True)` | none: the exec configuration is derived from the use, whatever relation produced it |
| on the tool target | nothing: `//:flatc` is a plain `cc_binary` | nothing: `[target.flatc]` is a plain executable |

`grep -r 'cfg *= *"exec"'` over flatbuffers' `.bzl` and BUILD files finds **0**
matches. The exec transition comes entirely from genrule's `tools` attribute.
The nine `flatbuffer_cc_library` uses in the tests write nothing about exec.

So the annotation Bazel requires is written by the rule author, once. buildlog
also needs one statement per rule: `action_run` rather than `action_arg`, which
says "this argument position is a program to run". The real difference is
narrower than "annotation vs. none":

1. **Bazel annotates the attribute; buildlog classifies the use.** Bazel's
   `cfg = "exec"` is a property of the attribute, fixed before any action exists.
   If a tool is used both as a runnable and as data (for example, packaged into
   the product), Bazel needs two attributes, one per configuration. In
   buildlog, the same target reached via `dep` and via `action_run` gets two
   configurations, derived per edge.
2. **Bazel's transition is chosen by the rule class; buildlog's comes from the
   action.** A `genrule` has `tools` (exec) and `srcs` (target), and the
   author picks the right list. In buildlog the program position of an action
   is the only way to run something (`action_run`), and core turns it into
   `tool_dep`/`exec_config`. The rule author cannot get the *configuration*
   wrong while still using `action_run`, but they can still bypass it (see the
   next section).

### The annotation experiment: putting the tool in the wrong place

Both systems were given the same mistake: flatc's path in the *product's*
configuration.

- **Bazel:** `//:monster_misannotated` in `consumer/BUILD.bazel` is a genrule
  with flatc in `srcs` instead of `tools`. Under the cross flags,
  `bazel build //:monster_misannotated` **succeeds**. cquery shows
  `srcs#…//:flatc#NoTransition`, and the genrule runs
  `bazel-out/darwin_x86_64-fastbuild/bin/external/flatbuffers+/flatc`, which is
  `Mach-O 64-bit executable x86_64`. Rosetta runs it silently. On a Linux host
  with a foreign target the action would fail at run time.
- **buildlog:** after the UX pass, the project uses `[generator.fbs_cc]`; the
  regression test appends a bad project rule that spells `flatc`'s
  product-configuration executable path as argv[0]. Core rejects the generating
  action with "action runs a program built for another platform" while emitting
  the rest of the graph under `--keep-going`.

Until step 4, the project's `rules.dl` added a check rule (design input 3).
Core now makes the check (L1_targets.dl, "the exec inference check"). An error
is raised when an action's argv[0] is the run artifact of a target this build
makes for a platform other than `host_platform`, unless that artifact is a
universal binary that includes the host. The error is attributed to the action,
so configure fails as before. With `--keep-going` the rest of the graph is
emitted, and the header's edge fails. With the misannotated rule under
`--config cross`, it reports:

```
error: action runs a program built for another platform:
  out:gen/x86_64-macos.debug.none/monster_fbs/monster_generated.h: out:bin/x86_64-macos.debug.none/flatc
  (flatc in x86_64-macos.debug.none; the build machine is arm64-macos; name it with action_run)
```

It does not fire on the correct rule, or on the misannotated rule under
`--config native`, where the product's platform is the host's
(`test_misannotated_rule_is_caught`). Bazel has no analogue: `srcs` is simply
target-configured data, and a genrule's `cmd` may run anything.

### How the exec platform is chosen

**Bazel.** The execution platforms are those registered
(`register_execution_platforms`, `--extra_execution_platforms`) plus the
host platform, `@platforms//host:host` (`--host_platform`). Resolution runs per
target and per toolchain type:

- It picks the first execution platform for which a toolchain matches both
  the *target* constraints (`target_compatible_with`) and its own
  `exec_compatible_with`. Here, for target `darwin_x86_64` it selects
  `cc-compiler-darwin_x86_64` with execution platform `@platforms//host:host`,
  and rejects the arm64 toolchains ("mismatching values: aarch64").
- The exec transition then sets `--platforms` to that execution platform
  (`bazel config` diff: `platforms: [darwin_x86_64], [@platforms//host:host]`).
  The tool is then configured and its toolchain resolved *for the host* as the
  target.
- The exec configuration's other options are Bazel's fixed exec policy:
  `compilation_mode=opt` (overridable with `--host_compilation_mode`),
  `--host_copt`/`--host_cxxopt`, `-g0`, `strip=always`.

**buildlog.**

- `exec_config(c, e)` (L1_targets.dl:133) puts `e` on the probed
  `host_platform` (inventory probe: `arm64-macos`).
- `e`'s settings are `c`'s after the layer's exec policy, e.g.
  `setting_exec_value("native.opt", "release")` in L3_native.dl:44 (tools run
  optimized). The user relation `exec_setting(k, v)` overrides it.
- The compiler for `e` is the user's pin `["cxx", "arm64-macos", "arm64-macos",
  "apple-clang"]`, checked against discovery (`runs_on`/`can_target`).
- There is exactly one execution platform, the build machine. There is no
  constraint language: the user pins a toolchain per (host, target) pair
  instead of Bazel selecting one by constraint.

### `buildlog why` next to Bazel's evidence

Bazel: why is flatc in the exec configuration? (`bazel-cross-evidence.txt`)

```
$ bazel cquery --transitions=lite 'deps(//:monster_fbs_srcs, 1)'
NoTransition -> //:monster_fbs_srcs (0c8de79)
  srcs#//:monster.fbs#(null transition) ->
  tools#@com_github_google_flatbuffers//:flatc#(exec + (TestTrimmingTransition + ConfigFeatureFlagTaggedTrimmingTransition)) -> af7c1be

$ bazel config 0c8de79 af7c1be
  platforms: [@@apple_support+//platforms:darwin_x86_64], [@@platforms//host:host]
  compilation_mode: fastbuild, opt
  is exec configuration: false, true
  copt: [], [-g0]    strip: sometimes, always
```

buildlog: why is flatc compiled with this clang++ in arm64-macos.release.none?
(`why-flatc-cross.txt`, abridged)

```
$ buildlog why -C B --with-demand --flag clang++ --output bin/arm64-macos.release.none/flatc
action_arg("out:bin/arm64-macos.release.none/flatc", 0, "", ".../clang++")
  executable("flatc", ‹arm64-macos.release.none›, …)
    needed("flatc", ‹arm64-macos.release.none›)                      [L1_targets.dl:291]
      tool_dep("monster_fbs", ‹x86_64-macos.debug.none›, "flatc")     [L1_targets.dl:534]
        tool_ref(… "monster_generated.h", "flatc", "run")
          action_run(… "monster_generated.h", 0, "", "flatc", "run")  [project:rules.dl:40]
            fbs_cc_out("monster_fbs", ‹x86_64-macos.debug.none›, …)
              fbs_cc_library("monster_fbs")  ← spec.toml:82
              … needed("sample_binary", ‹x86_64-macos.debug.none›) ← request_all ← --config cross
            fbs_cc_tool("monster_fbs", "flatc")  ← spec.toml:83
        tool_resolves("monster_fbs", "flatc", "flatc")
      exec_config(‹x86_64-macos.debug.none›, ‹arm64-macos.release.none›)  [L1_targets.dl:133]
```

and, with `--structural`, where the exec configuration's settings come from
(`why-exec-config-cross.txt`):

```
exec_config(‹x86_64-macos.debug.none›, ‹arm64-macos.release.none›)
  config(‹x86_64-macos.debug.none›) ← decl_cfg("cross", …) ← spec.toml:7
  host_platform("arm64-macos") ← probe inventory request
  exec_settings(debug,none → release,none)
```

The two answer the same question at different levels:

- Bazel names the *attribute* (`tools`) and the *transition* (`exec`), and
  `bazel config` diffs the options.
- buildlog names the *action* whose program position holds flatc
  (`rules.dl:40`), the user request that made that action needed
  (`request_all`, `--config cross`), and the exec rule.

Bazel's chain stops at the attribute, because the rule class decides.
buildlog's goes back to the spec lines.

## Ledger

Every difference between the two graphs, with its cause.

1. **Per-cc_library archives** (Bazel 12 exec + 3 target, buildlog 1 + 1).
   Bazel archives every `cc_library` (`libflatc_library.a`,
   `libcpp_generator.a`, …). The buildlog project folds flatc's libraries into
   one executable target and the parser core into one static library, as
   flatbuffers' CMake build does. This is a modelling choice of the slice, not
   an exec difference.
2. **Four exec sources compiled twice by Bazel** (`bfbs_gen_lua.cpp`,
   `bfbs_gen_nim.cpp`, `idl_gen_text.cpp`, `util.cpp`). Upstream's
   `//src:flatc` lists them, and so do `//src:flatc_library` or
   `//src:flatbuffers`: 41 exec compiles vs. 37. This is an upstream Bazel quirk.
3. **The parser core in both configurations under `-c opt`** (Bazel 6 + 6
   compiles, buildlog 6). Bazel's exec configuration is always distinct.
   buildlog's exec configuration of a release configuration on the host is that
   configuration. This is by design in both.
4. **Flags**: see the flags table above. Every difference comes from the
   toolchain feature set (apple_support vs. buildlog-cpp) or the header layout.
   The exec/target split agrees.
5. **Deployment target**: Bazel `-target …-macosx26.5` (the SDK version),
   buildlog `--target=…-macos13` (buildlog-cpp's platform triple).
6. **flatc include and output paths**: Bazel adds `-I <bindir>` ×2 and
   `-I flatc.runfiles/flatbuffers+`, which are runfiles and sandbox layout. It
   also runs flatc in a `for f in $(SRCS)` loop.
7. **Toolchain wrappers**: apple_support's `wrapped_clang`/`cc_wrapper.sh` are
   themselves built in the exec configuration (4 actions), so every Bazel
   compile runs an exec-built tool. They are excluded by label
   (`^@apple_support//`) and reported in `bazel-<v>-summary.txt`.
8. **Housekeeping** (254 `_virtual_includes` symlinks, runfiles trees and
   manifests, build info, strip): these have no Ninja counterpart. They are
   excluded by mnemonic.
9. **Bazel output-dir collision**: without `--experimental_platform_in_output_dir`,
   the cross target configuration's output dir is still `darwin_arm64-fastbuild`
   (the mnemonic follows `--cpu`, not `--platforms`), so it overwrites the
   native build's outputs. The baseline uses the flag. aquery's `platformName`
   also follows `--cpu` (it says `darwin_arm64` for the x86_64 target); the
   normalizer keeps it as `platform_name` and documents it. buildlog's
   directories are named from the configuration record (`x86_64-macos.debug.none`).
10. **One exec configuration per target configuration**: in Bazel the exec
    configuration's hash differs by target config (`f50cc19` native vs.
    `af7c1be` cross, whose `platform_suffix`/output dir differ). In buildlog,
    `exec_config(native) = exec_config(cross) = arm64-macos.release.none`. When
    one build selects all three configurations, flatc is built once for all of
    them (62 Ninja edges).

11. **Shared-library versioning: not in the slice.** Neither system builds a
    shared library here. Upstream's Bazel build has no `cc_shared_library` or
    `linkshared` target, and the buildlog project builds `flatbuffers` static.
    For the record, upstream's CMake build (option
    `FLATBUFFERS_BUILD_SHAREDLIB`, CMakeLists.txt:485-495) does version one:
    - `flatbuffers_shared` (OUTPUT_NAME `flatbuffers`) has **VERSION =
      SOVERSION = 25.12.19**, the full calendar version (`PROJECT_VERSION`).
      Upstream's comment gives the reason: there are "no ABI stability
      guarantees", so every release gets a new SONAME.
    - On macOS, CMake maps these to the install name
      `@rpath/libflatbuffers.25.12.19.dylib`, `current_version` 25.12.19 (from
      VERSION) and `compatibility_version` 25.12.19 (from SOVERSION), since
      `MACHO_*_VERSION` are unset. That is CMake's documented mapping; it was
      not built or checked with `otool -L` here.
    - The package version file (`CMake/flatbuffers-config-version.cmake.in`)
      says something else: it accepts any *newer* version
      (`PACKAGE_VERSION_COMPATIBLE` unless less than the requested one), which
      is CMake's AnyNewerVersion.

    buildlog does not model SOVERSION, install_name or compatibility_version
    yet; see design input 7.

Not verified: a cross-platform Bazel build on Linux (the Linux section covers native and opt), Bazel remote execution, a Bazel execution
platform other than the host, and whether Bazel 9's exec-configuration naming
changes counts. buildlog was not run with an execution platform other than
the build machine, because it has none.

## What Bazel does better

This slice exercises Bazel's weakest point (its exec machinery is overkill
for one host) and buildlog's strongest (inference from one action). A fair
account of what Bazel does and buildlog does not attempt:

- **Hermeticity and sandboxing.** Every Bazel action runs in a sandbox with only
  its declared inputs. An undeclared header or tool fails the build. buildlog
  emits Ninja and trusts depfiles; an undeclared input is a correctness bug it
  cannot see.
- **Remote execution and caching.** Bazel's execution platforms are real
  machines. An exec-configured flatc can be built and run on a Linux remote
  worker while the product targets macOS, and outputs are content-addressed
  and shared. buildlog's exec platform is always the machine running Ninja.
- **Toolchain resolution by constraints.** Bazel selects a C++ toolchain by
  matching constraint values of target and execution platforms. New platforms
  and toolchains compose without a user table. buildlog asks the user to pin a
  toolchain per (host, target) pair, and discovery only checks the pin.
- **Multiple execution platforms.** Bazel can resolve a different execution
  platform per action (for example, a GPU tool on a GPU runner). buildlog's
  `exec_config` has one host.
- **A stable, documented exec policy** (`--host_compilation_mode`,
  `--host_copt`) that every rule set shares. buildlog's is per layer
  (`setting_exec_value`) and only visible with `why --structural`.

**Scope of the comparison.** The claim "exec configuration inferred,
zero annotations" is precise only when stated as follows:

- buildlog needs no *attribute-level* or *per-use* exec annotation. The
  configuration of a tool is derived from an action that runs it.
- Bazel's annotation is also written once, in the rule definition, and
  flatbuffers' users write none. The difference is in *who* can get it wrong
  and *where*: an attribute's transition vs. an action's program position.
- Neither system caught a tool spelled in the product configuration without
  an explicit check. buildlog expressed that check in its own language
  (design input 3), and since step 4 core makes it. Bazel's `genrule` cannot.
- The claim does not extend to hermeticity, remote execution or
  constraint-based toolchain selection. buildlog trades those for a model
  where the exec platform is the build machine and the toolchain is pinned.

## Design input

Limitations and opportunities exposed by the comparison:

1. **Read "no per-use or per-attribute exec annotation", not "no
   annotation".** `action_run` is the one statement per rule, parallel to
   Bazel's `tools`. The comparison is only fair with that phrasing, and the
   distinction matters when comparing the systems.
2. **`why` explains an exec configuration by its declaration when one
   exists.** In `--config cross`, `config(‹arm64-macos.release.none›)` is
   explained via `decl_cfg("opt", …)`, because declared configs exist whether or
   not they are selected, and `why` prefers that derivation over
   `config(e) :- exec_config(_, e)`. The `needed` chain is right
   (`action_run → tool_ref → tool_dep → exec_config`), but the config node
   suggests the user's `opt` declaration matters. It does not: without it, the
   exec config would still exist. `why` could prefer the derivation through
   the node's demand path, or mark unselected declarations.
3. **A core check that a run program is built for the build machine.** The
   project's rule (`rules.dl`, last rule) catches the symmetric misannotation
   (`dep` plus `action_arg` in place of `action_run`) that neither core nor
   Bazel reports. In core it would be cheap: argv[0] ∈ `executable(_, c, _)`
   with `config_platform(c) ≠ host_platform` and not a universal slice. It is
   currently a *global* error (unattributed), which aborts configure. In core
   it should attach to the action as a failing edge.
   - *Step 4:* done as described, as an `action_error` in L1_targets.dl, over
     any run artifact (not only executables). The project's rule is gone. See
     "Step 4 changes".
4. **Show the exec policy in the default `why`.** `setting_exec_value("native.opt",
   "release")` explains why flatc is `release`, but only `--structural`
   shows it. It is the buildlog analogue of Bazel's `compilation_mode: fastbuild,
   opt` diff and belongs in the user-facing chain.
5. **Keep the "one exec instance" property visible.** Sharing
   (ledger 3 and 10) is a measurable advantage over Bazel for release builds and
   multi-config builds: 7 fewer actions in opt here, and one flatc for three
   configs.
6. **The aquery normalizer is reusable.** Any Bazel baseline (e.g. a
   rules_cc project) can be compared with `compare.py` given `--role`. It
   generalizes the Halide/CMake record shape with `configuration`,
   `config_role`, `tool_config` and `target_rule`.

7. **Versioning of exported shared libraries (not implemented).** flatbuffers
   is a sharp case for the exported version constraint (ledger 11). The
   SONAME, 25.12.19, says "exactly this release" for ABI, while the CMake
   package version file says "this or anything newer" for `find_package`.
   - If buildlog exports a `flatbuffers_shared` component, CPS would carry
     `version = "25.12.19"` and a `compat_version`. The honest value from
     the SOVERSION is `compat_version = "25.12.19"`, meaning no older or newer
     release is ABI compatible. buildlog's `export_compat` should derive it
     from the modelled SOVERSION, not copy the looser CMake policy.
   - A downstream `requires = { flatbuffers = ">=25.12.19" }` should then
     mean *API* (source) compatibility for resolution. For the shared
     component, the resolved version must also match the `compat_version`
     exactly, or the downstream must rebuild.
   - On macOS, `compatibility_version` equal to `current_version` makes dyld
     enforce the same thing.
   - For generated code there is a further coupling: `monster_generated.h`
     static-asserts `FLATBUFFERS_VERSION_*` against the runtime headers. flatc
     (exec) and the runtime library (target) must come from the same release,
     which both systems here guarantee by building both from one checkout.
     A buildlog model of `requires` across packages would need to express
     "the tool's version equals the runtime's version".

## UX input

These are the authoring and usability frictions from this study. Each item
gives the snippet as written, the wished-for form, and the line counts. None
is implemented.

**Spec size.** Lines are counted without blanks, comments or source-list
entries.

| | buildlog | Bazel (upstream) |
|---|---|---|
| use site | `spec.toml` 30 (+37 source entries) | consumer `BUILD.bazel` 11 (`load`, `flatbuffer_cc_library`, `cc_binary`) plus `MODULE.bazel` 8 |
| the flatc/runtime targets | included in the 30 above | `BUILD.bazel` 79, `src/BUILD.bazel` 83, `grpc/src/compiler/BUILD.bazel` 107 (whole files, which contain more targets than the slice) |
| the generator rule | `rules.dl` 31, plus 3 for the check rule | `build_defs.bzl` 265 (whole file, covering all languages; the C++ path is roughly 130) |
| project and user files | `buildlog.toml` 5, `presets/apple-clang.toml` 6 | `.bazelversion` 1, `MODULE.bazel` (above) |

buildlog's 30 spec lines replace Bazel's per-directory BUILD files, because the
project folds flatc's libraries into one executable target. Its rule is much
shorter than `build_defs.bzl`, but most of those 31 lines are string and path
plumbing (items 1–3).

1. **Basename derivation in Datalog** (`rules.dl:18-25`, 8 lines).
   - Written: build_defs.bzl's `base_name = s.split("/")[-1].replace(".fbs", "")`
     became a helper relation of slash positions, a `max` aggregate, and two
     `substr`/`strlen` rules (with and without a directory).
   - Wished: a string functor, e.g. `stem(s)` / `basename(s)`, so it becomes
     `fbs_cc_base(l, stem(s)) :- fbs_cc_schema(l, s), suffix(s, ".fbs").`
     That is 1 line.
2. **Output path formatting** (`rules.dl:28-30`, 3 lines).
   - Written: `d = cat("out:gen/", n, "/", l)` with `needed(l, c),
     cfg_name(c, n)`. Every generator rule rebuilds the per-configuration
     generated directory by hand.
   - Wished: a core relation `gen_dir(l, c, d)`, so it becomes
     `fbs_cc_out(l, c, d, cat(d, "/", b, "_generated.h")) :- gen_dir(l, c, d), fbs_cc_base(l, b).`
3. **A constant flag list as ranked facts** (`rules.dl:33-36`, 4 lines).
   - Written: `fbs_cc_flag(1, "--gen-object-api"). fbs_cc_flag(2, "--gen-compare"). …`
     with hand-numbered ranks, then `action_arg(o, 10 + r, "", f)`. Argument
     ranks (1, 2, 10+r, 30–32) are hand-allocated across four rules.
   - Wished: a TOML list in the rule package, e.g.
     `[fbs_cc_library.defaults] flatc_args = ["--gen-object-api", …]`, and
     `action_args(o, list)` that ranks a list itself. That would be 1 line of
     TOML and 1 line of Datalog.
4. **Declaring a new abstraction** (`rules.dl:12-15`, 4 `.decl` lines, plus
   `target(l) :- fbs_cc_library(l).`).
   - Written: every field of `[fbs_cc_library.monster_fbs]` needs a `.decl` by
     hand. The TOML table key and the `.decl` prefix must agree
     (`fbs_cc_tool` in both).
   - Wished: a schema table,
     `[abstraction.fbs_cc_library] fields = { tool = "ref", schema = "path", dep = "ref*" }`,
     generating the decls. The spec entry could then drop the prefix and say
     `tool = "flatc"`, `schema = …`. That would be 1 table of 4 lines, and 3
     spec lines without the repeated prefix.
5. **Configurations in two tables** (`spec.toml:7-12`, 6 lines).
   - Written: `config_decl = [["cross", "x86_64-macos"], …]` plus
     `config_decl_setting = [["cross", "native.opt", "debug"], …]`, with each
     configuration name repeated.
   - Wished: `[config.cross] platform = "x86_64-macos"`, `native.opt = "debug"`,
     2 lines per configuration.
6. **Repetitive toolchain pins** (`presets/apple-clang.toml`, 4 rows).
   - Written: one row per (tool kind × host × target):
     `["cxx", "arm64-macos", "x86_64-macos", "apple-clang"]` and
     `["native-link", …]` again.
   - Wished: `[toolchain.apple-clang] host = "arm64-macos", targets = ["arm64-macos", "x86_64-macos"]`
     (covering cxx and link), 1 table of 2 lines.
   - This tiny user file is also a separate file per machine (file sprawl).
     The project is `buildlog.toml` + `spec.toml` + `rules.dl` + `presets/…`:
     4 files for one generator and 4 targets.
7. **`config_default` help text contradicts the check.**
   - `buildlog configure --help` says the default is "the spec's
     config_default". Putting it in the spec fails with:
     `spec.toml:1: config_default is a user relation. Move this fact to a file passed with --user, …`.
   - The error itself is good. The help text (cli.py) and engine docstring
     should say "the user file's".
8. **`why --flag` needs the exact spelled token.**
   - `buildlog why --flag clang++ --output bin/…/flatc` answers "no action_arg
     spells clang++". The flag must be the full
     `/Applications/Xcode.app/…/clang++` path (the script uses
     `$(xcrun --find clang++)`).
   - Wished: a basename or substring match, or a list of close candidates in
     the error.
9. **`why` on a configuration term** (`run-buildlog.sh`).
   - Written: `buildlog why --structural 'exec_config(["x86_64-macos", [["native.opt", "debug"], [["native.sanitize", "none"], nil]]], ["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], nil]]])'`.
     This is 1 line of 190 characters with cons-list syntax. The obvious form,
     `exec_config(x86_64-macos.debug.none, arm64-macos.release.none)`, fails:
     "cannot parse … expected a value at offset 12".
   - Wished: configuration names (as printed in `‹…›` and in directory names)
     accepted wherever a `Cfg` is expected. Something like
     `buildlog why --config-of flatc --config cross` would be better still.
10. **`why` output is long and centred on the wrong node.**
    - `why-flatc-cross.txt` is 137 lines for one question ("why is flatc
      built in the exec configuration"). The informative chain (`action_run →
      tool_ref → tool_dep → exec_config`) is 6 lines. The rest derives the
      basename (item 1: slash positions, `"/" = "/"`, `7 = 7`), the
      configuration's settings suffix, and the config's declaration.
    - The configuration node is explained via the unselected `opt`
      declaration (design input 2). The exec policy (`setting_exec_value`)
      is missing unless `--structural` is used (design input 4).
    - Wished: a summary mode that shows only project facts, spec lines and
      the core rules crossing layers, and folds arithmetic and string
      builtins.
11. **Errors raised by project check rules are global.**
    - The misannotation check (`rules.dl`, last rule) aborts configure with
      nothing emitted, and a test must catch `SystemExit`. Its message names
      the action, but the error is not attached to it.
    - Wished: `error_at(action, msg, detail)`, or attribution by the first
      argument when it is an action or target, so it becomes a failing edge
      like core's.

## Tests

`tests/test_bazel_flatc_case.py`:

- unit tests of `normalize_aquery.py` (configuration roles, `$OUT_<role>`,
  `--map` only at a path's start, a genrule running an exec-built flatc →
  `tool_config: exec`, housekeeping and label exclusions) and `compare.py`
  (a shared exec=target configuration, flag extraction);
- checks of the checked-in baseline: no machine paths; flatc linked only in
  exec and run with `tool_config: exec` in all variants; no exec, host or
  tool annotation in the project;
- integration tests, which skip without the checkout, Souffle or ninja:
  - the cross build puts flatc only in `bin/arm64-macos.release.none`, with
    arm64 flatc and x86_64 sample_binary, and the header's sha256 equals
    Bazel's;
  - the misannotated rule is rejected in cross, by core since step 4, with a
    failing edge under `--keep-going`, and accepted in native.

## Step 4 changes

Spec lines are measured with `historical source-line accounting` (non-blank, non-comment).

| Change | Files | Before | After |
|---|---|---|---|
| §6: the exec inference check is core's; the project's check rule goes | rules.dl | 34 | 31 |
| | total | 69 | 66 |

- The graphs are unchanged. The check fires on the misannotated rule as before,
  and it now fails the action, not the configuration. The error names the
  configuration, the build machine and the fix.


## UX pass port

The port replaced the project-owned `fbs_cc_library` Datalog rule with a core
`[generator.fbs_cc]` template, a `word_list` for `DEFAULT_FLATC_ARGS`, and ordinary
`[target.*]` tables (`kind`, `sources`, `deps`, `public`/`private`, `gen_include`).
Path helpers were not needed at the one use site because the generated header name is stated in
the generator instance; the only remaining Datalog line preserves the generated header as a
default output for graph identity.

| file | before (4a1ccb0) | after |
|---|---:|---:|
| buildlog.toml | 5 | 5 |
| rules.dl | 34 | 1 |
| spec.toml | 30 | 43 |
| total | 69 | 49 |
| Datalog | 34 | 1 |

Graph identity: `native`, `opt` and `cross` compare `IDENTICAL` with `ninja_norm.py`.
Bazel rule-library accounting: upstream `build_defs.bzl` is library code; buildlog's reusable
rule is 18 TOML lines plus one Datalog compatibility line, while the use site is one generator
instance row plus the small `monster_fbs` target table.

## Linux (arm64, GCC 13 and Clang 18, tier 3)

Bazel 8.8.1 (linux-arm64, in the image) builds the same consumer with `rules_cc`'s auto-configured
compiler (GCC; `CC=clang CXX=clang++` for the Clang run, which also writes `CppModuleMap` actions);
buildlog's spec and rules are the macOS ones. `linux/run.sh baselines gcc|clang bazel-flatc` runs
`run-bazel.sh` and `run-buildlog.sh` in the container and writes `baseline/linux/` and
`baseline/linux-clang/` (`bazel-<v>.jsonl`, `buildlog-<v>.jsonl`, `compare-<v>.txt`, evidence).

- Variants: **native** (debug target, release exec) and **opt**. There is **no cross variant**: its
  target is `x86_64-macos` through `apple_support`, and an x86_64 Linux product would need a cross
  C++ toolchain and sysroot in the image (and a custom toolchain in Bazel), which is outside the
  matrix; the exec/target split is still shown (flatc in `arm64-linux.release.none` only, run by the
  debug target's genrule). So the exec-inference check's *rejection* test (cross) is macOS only; on Linux
  the misannotated rule is checked to be accepted in native.
- The consumer's `MODULE.bazel` lacks `apple_support` on Linux (the script drops that line; the
  file in the repository is unchanged).
- Results (GCC and Clang equal): target compile 7/7 and the genrule 1/1 matched, flatc built in Bazel's
  exec and buildlog's `arm64-linux.release.none` (`tool built in: Bazel exec, buildlog exec`), exec compile
  37/37 matched; the header's sha256 equals macOS's Bazel header
  (`b735c911…`). Bazel's counts are higher than on macOS because of what Bazel does on Linux, not
  because of the project: it compiles every exec source twice (PIC and non-PIC objects, 82 vs 41) and
  also links `.so` per `cc_library` for flatc's dynamic mode (13 links, 24 archives vs 1 and 12), which
  buildlog has no counterpart for (a library kind is the preset's, one per configuration).
  Flag differences are Bazel's: `-fPIC`, hardening (`-fstack-protector`, `-D_FORTIFY_SOURCE`), `-g0`,
  `-Wall`, `-Wl,-z,relro,-z,now`, `-fuse-ld=lld` (rules_cc finds the image's lld), archives `rcsD`.
  buildlog states `-O3` (opt) or `-O0 -g` (debug).
- Bazel housekeeping the normalizer now excludes: `SolibSymlink` (Linux), `CppModuleMap` (Clang).
- Not verified (as on macOS): remote execution and a foreign execution platform.
