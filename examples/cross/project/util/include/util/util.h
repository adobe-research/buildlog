#pragma once
#if __cplusplus < 202002L
#error "util's public interface requires C++20"
#endif
#include <span>

int util_sum(std::span<const int> xs);
