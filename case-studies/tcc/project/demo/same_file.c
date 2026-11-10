/*
 * SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/* Exits 0 when its two arguments are files with the same bytes (cmp -s). */
#include <stdio.h>

int main(int argc, char **argv)
{
    FILE *a, *b;
    long n = 0;
    int x, y;
    if (argc != 3) {
        fprintf(stderr, "usage: %s FILE1 FILE2\n", argv[0]);
        return 2;
    }
    a = fopen(argv[1], "rb");
    b = fopen(argv[2], "rb");
    if (!a || !b) {
        perror("fopen");
        return 2;
    }
    do {
        x = getc(a);
        y = getc(b);
        if (x != y) {
            printf("%s and %s differ at byte %ld\n", argv[1], argv[2], n);
            return 1;
        }
        n++;
    } while (x != EOF);
    printf("%s and %s are identical (%ld bytes)\n", argv[1], argv[2], n - 1);
    return 0;
}
