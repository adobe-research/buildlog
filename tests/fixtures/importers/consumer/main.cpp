#include "greet/greet.h"
#include <cstdio>
std::string shout(const std::string &who);
int main() {
#ifdef GREET_API
  std::puts(shout("consumer").c_str());
#endif
  return 0;
}
