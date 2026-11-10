// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// The frames of Halide's HelloWasm app (apps/HelloWasm/app/core.cpp), computed
// by the same three pipelines built for the build machine, without SDL or a
// browser: init, then per frame update and render, as core.cpp's mainloop does
// with the mouse at SDL's initial position (0, 0). Writes the last frame's
// pixels (W x H little-endian ARGB words, rows from the top) to argv[2].
//   hellowasm_reference FRAMES OUT
#include <cstdio>
#include <cstdlib>
#include <utility>

#include "HalideBuffer.h"
#include "reaction_diffusion_init.h"
#include "reaction_diffusion_render.h"
#include "reaction_diffusion_update.h"

int main(int argc, char **argv) {
    const int W = 1024, H = 1024;
    const int frames = argc > 1 ? std::atoi(argv[1]) : 32;
    Halide::Runtime::Buffer<float, 3> buf1(W, H, 3), buf2(W, H, 3);
    Halide::Runtime::Buffer<uint32_t, 2> pixel_buf(W, H);
    reaction_diffusion_init(buf1);
    for (int i = 0; i < frames; i++) {
        reaction_diffusion_update(buf1, 0, 0, i, buf2);
        reaction_diffusion_render(buf2, pixel_buf);
        std::swap(buf1, buf2);
    }
    FILE *f = std::fopen(argv[2], "wb");
    std::fwrite(pixel_buf.data(), sizeof(uint32_t), size_t(W) * H, f);
    return std::fclose(f) != 0;
}
