# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Shadow flatbuffers package for the macOS -> Linux Halide cross build (M3 phase 7c).
#
# The sysroot's package (built in the Linux image) provides flatbuffers::flatbuffers for
# the target, but its flatbuffers::flatc is a Linux executable that cannot run during a
# macOS-hosted build. CMake has one package namespace per build, so the library half
# comes from the sysroot and the flatc half from the host (Homebrew, the same 25.12.19:
# the generated header static_asserts the flatbuffers version).
#
# Used with -DFlatBuffers_DIR=<this directory>.
set(_fb_target "${CMAKE_SYSROOT}/usr/local/lib/cmake/flatbuffers")
set(_fb_host "/opt/homebrew/lib/cmake/flatbuffers")
include("${_fb_target}/FlatBuffersTargets.cmake")
include("${_fb_target}/FlatBuffersSharedTargets.cmake")
include("${_fb_host}/FlatcTargets.cmake")
include("${_fb_target}/BuildFlatBuffers.cmake")
