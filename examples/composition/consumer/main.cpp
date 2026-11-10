// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include <cstdio>

extern const int ANSWER;
extern const int BONUS;

int main() {
    std::printf("%d\n", ANSWER + BONUS);
    return ANSWER + BONUS == 42 ? 0 : 1;
}
