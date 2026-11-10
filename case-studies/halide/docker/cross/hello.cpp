// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Cross toolchain smoke test (M3 phase 7c): built on macOS for aarch64-linux-gnu,
// run in the Linux image. Exercises libstdc++ (iostream, exceptions, threads) and dlopen.
#include <dlfcn.h>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

int main() {
    std::string where = "unknown";
    std::thread t([&] { where = "a std::thread"; });
    t.join();
    try {
        throw std::runtime_error("exceptions work");
    } catch (const std::exception &e) {
        std::cout << e.what() << ", ran on " << where << "\n";
    }
    void *h = dlopen("libz.so.1", RTLD_NOW);
    std::cout << "dlopen(libz.so.1): " << (h ? "ok" : dlerror()) << "\n";
    std::cout << "hello from aarch64-linux-gnu, __GLIBCXX__=" << __GLIBCXX__ << "\n";
    return h ? 0 : 1;
}
