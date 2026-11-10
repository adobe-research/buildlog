#pragma once
#ifndef HAVE_SIMD
#error "HAVE_SIMD must come from simd's public usage requirements"
#endif
int simd_dot4(const int *a, const int *b);
