{
  "cps_version": "0.14.1",
  "name": "Multi",
  "prefix": "/opt/multi",
  "platform": {"isa": "arm64", "kernel": "darwin"},
  "configurations": ["Release", "Debug"],
  "components": {
    "core": {
      "type": "archive",
      "requires": [":base", "Other:thing"],
      "link_requires": ["Hidden"],
      "includes": {"*": ["@prefix@/include"], "cpp": ["@prefix@/include/c++"], "c": ["@prefix@/include/c"]},
      "definitions": {"*": {"MULTI": "1", "MULTI_BARE": null}, "c": {"ONLY_C": "1"}},
      "compile_flags": {"cpp": ["-fno-rtti"]},
      "compile_features": ["c++20", "threads"],
      "link_flags": ["-Wl,-x"],
      "link_libraries": ["z", "/usr/lib/libobjc.A.tbd", "-framework Foundation"],
      "location": "@prefix@/lib/libcore.a",
      "configurations": {
        "Debug": {"location": "@prefix@/lib/libcored.a", "definitions": {"*": {"MULTI_DEBUG": "1"}}},
        "Release": {"includes": null},
        "RelWithDebInfo": {"location": "@prefix@/lib/libcore-rwdi.a"}
      }
    },
    "base": {"type": "interface", "compile_features": ["c_std_99"]},
    "tool": {"type": "executable", "location": "bin/multi-tool"}
  }
}
