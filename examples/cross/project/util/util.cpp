#include <util/util.h>
#include <core/core.h>
#ifndef UTIL_IMPL
#error "UTIL_IMPL is util's private define"
#endif

int util_sum(std::span<const int> xs) {
  int s = 0;
  for (int x : xs) s += x;
  return s * core_weight().value_or(1) / core_weight().value_or(1);
}
