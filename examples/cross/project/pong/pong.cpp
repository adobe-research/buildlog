#include <ping/ping.h>
#include <pong/pong.h>
int pong(int n) { return n <= 0 ? 0 : 1 + ping(n - 1); }
