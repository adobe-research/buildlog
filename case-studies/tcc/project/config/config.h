/*
 * SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/* What tinycc's ./configure writes for a native macOS arm64 build, cut to
   what tcc.c reads (the case study's README): the version, ad-hoc code
   signing of Mach-O output, DWARF debug info, and no compiled-in predefs
   (tccdefs.h is read from {B}/include at run time instead). The target is
   the building compiler's own (tcc.h detects it), and the paths
   (CONFIG_SYSROOT, CONFIG_TCCDIR) come as -D on the compile line. */
#define TCC_VERSION "0.9.28rc"
#define CONFIG_CODESIGN 1
#define CONFIG_DWARF_VERSION 4
#define CONFIG_TCC_PREDEFS 0
