#pragma once
#ifndef CORE_API
#error "CORE_API must arrive via core's public usage requirements"
#endif
#if __cplusplus < 201703L
#error "core requires C++17"
#endif
#include <optional>

std::optional<int> core_weight();
const char *core_build_kind();
