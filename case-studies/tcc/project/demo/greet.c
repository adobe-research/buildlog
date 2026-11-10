/*
 * SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "greet.h"

const char *greet_who(void)
{
#ifdef __TINYC__
    return "tcc";
#else
    return "not tcc";
#endif
}

long long greet_mul(long long a, long long b)
{
    return a * b;
}
