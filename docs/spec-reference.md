<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Spec reference

The vocabulary of a buildlog project, as TOML (or `.facts` and Datalog). A
spec is a set of *facts*: rows of relations that the core and the loaded
packages declare. This page lists the forms a spec can use and the relations
the core and `buildlog-cpp` define for the common cases. For how to run a
build see [user-guide.md](user-guide.md). For writing new relations and rules
see [rule-authoring.md](rule-authoring.md).

The authoritative definitions are the layer files: `src/buildlog/layers/core/`
(`L0_actions.dl`, `L1_targets.dl`, `L1_options.dl`, `L1_configure_file.dl`,
`L2_toolchains.dl`, `L3_native.dl`, `L3_discovery.dl`, `L3_install.dl`, and the
`V_*.dl` authoring vocabulary) and `plugins/buildlog-cpp/src/buildlog_cpp/layers/`.
A relation's declaration (`.decl`) gives its columns, and the comments above it
give its meaning. Which facts an author may state and which belong to the user
is declared in each manifest (`accepts` and `user`).

## File forms

The project's entry point is `buildlog.toml` (or `[tool.buildlog]` in
`pyproject.toml`); see [the user guide](user-guide.md#project-files). It
lists the files that supply facts, and nothing else is read.

Project facts come only from files named by `spec`. `.facts` files are TSV rows for the relation named by the filename. TOML is schema-driven and vocabulary-neutral:

- `rel = [[...], ...]` and `[[rel]]` named-column tables are unchanged.
- Unary relations may use `rel = ["a", "b"]`.
- `[R.x]` states `R("x")`; keys inside the table state relations whose first column is the subject. Map keys fill the next column after the subject, so with the current `dep(t, d, vis)` signature `[target.hello] dep = { zlib = "private" }` states `dep("hello", "zlib", "private")`.
- Ordered lists fill exactly one unfilled `number`/`unsigned` column with a 0-based rank (nested lists may fill two).

TOML syntax, arity and type errors include `file:line:column`/`file:line`, the relation signature and suggestions where possible. The loader deliberately does not know build vocabulary; higher-level authoring forms are ordinary relations and rules.

## Conditions

**Condition syntax** (`src/buildlog/conditions.py`). A spec may write any `Cond`
column, and a condition name in a `cond` column, as infix text: `LOUD & native.opt != debug`,
`!HAVE_UNISTD_H | (a == b)`, `defined(T)`. `&` and `|` chain n-ary, `!` negates, `k == v`
(or `k = v`) is `$Eq`, `k >= v` and `k > v` (and `<=`, `<`) compare versions (below), `defined(T)` is `$Defined(T)`, and a bare name is `$Named(name)`: the
named condition `condition(name, …)`. Every declared option `X` is the condition `X` (value
`on`), and every configure check `X` is the condition `cc.check.X` (answer `yes`). An inline
expression in a `cond` column becomes an anonymous condition whose name is its text, so `why`
cites the spec row it came from. An unknown name or a condition that refers to itself is an
error. `$`-prefixed ADT text is accepted unchanged. In spec strings, `$${` is a literal `${`.
A multi-line string is one value (check programs and configured-file text split it into lines).
**The toolchain family** (buildlog-cpp). `cc.family` and `cxx.family` are configuration attributes
whose value is the family (`clang`, `gcc`) of the C and the C++ toolchain the configuration selects:
CMake's `$<C_COMPILER_ID:…>` and `$<CXX_COMPILER_ID:…>`. Apple's clang is `clang`. A property row
tests it like any attribute: `when."cxx.family == clang".private."cc.compile_flag" = "-Wextra-semi"`,
or a named condition `$Eq(cxx.family, gcc)`. The family comes from the toolchain selection, which is
a function of the configuration, the user's pins and the inventory, so a condition may test it
wherever conditions are read (`prop_when`, `when.`, `dep_when`, `install_when`) except in `request_when` and
`kind_when`, which decide what is built and accept only options: testing the family there is an error. The
attribute is discovered, as a check's answer is: a configuration without a toolchain for the language has no
value, and core reports a condition that tests it there. `tests/test_toolchain_family.py` covers it; the ggml study uses it for clang's `-Wunreachable-code-*`.

**Version comparison.** `k >= v` and `k > v` compare a version-valued attribute with a version, and `k <= v` and `k < v`
are their negations (`$Ge(k, v)`, `$Gt(k, v)`, `$Not` of them): `cxx.version >= 13`, `cc.version < 9.1`,
`cxx.family == gcc & cxx.version > 7.0`, `package.zlib.version >= 1.2.12`. The attributes are `cc.version` and
`cxx.version` (buildlog-cpp: the version of the C and the C++ toolchain the configuration selects, the `version` of
`toolchain(tc, family, version)`) and `package.<name>.version` (core: the version a provider reported for the package,
`package_version`, in a configuration whose platform the package is bound for; `<name>` is the package's own name, as
`package_of` gives it). A condition does not search for a package: it must be wanted (a dependency or `package_want`).
A version is dotted numbers, read as follows:

- the digits before each `.` are one component, and components compare as numbers (`9 < 10`, `1.10 > 1.9`);
- a missing component is 0, so `13`, `13.0` and `13.0.0` are equal;
- reading stops at the first character that is neither a digit nor a `.`: a suffix is ignored, so `13.0.0-rc1`,
  `1.2.3rc1` and `13.0.0+git` compare as `13.0.0` (a pre-release is not below its release);
- only the first 8 components count. A version that does not start with a digit, or a component of more than 9 digits,
  is an error where a comparison reads it (a toolchain whose `--version` has no number, for instance).

`==` and `!=` stay exact text comparisons (`cxx.version == 13` is false for `13.0.0`); write `>= v & <= v` for numeric
equality. The comparison is plain Datalog (`ver_key` in core's `L1_targets.dl`: a zero-padded sort key built with
regular expressions, no recursion, so it stays monotone), and `buildlog why` follows a comparison to the attribute and
from it to the toolchain or the package's metadata file. The version is an answer read as the family is: a
configuration something is built in that has no value is an error (`version undefined where a condition tests it`; a
misspelled package name is this error), and a comparison of a non-version attribute is `condition compares a non-version
attribute`. It may appear wherever conditions are read (`prop_when`, `when.`, `dep_when`, `install_when`) except in
`request_when` and `kind_when`, which decide what is built and accept only options (W5): there it is an error, and so is
a rule that makes `request`, `request_all` or `native_kind` read a version attribute. `tests/test_version_conditions.py`
covers it; the flatbuffers study states its GCC and clang warning guards with it.

A key in `[R.x]` whose relation doesn't take `x`'s kind of subject is warned about: it was
likely meant as a top-level relation but captured by the table header above it.

## Authoring vocabulary

**Authoring vocabulary** (`layers/core/V_vocabulary.dl`, `L1_options.dl`). Core
provides relations that desugar to the lower-level ones, and the loader reads them from per-subject
tables. In `[target.X]`, write `kind`, `sources` (a plugin claims each by suffix; an unclaimed
suffix is an error), `deps = { private = [...] }`, `private`/`public`/`interface` property maps
(`{ "cc.define" = ["A=1"], "cxx.std" = "17" }`), `when."COND".private.K = V` (nested maps),
`test = true`, `request_when = "COND"`, `kind_when = { module_library = "COND" }` and
`install_when = { "COND" = [files] }`. A key `k` in
`[R.x]` means `R_k` when that relation exists, otherwise `k(x, ...)`. `[defaults.target]` applies to
every `[target.*]` table in the same file, and `profile = "p"` applies `[profile.p]` from any spec
file. Maps accumulate across the table, its profiles and the defaults, while scalars and lists
take the nearest value. `[option.X]` declares an option: `default` (default `off`), `help`,
`type`, `choice = [...]`, `requires = "COND"` and `conflicts = "COND"`.
`buildlog configure -O X=VALUE` sets one, and a reconfigure keeps it, like `-D`. A condition that
decides what is built (`request_when`, `kind_when`) may test only options, and is evaluated once
per build (`option_effective`). Also in core: the path helpers `path_basename`/`dirname`/`stem`/`ext`
(ask with `path_want(p)`), `join_part(group, pos, item)` (a separator-less `joined`) and
`gen_dir(t, c, dir)`. buildlog.toml may hold spec keys itself, and `[presets.NAME]` there is a user
file selected with `--preset NAME`. `[export.P]` takes `version`, `compat`, `description` and
`component`. A relation name set under `[vars]` is warned about.

**Named rows.** A key under `[target.X]` whose value is a map naming the relation's columns
states one row by column name, with the target as the first column; a symbol column left out is
empty (the relation's own default). Any other key in the map states the relation
`<relation>_<key>` for the target. So a relation that takes options reads as one fact:

```toml
[target.reaction_diffusion_init]
halide_build_library = { from = "reaction_diffusion_generator", registration = "run" }
```

is `halide_build_library(x, "reaction_diffusion_generator", "", "", "")` (the empty generator,
function and runtime are CMake's defaults) plus `halide_build_library_registration(x, "run")`.
The raw relation rows stay valid. Plugins use this for their sugar (the halide-build keys are in
`case-studies/halide/packages/README.md`).

## Configurations

**Configurations.** The spec declares every configuration, `config_decl = [["debug", "arm64-macos"]]` with `config_decl_setting = [["debug", "native.opt", "debug"]]`, and requests targets in one (`request`) or all of them (`request_all = [["app"]]`). `--config` selects which are built (a user file's `config_default = [["release"]]` is the default); an unselected configuration's requests derive nothing, so it triggers no discovery and no probe. A setting's exec policy is `setting_exec_value` (the exec configuration uses a fixed value, e.g. tools build `release`) unless the spec says `exec_setting = [["native.sanitize", "inherit"]]` (tools take the configuration's own value, as in a single-tree CMake build). **Toolchain pins** are scoped by (language, exec platform, target platform): `toolchain_pin = [["cxx", "arm64-macos", "x86_64-macos", "apple-clang"]]`; the unscoped form is an error. **Discovery is per platform**: a name is wanted, found, selected and relayed per platform it is used on (a tool a command runs on its exec platform, a library on its consumer's), with `package_prefix_on`, `package_search_dir_on`, `package_file_on` and `package_pin_on` for platform-specific locations and pins; natively the platforms coincide and there is one binding (`tests/test_variants.py`, `tests/fixtures/per-platform`).

## Targets and native code

A target has a kind (`native_kind`: `executable`, `library`, a module or an
object library), sources (`cxx_source`, `c_source`, generated sources),
dependencies with visibility (`dep`, `dep_when`) and properties that propagate
along them by visibility (`prop`, `prop_when`). The default for a property key
is declared by the layer that owns it: a key is a set (union) or a lattice
(join).

**Linkage is a derived choice per library and configuration**. A target of kind `library` is built static or shared as `library_linkage_by(t, c, l, level)` decides, highest level first: the author's requirement (`[target.t] linkage = { require = "static", why = "…" }`, which is `linkage_require(t, l, why)`), a dependent's pin on its edge (`dep_linkage(t, d, l, why)`, CMake's `find_package(… static)`, Meson's `dependency(…, static: true)`), the user's per-library `library_linkage(t, l)`, the configuration's setting `native.linkage` (`default`, `static` or `shared`: the user's global default, CMake's `BUILD_SHARED_LIBS`, Meson's `default_library`), the author's `default_linkage(t, l)`, and otherwise static. The two constraints never silently override the user. A user choice that a requirement or a pin contradicts is an error that names both sources, the constraint's reason and the fix (accept it with `library_linkage`, or drop one). Authors are meant to leave linkage to the user, so a constraint without a reason is a load error, and the reason shows in errors and in `why`. **Every built library is kind `library`**: the old fixed kinds `static_library` and `shared_library` are a load error whose message gives the replacement (`kind = "library"`, plus `linkage = { require = …, why = … }` only if the library cannot be built the other way); an imported library's file keeps its nature (`imported_kind`), which is a fact, not a choice. `native_kind_in(t, c, k)` is every target's kind per configuration, and the rules that build artifacts read it. A property only one linkage has, such as an export header's `STATIC_DEFINE`, is `linkage_prop(t, l, key, value, vis)`. Objects are position-independent when the library is shared; the user's `cc_pic(t)` (a target or `"*"`, buildlog-cpp, Meson's `b_staticpic`) makes them so whatever the kind. Pins are read from declarations, because the needed closure depends on the choice. Two different pins on one library are therefore an error for the whole build, not just for one link closure. A pin on a target that is not a library (a module, an executable) is an error. A pin on an imported target chooses the package's linkage when the user states no `package_linkage`, and is an error against one that differs. **Both linkages in one configuration**: a linkage choice may be `both` (`default_linkage`, `library_linkage` or `linkage_require`; there is no global `both`). The library then has two build instances: the static one keeps the id `t`, the shared one is `t@shared` (`linkage_twin`, `inst_linkage(i, c, l)`), each with its own objects (`obj/<cfg>/t@shared/…`, PIC) and the library's name (`linkage_output_name(t, l, name)` renames one, e.g. SPIRV-Tools' `SPIRV-Tools-shared`). An edge to such a library goes to the instance the dependent pins (`dep_linkage`), else the one matching the dependent's own linkage (a static library links the static one, as Meson's `default_both_libraries = auto`), else it is an `ambiguous linkage` error naming the fix. Requesting `t` builds both. An exported component of such a library names its linkage (`export_component_linkage(P, comp, l)`), so one package can install `libz-ng.a` and `libz-ng.dylib`. zlib-ng and SPIRV-Tools build both from one target (`tests/test_linkage_both.py`). Both flavours can also be two configurations, e.g. `release` and `release-static` with `native.linkage = "static"`. `native.linkage` at `default` is left out of configuration names (`setting_name_elide`), so builds that never set it keep their names; the other values append `.static` or `.shared`. `tests/test_linkage.py` has the precedence, `why` and error cases.

**Export lists per object-file format.** A shared library states which symbols it exports in the format its platform's linker takes, as CMake's `target_export_script(t APPLE_LD … GNU_LD …)` does: `native_exported_symbols(t, file)` is ld64's exported-symbols list (spelled `-Xlinker -exported_symbols_list`), `native_version_script(t, file)` an ELF version script (`-Wl,--version-script=`, buildlog-cpp's GCC and Clang families on Linux). Each applies on the platforms whose driver spells it and is ignored elsewhere, so one spec can state both; it is an error when a library states lists and none applies to a configuration (`export list but no spelling for it`) unless the author states `native_export_list_optional(t, why)`, for a library whose upstream states a list on some platforms only (zlib's version script is CMake's `UNIX AND NOT APPLE`). The file is an implicit input of the link.

**Run-time dependencies and file names**. `runtime_dep(t, m, place)` (or `runtime_dep_when` with a condition) says that t needs m at run time but does not link it, for example a dlopen'd module or a plugin. m is built in t's configuration, not the exec configuration of a tool's `load`. Nothing propagates along the edge. t's artifact has m as an order-only input. m then sits where the loader looks. With `beside_executables`, every executable that is or links t gets a symlink `bin/<cfg>/<file> -> ../../lib/<cfg>/<file>` as an order-only input, and so `buildlog test` sees it. With `beside_loader`, m sits in t's own directory. It is an error for m also to be linked, for m to have no file to load, and for a place to be unknown. In a spec, state them as the relations are named, flat (`runtime_dep = [["loader", "plug", "beside_executables"]]`) or in the loader's table, where the subject is implied: `[target.loader] runtime_dep = { plug = "beside_executables" }`, `runtime_dep_optional = [["backend", "beside_loader"]]`, `runtime_dep_when = [["plug", "beside_loader", "native.opt == release"]]` (the condition is a name or an infix expression, as in `dep_when`). There is no second spelling: strength is the relation's name, a wrong column count, an unknown place or an unknown key such as `runtime_deps` is a load error, and `tests/test_runtime_dep.py` checks that the table form derives the same build as Datalog facts. `runtime_dep` is **required**: t cannot work without m. `runtime_dep_optional(t, m, place)` and `runtime_dep_optional_when` state the same placement for a module t only uses if it is there (a backend it checks for): built and placed the same way, but never required. Strength changes what install and export do, never the build tree. Installing a loader installs each required module it reaches (through required edges only, transitively), per its place: `beside_loader` into the loader's install directory, `beside_executables` into the install directory of each installed executable that is or links the loader (one file for executables in one directory). With no installed executable the module goes beside the loader. A required module that would install to two different directories is an error (`required run-time module installs to two directories`). An optional module installs only if the author lists it with `install_target`, and what it requires follows it then. The `buildlog` export format states a required module as a `module` component (`pkg_component`, `pkg_location`) and `pkg_runtime_dep(comp, module, place)`; a consumer that links the imported loader gets the module placed beside its executables, as for a built loader. An installed optional module is exported as an available module component with no requirement. CMake, CPS and pkg-config cannot state a requirement, so each active format warns (`export format cannot state a required run-time module`, `export_runtime_lossy`) until `export_format_omit` or `export_format_skip` drops it. A module that is exported must be a `module_library`. `native_file_name(t, c, f)` is an artifact's file name per configuration, prefix and suffix included (a Python extension's `<module>.abi3.so`), which the link writes. It applies to executables, archives and modules. A plugin derives it, together with the declaration-level `native_file_named(t)`. `tests/test_runtime_dep.py` covers both. The ggml case study's loader uses `runtime_dep_when`, and buildlog-python names extension modules with `native_file_name`.

**Target templates** (variants that coexist in one build). `[target_template.b]` states rows the way a target does: kind, sources, deps, props, runtime deps, version, linkage and output name. `param = [...]` names its parameters. `instance_of(t, b)` makes t a target with those rows, and `instance_param(t, k, v)` expands the placeholders `{k}` in their values. `{name}` expands to t. A parameter with several values gives several rows, and a row whose parameter has no value is dropped. An instance may belong to several templates and may add rows of its own. Instances come from TOML (`[target.t] instance_of = "b"`) or from project rules. The errors are an undeclared template, a template that is also a target, an unknown or unclosed placeholder, and an instance parameter of none of the instance's templates. Parameters may not depend on the needed closure, because they decide edges and sources. A property row may use a value that does (the expansion of properties is kept separate for this), but a parameter may not. `tests/test_templates.py` covers templates, and the ggml case study's per-ISA CPU backends use them.

**Source groups** (buildlog-cpp). `source_group(t, g)` names a set of t's sources (`group_source(t, g, src)`; the source is also a `c_source`/`cxx_source` of t, as usual) with its own condition, `group_when(t, g, cond)`, and its own compile properties, `group_prop(t, g, key, value)` and `group_prop_when(…, cond)`. The keys are `cc.define`, `cc.compile_flag` and `cxx.compile_flag`, and they are spelled after t's own. A grouped source compiles where one of its groups applies, as t's object (`out:obj/<cfg>/<t>/<src>.o`), with t's properties plus those of each group that applies. Nothing is copied into another target. Groups may overlap. A group of one source is a per-source property: CMake's `set_source_files_properties` and its `if()`-appended sources. `why src_on(t, c, src)` names the group and its condition. The errors are an unknown target or group, a group source that is not a source of t, two conditions, an unknown condition, and a key that is not a per-object compile key (`tests/test_source_groups.py`). The zlib-ng case study's ISA and fallback source sets use groups.

**ISA sets** (buildlog-cpp). What each architecture has is L2's knowledge, `isa_ext(cpu, e)` and `isa_baseline(cpu, e)` (a platform plugin adds its own), and how a family spells an extension is its F layer's: `family_isa_flag` (x86 `-m<ext>`), and for AArch64's `-march=<base>+<sfx>+…` the suffix ranks, `+no<sfx>` and the base versions. A project names ISA sets: `isa_requires(s, e)`, `isa_requires_when(s, e, cond)` (the flag only where cond holds) and `isa_excludes(s, e)`. It attaches a set to a source group, `isa_group(t, g, s)`, or to a whole target, `isa_target(t, s)`, whose objects then compile with the set's flags for the configuration's C toolchain family. buildlog-cpp derives `isa_compatible(s, cpu)` for the project's gates. It reports an unknown extension, extensions no architecture has together, an extension the family cannot spell, and, per configuration, a set's TU compiled where the cpu lacks its extensions. The zlib-ng study uses groups and the ggml study uses targets; clang is the only family spelled.

**Include directories and warnings** (buildlog-cpp). A C-family compile searches three classes, in order: the target's own `cc.include_dir` values as `-I`; every dependency's, direct and transitive, in-project and imported alike, as `-isystem` (a dependency's warnings fire when it builds its own sources, not again in every consumer); then `cc.system_include_dir` values (e.g. imported dirs under the user's `package_system_includes`) as `-isystem`. A dir reachable in two classes is emitted once, in the earlier one. Within a class the dirs are a set in lexical order, except where an author states `cc_include_before(x, y)` (TOML `cc_include_before = [["out:gen/…", "src:"]]` or a project rule) to settle a header collision: x precedes y wherever both are in one class; ties stay lexical; a cycle is an error; the fact appears in `buildlog why --flag "-isystem <dir>"`. `-Wshadow-header` (with `-Wno-error=shadow-header`) is added where a probe finds the toolchain accepts it (upstream clang 22; not Apple clang 21 or GCC 16). Turning warnings into errors is a user decision: `cc_werror = [["release", "*"]]` (configuration or `*`, target or `*`) in a `--user` file; `*` covers the configurations your requests reach, not the exec configurations where build-only tools are built, which opt in with `cc_werror_exec = [["*"]]` (a target id or `*`); an author's `-Werror` or `-Werror=…` (a compile-flag property or an argument a rule writes) is an error on actions whose compiler the user chooses (toolchain selection, not an author `toolchain_pin`) unless `buildlog.toml` has `[author_decides] cc_werror = "reason"`; an imported tool is the user's choice too (the user's package selection binds it); where the author's facts fix the tool (a tool the project builds, a literal program path the author writes, an imported tool tied by `import_same_package(tool, library)` to the library it must match and checked against the user's selection, e.g. Halide's runtime `Clang::clang` and the LLVM libHalide links, or an author-pinned toolchain) it is allowed, and `why --flag -Werror` shows that basis (`action_arg_basis`). A platform tool (lipo) is the user's choice. A configured file (`configure_file`) is owned by a target or by an owner several targets share (`owner_member(owner, target)`, e.g. a library's variants): one file per configuration, whose dir is each member's own `-I`.

## Exec configuration and tools

**Exec configuration is inferred from use.** Nothing says a target is a host tool. A command names the program it runs, and any tool it executes or plugin it loads, by target reference: `action_run(a, rank, prefix, ref, use)` with `use` `run` or `load`, never a path. Core derives `tool_dep(t, c, g)` (the owner `t` of `a` in `c` runs `g`), so `g` is needed in `c`'s exec configuration, and L2 picks the toolchain that runs on the exec platform and targets it, with no pin. The argv token and an implicit input come from `run_artifact(g, exec, path)` (L3: an executable, a shared library, an imported executable or module). `tool_dep` is derived, not open. Several references in one argv word (a comma-separated plugin list, `-p a.so,b.so`) are `action_run_list(a, rank, prefix, sep)` plus `action_run_item(a, rank, i, ref, use)`; L0 spells the word from typed parts (`action_arg_join`/`action_arg_part`). `buildlog why --flag <compiler> --output <tool>` shows the chain (`action_run` ⇒ `tool_dep` ⇒ `needed(g, exec)` ⇒ `tc_select`); `tests/fixtures/exec-demo` is a cross build with two unpinned toy toolchains. **Standard streams** are typed: `action_stdin(a, path)` (an input) and `action_stdout(a, path)` (one of the action's outputs), at most one each; the emitter appends `< in > out` to the quoted argv. So are the **working directory** (`action_cwd(a, dir)`: the emitter rebases the command's rooted paths onto `dir` and spells `cd dir && …`), **restat** (`action_restat(a)`, Ninja's `restat = 1`) and **portable file tasks** (`action_builtin(a, name)`: `buildlog -E copy_if_different|copy|create_symlink|make_directory|remove|touch|configure_file`, in the spirit of `cmake -E`). buildlog-cpp's open `cxx_generated_source(t, c, path)` compiles an `out:` file an action generates into `t`, like a `cxx_source`; `c_source(t, src)` compiles a C source with the toolchain's C compiler and the C-family keys only (`cc.*`, not `cxx.std` or the C++-only `cxx.compile_flag`), and `cc.pic=on` makes a target's objects position-independent whatever its kind. **The configuration's standard flags are per target** (core L3): `native.link_config_flags=on` puts the compile-phase flags of `native.opt` (release's `-O3 -DNDEBUG`) on the target's link line, as CMake does when the compiler driver links (off by default; Meson passes none), and `native.wipe_config_flag=<regex>` removes the flags it matches from the target's compiles and links, CMake's `wipe_standard_flags` (interface visibility covers a directory; `config_flag_wiped(t, c, arg)`). The Halide case study sets both. The Halide case study builds binary2cpp and build_halide_h this way, with 0 exec annotations, and runs the imported `Clang::clang`, `LLVM::llvm-as` and `flatbuffers::flatc` the same way; libHalide's graph takes nothing from the CMake build tree (`case-studies/halide/README.md`). **The exec inference check** covers the other direction. An action whose argv[0] is a run artifact this build makes for a platform the build machine does not run fails, with an error naming the configuration and the build machine. A universal binary that includes the build machine's platform counts as runnable. A reference is correct by construction, but a rule that spells a tool's path in the product's configuration (Bazel's tool in `srcs`, not `tools`) is not, and Rosetta would hide it. Under `--keep-going` the rest of the graph is emitted (`tests/test_exec_check.py`; the bazel-flatc case study's annotation experiment).

**A toolchain the build makes.** An installed toolchain's facts (`toolchain`, `toolchain_tool`, `toolchain_runs_on`, `toolchain_can_target`, `toolchain_supports`) come from buildlog-cpp's inventory probe, which runs the compiler. A toolchain built in the same graph states the same facts from its author instead, usually a rule package shipped with its sources, and names the target that plays each role in place of a path: `toolchain_tool_target(tc, role, t)` (open, core L2). Nothing runs it at configure time, so its capabilities are the author's facts, and no capability or warning probe is asked of it. `toolchain_tool_arg(tc, role, w)` gives the words that role's program takes first (TCC's archiver is `tcc -ar`). `toolchain_runtime(tc, r)` names a library that every executable, shared or module library built with tc links privately, in its own configuration (TCC's `libtcc1.a`). A built toolchain is never a candidate of the selection, so adding one never changes or makes ambiguous what the rest of the build compiles with. A target chooses one with `[target.t] toolchain = "tc"` (`target_toolchain(t, tc)`), and the choice covers every language tc targets. Lowering reads `tc_of(t, lang, c, tc)`: the target's own toolchain if it names one, else the configuration's `tc_for`. The role's program in argv[0] is the tool target's run artifact in the exec configuration, and it is an implicit input of the action. The target has a `tool_dep` on it, so the compiler is built for the build machine and passes the exec inference check like any tool. After that binding, a built toolchain is consumed exactly as an installed one: its family's setting spellings (`family_setting_arg`), its platform arguments (`tc_platform_arg`) and the rest. Errors: a `target_toolchain` that names an installed toolchain (choosing an installed toolchain is the user's `toolchain_pin`) or an undeclared one; a built toolchain that cannot run on the exec platform or target the configuration's platform or a language the target compiles; a role named twice; a built toolchain with no `toolchain` fact. `buildlog why --flag <compiler artifact> --output <object>` shows the choice (`action_tc_tool` ⇐ `tc_of` ⇐ `target_toolchain`, and the toolchain's stated facts). The TinyCC bootstrap (`case-studies/tcc`, `tests/test_tcc_bootstrap.py`) builds tcc with the host clang, then with itself twice, in one graph.

## C++ modules

buildlog-cpp builds C++20 **named modules** (no header units; installed or exported modules are deferred). Authors declare which sources are interface units; module names come from scanning, never from the spec.

- `cxx_module_interface(t, src, vis)`: `src` of target `t` is an interface or partition unit, `vis` `public` (targets that depend on `t` may import it) or `private` (only `t`). `cxx_generated_module_interface(t, c, path, vis)` is the same for an `out:` file an action generates (it must be a `cxx_generated_source` too). `cxx_import_std(t)`: `t` imports `std` (libc++'s module source, found by the `cxx_std_module` probe; `std.compat` is deferred). Targets of a configuration that has any of these need `cxx.std` 20 or later.
- **Hybrid graph.** Configure derives the static structure: the declared units, a scan action per C++ object (`clang-scan-deps -format=p1689`; GCC: `-fdeps-format=p1689r5`), a collate action per target, a BMI action per interface unit and compatibility class, the BMI paths and which interfaces each target may see. What a source imports is learned at build time: the **collator** (`buildlog -E collate`, the `modules` stratum in `plugins/buildlog-cpp/src/buildlog_cpp/strata/modules.dl`, run by patched Soufflé) reads the P1689 results and writes a Ninja dyndep file (`modules/<config>/<target>.dd`), module maps and a provenance file. Scan results never re-enter the configure model: the loader rejects a layer that names a stratum relation. Module-aware compiles take header dependencies from their scan (the compiler's own depfile names imported BMIs, which makes Ninja report false cycles when an import flips direction).
- **Compatibility classes.** One BMI per (provider, class); a class is the toolchain, `cxx.std`, ABI switches and the BMI-relevant flags of a hand-written per-family table (`family_bmi_flag`, F_clang/F_gcc). A consumer whose flags differ gets its own BMI. Sanitizer, `-O` and `-D` are not in the class key yet.
- **Errors, never tie-breaks**, each naming the units: a module name provided twice, an import cycle, an import of a `private` interface from another target, an import from a target that is not a dependency (an import never adds a link edge: add the `dep`), an import no declared unit provides, a declared unit that provides no module, a module-aware source that is not declared. Modules with Apple clang are a configure error (no scanner); on macOS use Homebrew `llvm@22` (`BUILDLOG_LLVM_BIN`). The collate action needs `BUILDLOG_SOUFFLE`.
- **Core.** `action_dyndep(a, file)` (`dyndep =` on the edge; Ninja ≥ 1.11 is required only by builds that use it), the `[[stratum]]` manifest key and the `collate` builtin (`buildlog -E collate`), and in `why --output <file>` a "Dynamic edges, from the build-time collation" section (the provenance file's `edges`, each with the import or BMI chain that caused it).
- **Toolchains tested.** Homebrew `llvm@22` (macOS), clang 22 (Linux), GCC 14 (Linux; no `import std`: libstdc++ ships the module source from GCC 15). Tests: `tests/test_cxx_modules.py` (partitions, cross-target visibility, generated interface, cycle and collision errors, flag-incompatible consumer, import-direction flip, `import std`). Design and limits: `docs/spec-reference.md`.

## Generators

**Generators** (`layers/core/V_generators.dl`). `[generator.G]` runs a program that
writes files. `owner = "T"` names the target whose action it is (outputs go in
`out:gen/<config>/T/` and become T's generated headers). `run = [...]` is the argv: a word is
literal text or a prefix plus one placeholder: `${out:F}` (an output), `${gen}`/`${gen:SUB}` (the
directory), `${src:P}` (a source, an input), `${pkg:T/REL}` or `${pkg:KEY}` (a file of the
imported target T, or `package_data` KEY's, an input), `${tool:T}` (another program) and
`${list:L}` (`word_list` L's words, spliced). The first word is the program, a target reference.
`output`, `input`, `stdout`, `stdin` and `label` complete it. A generator with
`params = ["a", "b"]` is a template whose text holds `{a}` and `{b}`, and
`generate = [["tpl", "x", "y"], ...]` instantiates it. `why` on an argument cites the
template line and the instance row. `[target.T] gen_include = { private = ["", "sub"] }` adds T's
generated directory to its include path, and `gen_user = "O"` compiles T against owner O's
generated files as its own. flatbuffers' flatc step and SPIRV-Tools' Python generators use this.

## Configure checks

**Configure checks** (zlib case study; `plugins/buildlog-cpp/.../L4_checks.dl`). A spec asks
questions of each configuration's C-family toolchain in a small fixed set of kinds:
`include`, `type` (existence and size), `function`, `compiles` and `runs` (it builds, then the program runs on this machine and must exit 0, CMake's `check_cxx_source_runs`: what the CPU can do, not what the compiler believes; a target this machine does not run answers `no`, and the CPU is part of the cache key). Each question is
`[cc_check.NAME]` with `lang`, `kind` and `subject`, plus an optional prelude
(`cc_check_headers = "a.h b.h"`) and optional extra arguments (`cc_check_args`). Arguments one toolchain family rejects are stated per family, `cc_check_args_family = [["HAVE_X", "gcc", ""]]` (check, family, arguments): the family's row replaces `cc_check_args` for configurations whose toolchain is of that family (CMake's `CMAKE_REQUIRED_FLAGS` chosen by `CMAKE_C_COMPILER_ID`). Compact
tables state the common checks one row each, in the language `cc_check_lang` names (`c` unless
stated): `[cc_check_include] HAVE_UNISTD_H = "unistd.h"`, `cc_check_function`, `cc_check_type`,
`cc_check_symbol` (name, symbol, headers; CMake's `check_symbol_exists` program for a function
or variable), `cc_check_flag` (a trivial program built with the given arguments) and
`cc_check_compiles` (a program). They desugar to `cc_check` rows, and `why` cites the compact
row. The answer is
a discovered fact: the `cc_check` probe builds one small program. It is cached by the
question's content and the toolchain's identity (tools, linker, triple, SDK, environment),
never by the check's name or the configuration. Conditions read the answer as the
configuration attribute `cc.check.NAME` (`yes` or `no`), and the check's name is also a
condition that holds where the answer is yes (`!cc.check.HAVE_FSEEKO`), and `why` on a definition such a condition adds leads to the
probe's result. A user file may state an answer instead (`cc_check_known`, which has the same
columns as the result). That question is then not probed, and the rules that read the answer
do not change. Core's `attr_discovered` marks attributes like these: each one is checked for
totality only where a condition tests it.

## Configured files

**Configured files** (zlib case study; core `L1_configure_file.dl`). `configure_file(t, file)`
generates `out:gen/<cfg>/<t>/<file>` (`configured_file(t, c, file, path)`) from a template,
CMake's `configure_file` in the dialect projects use: `@VAR@`, `${VAR}` unless
`configure_file_at_only(t, file)`, and `#cmakedefine VAR rest`. The template is ordered pieces:
`configure_file_lines(t, file, i, src, first, last)` (lines of a source file, `last` 0 = to the
end) and `configure_file_text(t, file, i, line)`. The variables are `t`'s own properties,
`configure.define` ("NAME=value") and `configure.undefine` ("NAME"), so conditions can set them
and `why --flag -DNAME=value --output <file>` explains each one. A variable the template uses
but nobody states is an error (CMake substitutes ""), and so is a `#cmakedefine` variable
defined to a value CMake reads as false: state it undefined instead. The action is
`buildlog -E configure_file` and is restat. buildlog-cpp's `cc_configured_header(t, file)`
makes the file's directory an interface include dir of `t` and the file a generated header
its users' compiles wait for. `cc_check_configure(t, check)` makes a configure check's
answer the variable `check` (1, or undefined).

## CUDA

**CUDA** (`buildlog-cuda`, stdgpu case study; `plugins/buildlog-cuda/README.md`). A `.cu` source (or `cuda_source(t, path)`) compiles with nvcc; the toolkit is one probed package (`CUDAToolkit_ROOT`, `CUDA_PATH`, `nvcc` on `PATH`, `/usr/local/cuda`) that also lists the GPU codes it supports (`nvcc --list-gpu-code`). **Host compiler:** `-ccbin` the configuration's C++ toolchain if one is uniquely determined, else `cuda_host_pin(config|"*", toolchain)` (a user fact; an author needs `[author_decides] cuda_host_pin`); several candidates and no pin is an error, never a tie-break, and the toolkit refusing the host (a `cuda_host_check` probe runs nvcc on it) is an error showing the toolkit's own message. There is no override. **Architectures:** `gpu_arch_set_code(set, code)` names sets, the configuration setting `cuda.arch` picks one (it is in the configuration's name), `cuda_arch_of(t, set)` gives a target its own; a code the toolkit lacks is an error, and there is no `native`. **Device link:** `cuda_rdc(t)` compiles with `-rdc=true`; `nvcc -dlink` runs once per executable, shared library or module over the RDC objects that reach it through static libraries, stopping at shared libraries; `cuda_resolve_device_symbols(lib)` states that a static library resolves device symbols; a link that drops an architecture its objects carry is an error unless `cuda_dlink_drops(t, code)` states it. **Runtime:** `cuda::cudart_static` (default), `cuda::cudart` or none, `cuda_runtime(t, static|shared|none)`; mixing in one link is an error. The toolkit's other libraries, `cuda::thrust` and `cuda::toolkit` are imported packages. GPU runs are not part of the study yet: the run test skips with that reason. **Composition:** a setting key's position is a priority; keys are ordered by (priority, name), so `cuda.arch` composes with other plugins' settings of the same priority (`tests/test_cuda.py`); nvcc's include directories use buildlog-cpp's include relations (classes, `cc_include_before`, implicit-directory filtering).

## Tests

**Tests as facts.** `test(name, program)` runs a target's run artifact after the build. A test requests its program: the program is built (and in Ninja's default set) in every active configuration, so a test never silently vanishes because nothing else asked for its program; `test_when(name, cond)` narrows a test to the configurations where a named condition holds (an option-gated test suite, like `dep_when`), and a test runs only where it applies. Its settings are typed facts, not a shell line: `test_arg(name, i, literal)`, `test_arg_ref(name, i, target)` (a file argument, CMake's `$<TARGET_FILE:…>`: resolved in the test's own configuration and needed there), `test_env(name, k, v)`, `test_label`, `test_regex(name, pass|fail|skip, re)` and `test_serial(name)`; a layer may add `test_config_env(name, c, k, v)` per configuration. L0's `test_case*` relations are what the driver writes to `<builddir>/souffle/tests.json` (none: no file). Each case runs in its own directory, `test/<config>/<name>` under the build directory. `test_cwd(name, dir)` (ctest's WORKING_DIRECTORY, a rooted `src:` or `out:` path, at most one) replaces it. A cross configuration's cases are listed as "not run" with the reason, as are a broken program's. There is one exception: when the user states `platform_runs_on(p, host, via)` in a `--user` file (for example `[["x86_64-macos", "arm64-macos", ""]]` for Rosetta, or a qemu-user command as `via`), p's cases run on host, with via's words before argv (tests.json's `launcher`). The fact is never inferred, and build actions do not read it. `buildlog test` applies ctest's rules: a skip match is skipped; a fail match fails; pass regexes, if any, decide regardless of the exit status; otherwise exit 0 passes; a timeout fails. Serial cases run alone, after the others. WILL_FAIL and fixtures are not modeled.

## Install and export

**Installed libraries and their compatibility**. A library in an `export_component` installs and exports its artifact in the configuration's linkage. For a static library that is `lib<o>.a`. For a shared library it is the versioned file, plus its install-name link and its `-l` link, installed as links. The artifact goes under `native_artifact_dir` (buildlog-cpp: `libdir`). CPS states the component's `type` (`archive` or `dylib`) and `location`, CMake gives `STATIC`/`SHARED IMPORTED` with `IMPORTED_LOCATION` and `IMPORTED_SONAME`, and pkg-config writes `Libs`. The CMake version file checks the pointer size. Executables, modules and a static library's private (link-only) requirements are not exported yet. The package's compatibility claim defaults to its libraries' ABI claim, `native_abi_compat(t, policy)`, derived from `native_version(t, version, soversion)`: a soversion that is the major gives `same_major`, major.minor gives `same_minor`, and the whole version gives `exact`. A 0.y version with soversion 0 gives `same_minor` with a warning, and a shared library with no version gives `exact` with a warning. The Mach-O compatibility version is the soversion padded to X.Y.Z, the lower bound of the same claim. A stated `export_compat` looser than the derived claim is an error unless `buildlog.toml` has `[author_decides] export_compat = "reason"`, which `export_compat_looser` records. `tests/test_export_native.py` covers these rules, and the zlib case study runs them against CMake's install.

**Installing built targets that are not exported.** `install_target(t, dir)` is CMake's `install(TARGETS t DESTINATION dir)` for a target outside any `export_component`: `dir` is a layout directory (`libdir`, `bindir`, ...), and the files derive from the target and each active configuration, the same ones a component installs: a shared library's versioned file plus its links, a static library's archive, a module (`module_library`) or an executable by its file name. A rule may derive `install_target` (the relation is open; ggml installs its CPU modules to `bindir` that way). `install_configured(t, file, dir)` installs t's `configure_file` output from each configuration and maps its generated directory to `dir`, so an exported include directory finds it too (`install_configured = { "zconf.h" = "includedir" }`). Several active configurations each install their own files, as `install_file` does; CMake installs the one it configured. Two different built files meeting at one destination (a library built in two configurations) is an error of the `out:install` action at configure time when an install prefix is given: install one configuration (`--config`). A configured file's content is only known at build time, so identical copies pass and differing ones fail at install via the install action's hashes. It is an error to `install_target` an exported component (its package installs it), a nonexistent target, an unknown layout directory or a target with no installable file in a configuration where it is needed, or to `install_configured` a file that is not `configure_file`d. `tests/test_install_target.py` covers these rules.

**The `buildlog` export format** writes an install's facts as Datalog: `share/buildlog/<Pkg>/manifest.toml` and `<Pkg>.facts.dl`, in the `pkg_*` vocabulary (`pkg_package`, `pkg_version`, `pkg_prefix`, `pkg_component`, `pkg_default`, `pkg_location`, `pkg_link_name`, `pkg_soname`, `pkg_requires`, `pkg_depends`, `pkg_runtime_dep`, `pkg_prop`, `pkg_prop_path`, `pkg_prop_abs`, plus the metadata `pkg_compat`, `pkg_description`, `pkg_requires_version`), with prefix-relative paths so the install stays relocatable. It is on by default for every exported package, in the directory `buildlogdir` (default `<datadir>/buildlog`); `export_format_skip = [["Pkg", "buildlog"]]` (a user file, as for every format) turns it off. The rules are a loss in every other format: a package with `export_rules` warns once per active CMake, CPS or pkg-config format (`export format cannot carry shipped rules`, from `export_rules_lossy(pkg, fmt, dir)`), like the per-property `export_lossy`; `export_format_omit` or `export_format_skip` drops the format and the warning. `export_rules = [["Pkg", "src:packages/pkg"]]` names a project-local rule package (its `name` must be `Pkg`) whose manifest, layers and facts are installed with it, so a library can ship the rules that go with its facts; it is an error for an undeclared package or a directory with no `manifest.toml`. A manifest's `imports = [{ format, file[, namespace] }]` lists nearby export files (relative to the manifest, inside the prefix) that the `buildlog` provider hands to the provider of their format; install writes the facts file as one, and a hand-written manifest may list a CMake, CPS or pkg-config export instead. The lock hash covers them. A package that ships its upstream's exports plus facts and rules (LLVM, Qt) may state target facts only about the namespaces its imports own (the loader's ownership check, `docs/rule-authoring.md`); package-level facts reach consumers through `package_version`, `package_attr` and `import_linkage_dep`, which are open (a package writes its own name in the first column), and the core exports `import_target_on(t, platform)` and `import_ns_target(ns, t)` (an imported target of an import with namespace `ns`) for those rules. A downstream project binds it with `package_search_dir = [["buildlog", "<prefix>/share/buildlog"]]` (a prefix search binds it too: the CPS file the install writes names its manifest as `x_buildlog_manifest`, so the two are one candidate; a CPS file without the marker is ambiguous with it until pinned); the package is pinned in `buildlog.lock` and its rules pass every loader check. See [rule-authoring.md](rule-authoring.md#a-library-installed-as-datalog-the-buildlog-format).

## Target ids

**Target ids.** Project targets have bare names. Targets a package defines are always `<q>::name`, where `q` is its `target_namespace`; there are no bare exports (`Halide::Halide`). The spec may not define ids in a loaded package's target namespace, and two sources may not define one id. A `dep`/`dep_when` reference resolves in L1 (`dep_resolves`): a scope's own names first, as lexical sugar (inside package `q`, `util` means `q::util` if `q` defines it; in the project, a bare name the project defines); otherwise the name must have exactly one candidate: an exact target id, or where an explicit `alias(from, to)` chain leads. A bare name never matches some package's `*::name` by suffix (that would be an implicit export). None is `unresolved dependency`, several is `ambiguous dependency` ("qualify it"). `alias` is open and accepted; alias cycles, dangling and ambiguous aliases are errors. Bindings are aliases: Halide depends on `libpng`, and the pkg-config importer that finds `libpng.pc` defines `pc:libpng` and states `alias("libpng", "pc:libpng")`. `$Defined(n)` holds when `n` has at least one candidate, so `$Defined(libpng)` holds exactly when a binding source is loaded. Action ids and outputs derive from target ids, so they do not collide.

## Integrity errors

**L0 integrity** is derived in `L0_actions.dl` as `error` facts: an output with two producers, an input that is neither a source (`src:`), an external absolute path, nor produced (`out:`), an output outside `out:`, action dependency cycles, argv without a program at position 0, a position with several tokens that the producer did not declare `action_arg_set(a, rank)` (an unordered, lexically sorted set), a position used by both a token and a sequence, a `default_output` nobody produces, and two stdin or stdout files, or a stdout file that is not an output. Interior argv position gaps are allowed (ranks are sparse sort keys).

## Options

`[option.X]` declares a project option (see "Authoring vocabulary" above). The
relations `option_decl(name, default)` (author) and `option_value(name, value)`
(user, set with `-O` or a user file) are in `L1_options.dl`. Only the user
states a value, and an option's value is one value for the whole build, like a
CMake cache variable. Options are boolean (`on` or `off`). The root project's
option `X` is named `X`, and a source project `N`'s is `N.X`. Every
configuration has the attribute `option.<name>`, so a condition can test it.
