// A stand-in for a Halide generator: a host tool that, given a target triple,
// produces a static library + C header compatible with that target. Real Halide
// generators embed LLVM; this one shells out to a C compiler for the target.
#include <core/core.h>
#include <cstdio>
#include <cstdlib>
#include <string>

#if defined(__aarch64__)
static const char *kGenArch = "arm64";
#elif defined(__x86_64__)
static const char *kGenArch = "x86_64";
#else
static const char *kGenArch = "unknown";
#endif

int main(int argc, char **argv) {
  std::string out, fn, triple, cc;
  for (int i = 1; i + 1 < argc; i += 2) {
    std::string k = argv[i];
    if (k == "-o") out = argv[i + 1];
    else if (k == "-f") fn = argv[i + 1];
    else if (k == "-target") triple = argv[i + 1];
    else if (k == "-cc") cc = argv[i + 1];
  }
  if (out.empty() || fn.empty() || triple.empty() || cc.empty()) {
    std::fprintf(stderr, "usage: blurgen -o DIR -f NAME -target TRIPLE -cc CC\n");
    return 2;
  }
  int w = core_weight().value_or(1);
  if (std::system(("mkdir -p '" + out + "'").c_str()) != 0) return 1;
  std::string h = out + "/" + fn + ".h", c = out + "/" + fn + ".c";
  FILE *f = std::fopen(h.c_str(), "w");
  std::fprintf(f,
               "#pragma once\n#ifdef __cplusplus\nextern \"C\" {\n#endif\n"
               "void %s(const int *in, int *out, int n);\n"
               "const char *%s_target(void);\nconst char *%s_generated_on(void);\n"
               "#ifdef __cplusplus\n}\n#endif\n",
               fn.c_str(), fn.c_str(), fn.c_str());
  std::fclose(f);
  f = std::fopen(c.c_str(), "w");
  std::fprintf(f,
               "#include \"%s.h\"\n"
               "void %s(const int *in, int *out, int n) {\n"
               "  for (int i = 0; i < n; i++) {\n"
               "    int l = in[i > 0 ? i - 1 : i], r = in[i + 1 < n ? i + 1 : i];\n"
               "    out[i] = (l + in[i] + r) / %d;\n  }\n}\n"
               "const char *%s_target(void) { return \"%s\"; }\n"
               "const char *%s_generated_on(void) { return \"%s\"; }\n",
               fn.c_str(), fn.c_str(), w, fn.c_str(), triple.c_str(), fn.c_str(), kGenArch);
  std::fclose(f);
  std::string o = out + "/" + fn + ".o", a = out + "/" + fn + ".a";
  std::string cmd = "'" + cc + "' -x c --target=" + triple + " -c '" + c + "' -o '" + o +
                    "' && rm -f '" + a + "' && ar rcs '" + a + "' '" + o + "'";
  return std::system(cmd.c_str()) == 0 ? 0 : 1;
}
