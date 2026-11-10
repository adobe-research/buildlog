/*
 * SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include "greet.h"

int main(void)
{
    printf("hello from %s, 6 * 7 = %lld\n", greet_who(), greet_mul(6, 7));
    return 0;
}
