#include <blur.h>
#include <core/core.h>
#include <ping/ping.h>
#include <util/util.h>
#include <zlib.h>
#include <cstdio>
#ifdef HAVE_SIMD
#include <simd/simd.h>
#endif
#ifdef UTIL_IMPL
#error "util's private define leaked into a consumer"
#endif
#ifndef CORE_API
#error "core's public define should reach app transitively via util's PUBLIC dep on core"
#endif
#if defined(__x86_64__) == defined(SIMD_FALLBACK)
#error "SIMD_FALLBACK must be present exactly in non-x86_64 configurations (complement condition)"
#endif
#if defined(__x86_64__) != defined(HAVE_SIMD)
#error "HAVE_SIMD must be present exactly in x86_64 configurations"
#endif

#if defined(__has_feature)
#if __has_feature(address_sanitizer)
#define ASAN_ON 1
#endif
#endif

int main() {
  int in[5] = {3, 6, 9, 12, 15}, out[5];
  blur(in, out, 5);
#if defined(__x86_64__)
  const char *arch = "x86_64";
  int a[4] = {1, 2, 3, 4};
  int simd = simd_dot4(a, a);
#else
  const char *arch = "arm64";
  int simd = -1;
#endif
#ifdef NDEBUG
  const char *opt = "release";
#else
  const char *opt = "debug";
#endif
#ifdef ASAN_ON
  const char *asan = "on";
#else
  const char *asan = "off";
#endif
#ifdef CORE_CHECKED
  const char *checked = "yes";
#else
  const char *checked = "no";
#endif
  std::printf("app arch=%s opt=%s core=%s asan=%s checked=%s simd=%d pingpong=%d blur_target=%s "
              "blur_generated_on=%s zlib=%s sum=%d\n",
              arch, opt, core_build_kind(), asan, checked, simd, ping(7), blur_target(),
              blur_generated_on(), zlibVersion(), util_sum(out));
  return 0;
}
