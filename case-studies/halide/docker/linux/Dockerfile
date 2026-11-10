# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Linux/GCC environment for the Halide case study (M3 phases 7b and 7c).
#
# 7b builds Halide entirely in this image: GCC compiles libHalide, the apt.llvm.org
# LLVM/Clang/LLD 22 packages provide Halide's LLVM and the runtime's clang/llvm-as.
# 7c exports this image's filesystem as the sysroot of a macOS -> Linux cross build
# (../sysroot.sh), so everything a Linux libHalide links against lives here.
#
# CMake, Ninja and uv come from PyPI at the versions the macOS baseline used, and
# flatbuffers and wabt are built from source at the Homebrew versions, so that the
# graphs differ by platform and toolchain family rather than by tool versions. Matching
# flatbuffers also matters for 7c: the macOS-hosted flatc's generated header
# static_asserts the flatbuffers version of the headers in the sysroot.
#
# Build with ../build-image.sh (it records the resolved versions and the image digest).

# ubuntu:24.04 (noble), linux/arm64, pinned by index digest.
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

ARG LLVM_MAJOR=22
ARG CMAKE_VERSION=4.4.3
ARG NINJA_VERSION=1.13.2
ARG UV_VERSION=0.12.19
ARG FLATBUFFERS_TAG=v25.12.19
ARG WABT_TAG=1.0.42

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

# Base toolchain (GCC 13 is noble's default) and the libraries Halide's tests and LLVM's
# exported config need. python3-pip only bootstraps the PyPI tools below. python3-dev and
# pybind11-dev (2.11.1, Halide's minimum) are there because WITH_PYTHON_BINDINGS defaults
# to ON and configure fails without them; the bindings themselves are out of scope.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc g++ binutils \
        ca-certificates curl gnupg git file patch \
        python3 python3-pip python3-venv python3-dev pybind11-dev \
        pkg-config \
        libpng-dev libjpeg-dev \
        zlib1g-dev libzstd-dev libxml2-dev libedit-dev libffi-dev libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

# LLVM/Clang/LLD from apt.llvm.org (llvm-toolchain-noble-22).
RUN curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/apt.llvm.org.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/apt.llvm.org.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-${LLVM_MAJOR} main" \
        > /etc/apt/sources.list.d/llvm.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        llvm-${LLVM_MAJOR} llvm-${LLVM_MAJOR}-dev llvm-${LLVM_MAJOR}-tools llvm-${LLVM_MAJOR}-runtime \
        libllvm${LLVM_MAJOR} \
        clang-${LLVM_MAJOR} libclang-${LLVM_MAJOR}-dev libclang-cpp${LLVM_MAJOR}-dev \
        lld-${LLVM_MAJOR} liblld-${LLVM_MAJOR}-dev \
        libpolly-${LLVM_MAJOR}-dev \
    && rm -rf /var/lib/apt/lists/*

# CMake, Ninja, uv at the macOS baseline's versions.
RUN pip3 install --no-cache-dir --break-system-packages \
        "cmake==${CMAKE_VERSION}" "ninja==${NINJA_VERSION}" "uv==${UV_VERSION}"

# flatbuffers (flatc + libflatbuffers.a), built like Homebrew's formula, plus PIC because
# the static library ends up in libHalide.so.
RUN git clone --depth 1 --branch ${FLATBUFFERS_TAG} https://github.com/google/flatbuffers /opt/src/flatbuffers \
    && cmake -S /opt/src/flatbuffers -B /opt/src/flatbuffers/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DFLATBUFFERS_BUILD_SHAREDLIB=ON -DFLATBUFFERS_BUILD_TESTS=OFF \
    && cmake --build /opt/src/flatbuffers/build \
    && cmake --install /opt/src/flatbuffers/build \
    && rm -rf /opt/src/flatbuffers && ldconfig

# wabt (libwabt.a + its CMake config), built like Homebrew's formula on Linux.
RUN git clone --depth 1 --branch ${WABT_TAG} --recurse-submodules --shallow-submodules \
        https://github.com/WebAssembly/wabt /opt/src/wabt \
    && cmake -S /opt/src/wabt -B /opt/src/wabt/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_TESTS=OFF -DWITH_WASI=ON -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    && cmake --build /opt/src/wabt/build \
    && cmake --install /opt/src/wabt/build \
    && rm -rf /opt/src/wabt

# The resolved versions, for build-image.sh and the README.
RUN { \
      echo "gcc=$(gcc -dumpfullversion)"; \
      echo "llvm=$(/usr/lib/llvm-${LLVM_MAJOR}/bin/llvm-config --version)"; \
      echo "llvm_pkg=$(dpkg-query -W -f='${Version}' llvm-${LLVM_MAJOR})"; \
      echo "clang=$(/usr/lib/llvm-${LLVM_MAJOR}/bin/clang --version | head -n1)"; \
      echo "cmake=$(cmake --version | head -n1)"; \
      echo "ninja=$(ninja --version)"; \
      echo "uv=$(uv --version)"; \
      echo "python=$(python3 --version)"; \
      echo "flatc=$(flatc --version)"; \
      echo "wabt=$(/usr/local/bin/wat2wasm --version)"; \
      echo "libpng=$(dpkg-query -W -f='${Version}' libpng-dev)"; \
      echo "libjpeg=$(dpkg-query -W -f='${Version}' libjpeg-turbo8-dev)"; \
      echo "glibc=$(dpkg-query -W -f='${Version}' libc6)"; \
    } > /etc/halide-case-versions && cat /etc/halide-case-versions

WORKDIR /work
