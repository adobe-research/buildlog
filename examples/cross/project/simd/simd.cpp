// Only ever built for x86_64 configurations (app depends on it conditionally).
#include <simd/simd.h>
#if !defined(__x86_64__)
#error "simd is x86_64-only; the conditional dependency should have excluded it"
#endif
#include <immintrin.h>
int simd_dot4(const int *a, const int *b) {
  __m128i p = _mm_mullo_epi32(_mm_loadu_si128((const __m128i *)a), _mm_loadu_si128((const __m128i *)b));
  int o[4];
  _mm_storeu_si128((__m128i *)o, p);
  return o[0] + o[1] + o[2] + o[3];
}
