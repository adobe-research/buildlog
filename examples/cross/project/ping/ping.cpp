// ping and pong are mutually dependent static libraries (a link-order cycle).
#include <ping/ping.h>
#include <pong/pong.h>
int ping(int n) { return n <= 0 ? 0 : 1 + pong(n - 1); }
