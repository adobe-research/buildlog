#include <core/core.h>

std::optional<int> core_weight() { return 3; }
const char *core_build_kind() {
#ifdef CORE_DEBUG
  return "debug";
#else
  return "release";
#endif
}
