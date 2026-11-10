# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The one Linux evaluation image for buildlog (the case studies, the upstream
# baselines and Halide's 7b ground truth).
#
# Ubuntu 24.04 LTS (arm64 on Apple silicon): GCC 13 and Clang 18 as the two
# toolchain families (plus GCC 14 and Clang 22 with libc++, for C++ modules:
# P1689 scanning, tests/test_cxx_modules.py), CMake and Ninja (from PyPI, at the macOS baselines'
# versions), Meson for fribidi's baseline, uv for the driver, LLVM 22 (apt.llvm.org,
# Halide's LLVM and its runtime's clang), flatbuffers and wabt as Halide's
# 7b image builds them, googletest and the two SPIRV-Headers installs the
# SPIRV-Tools study imports, and Soufflé built from the patched source the macOS runs
# use (the SOUFFLE_SRC checkout, main by default; linux/run.sh exports
# it into the build context as souffle.tar). gcc-x86-64-linux-gnu is there
# to check GCC's x86 ISA flag spellings (F_gcc.dl) on an arm64 host.
#
# Build and run with linux/run.sh.
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

ARG UV_VERSION=0.12.19
ARG PYTHON_VERSION=3.14
ARG JOBS=4
ARG LLVM_MAJOR=22
ARG CMAKE_VERSION=4.4.3
ARG NINJA_VERSION=1.13.2
ARG MESON_VERSION=1.12.1
ARG FLATBUFFERS_TAG=v25.12.19
ARG WABT_TAG=1.0.42
ARG GOOGLETEST_TAG=v1.18.0
ARG SPIRV_HEADERS_REV=29981f65241605e08b0ede4cfeb999fe3b723c6a
ARG SPIRV_HEADERS_ALT_TAG=vulkan-sdk-1.4.363.0

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc g++ gcc-14 g++-14 binutils gcc-x86-64-linux-gnu g++-x86-64-linux-gnu \
        clang lld llvm \
        pkg-config \
        ca-certificates curl gnupg git file patch rsync \
        python3 python3-pip python3-venv python3-dev pybind11-dev \
        libpng-dev libjpeg-dev libzstd-dev libxml2-dev libedit-dev libssl-dev libcurl4-openssl-dev \
        bison flex mcpp libffi-dev zlib1g-dev libsqlite3-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/*

# CMake, Ninja and Meson at the versions the macOS baselines used (Halide's 7b image does the same).
RUN pip3 install --no-cache-dir --break-system-packages \
        "cmake==${CMAKE_VERSION}" "ninja==${NINJA_VERSION}" "meson==${MESON_VERSION}" "uv==${UV_VERSION}" \
    && UV_PYTHON_INSTALL_DIR=/opt/python uv python install ${PYTHON_VERSION}

# LLVM/Clang/LLD ${LLVM_MAJOR} from apt.llvm.org, for Halide (its /usr/lib/llvm-22 does not
# displace the unversioned clang 18 above).
RUN curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/apt.llvm.org.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/apt.llvm.org.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-${LLVM_MAJOR} main" \
        > /etc/apt/sources.list.d/llvm.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        llvm-${LLVM_MAJOR} llvm-${LLVM_MAJOR}-dev llvm-${LLVM_MAJOR}-tools llvm-${LLVM_MAJOR}-runtime \
        libllvm${LLVM_MAJOR} \
        clang-${LLVM_MAJOR} libclang-${LLVM_MAJOR}-dev libclang-cpp${LLVM_MAJOR}-dev \
        libc++-${LLVM_MAJOR}-dev libc++abi-${LLVM_MAJOR}-dev \
        lld-${LLVM_MAJOR} liblld-${LLVM_MAJOR}-dev libpolly-${LLVM_MAJOR}-dev \
    && rm -rf /var/lib/apt/lists/*

# flatbuffers (flatc + libflatbuffers) and wabt, as Halide's 7b image builds them.
RUN git clone --depth 1 --branch ${FLATBUFFERS_TAG} https://github.com/google/flatbuffers /opt/src/flatbuffers \
    && cmake -S /opt/src/flatbuffers -B /opt/src/flatbuffers/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DFLATBUFFERS_BUILD_SHAREDLIB=ON -DFLATBUFFERS_BUILD_TESTS=OFF \
    && cmake --build /opt/src/flatbuffers/build -j ${JOBS} \
    && cmake --install /opt/src/flatbuffers/build \
    && rm -rf /opt/src/flatbuffers && ldconfig
RUN git clone --depth 1 --branch ${WABT_TAG} --recurse-submodules --shallow-submodules \
        https://github.com/WebAssembly/wabt /opt/src/wabt \
    && cmake -S /opt/src/wabt -B /opt/src/wabt/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_TESTS=OFF -DWITH_WASI=ON -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    && cmake --build /opt/src/wabt/build -j ${JOBS} \
    && cmake --install /opt/src/wabt/build \
    && rm -rf /opt/src/wabt

# What the SPIRV-Tools study imports: googletest (Homebrew's 1.18.0 on macOS) at
# /opt/googletest, SPIRV-Headers at the commit of SPIRV-Tools v2026.3's DEPS at
# /opt/spirv-headers and the second release (vulkan-sdk-1.4.363.0) at /opt/spirv-headers-alt.
RUN git clone --depth 1 --branch ${GOOGLETEST_TAG} https://github.com/google/googletest /opt/src/googletest \
    && cmake -S /opt/src/googletest -B /opt/src/googletest/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/googletest \
    && cmake --build /opt/src/googletest/build -j ${JOBS} && cmake --install /opt/src/googletest/build \
    && rm -rf /opt/src/googletest
RUN install_headers() { \
      git init -q /opt/src/sh && git -C /opt/src/sh fetch -q --depth 1 https://github.com/KhronosGroup/SPIRV-Headers.git "$1" \
      && git -C /opt/src/sh checkout -q FETCH_HEAD \
      && cmake -S /opt/src/sh -B /opt/src/sh/build -G Ninja -DSPIRV_HEADERS_ENABLE_TESTS=OFF -DCMAKE_INSTALL_PREFIX="$2" \
      && cmake --install /opt/src/sh/build && git -C /opt/src/sh rev-parse HEAD > "$2/commit.txt" \
      && rm -rf /opt/src/sh; }; \
    install_headers ${SPIRV_HEADERS_REV} /opt/spirv-headers && install_headers ${SPIRV_HEADERS_ALT_TAG} /opt/spirv-headers-alt

# Soufflé, as the macOS build is configured (64-bit domain, RelWithDebInfo).
COPY souffle.tar souffle-commit.txt /opt/src/
RUN mkdir -p /opt/src/souffle && tar -xf /opt/src/souffle.tar -C /opt/src/souffle \
    && cmake -S /opt/src/souffle -B /opt/src/souffle/build -G Ninja \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo -DSOUFFLE_GIT=OFF -DSOUFFLE_DOMAIN_64BIT=ON \
        -DSOUFFLE_ENABLE_TESTING=OFF -DSOUFFLE_BASH_COMPLETION=OFF \
        -DCMAKE_INSTALL_PREFIX=/opt/souffle \
    && ninja -C /opt/src/souffle/build -j ${JOBS} install \
    && cp /opt/src/souffle-commit.txt /opt/souffle/commit.txt \
    && rm -rf /opt/src/souffle/build /opt/src/souffle.tar

# nanobind's case study: the two uv-managed CPythons the macOS study builds
# for (3.13 for the stable ABI, 3.11 for the full ABI; uv offers no
# 3.11.15 for Linux, so 3.11.16), each in a venv with the build requirements
# at the macOS baseline's versions (baseline/run-baseline.sh builds without
# isolation). Other interpreters (or emsdk's, Bazel's) are further `uv python install`s.
ARG NANOBIND_PY313=3.13.15
ARG NANOBIND_PY311=3.11.16
RUN export UV_PYTHON_INSTALL_DIR=/opt/python UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy \
    && mkdir -p /opt/nanobind-venvs \
    && for pair in "313 ${NANOBIND_PY313}" "311 ${NANOBIND_PY311}"; do set -- $pair; \
         uv python install "$2" && uv venv -q --no-project -p "$2" /opt/nanobind-venvs/venv$1 \
         && VIRTUAL_ENV=/opt/nanobind-venvs/venv$1 uv pip install -q scikit-build-core==1.1.0 nanobind==3.1.0 \
              build==1.6.1 cmake==${CMAKE_VERSION} ninja==${NINJA_VERSION} pytest==9.1.1; \
       done

# Tier 3-4 additions, as layers after the nanobind one so that the layers above stay cached.
#
# Qt 6.11.3 (the macOS study's version, from Qt's own binaries by aqtinstall: linux_gcc_arm64, the
# qtbase and icu archives) at /opt/qt/6.11.3/gcc_arm64, with the system libraries its Core, Gui,
# Widgets and offscreen platform plugin link. Ubuntu's Qt 6.4.2 would not do: the package manifests
# pin ==6.11.3 and import Qt's own lib/cmake exports at a prefix-relative path that the distribution
# splits across /usr/lib/<triple> and /usr/lib/qt6.
ARG AQT_VERSION=3.3.0
ARG QT_VERSION=6.11.3
RUN apt-get update && apt-get install -y --no-install-recommends \
        libegl1 libgl1 libx11-6 libdbus-1-3 libfontconfig1 libfreetype6 libglib2.0-0t64 libxkbcommon0 \
        libgl-dev libegl-dev unzip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && uv tool run --from aqtinstall==${AQT_VERSION} aqt install-qt linux_arm64 desktop ${QT_VERSION} linux_gcc_arm64 \
        --archives qtbase icu -O /opt/qt \
    && rm -rf /root/.cache

# An amd64 userland for the Android NDK: its host tools exist for x86_64 only. Docker Desktop's
# binfmt emulation runs them inside this arm64 container once the amd64 loader and libc are there
# (Ubuntu's amd64 archive added as a second architecture; nothing else amd64 is installed).
RUN sed -i 's/^Types: deb$/Types: deb\nArchitectures: arm64/' /etc/apt/sources.list.d/ubuntu.sources \
    && sed -i 's/^deb \[signed-by/deb [arch=arm64 signed-by/' /etc/apt/sources.list.d/llvm.list \
    && printf 'Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates noble-security\nComponents: main universe\nArchitectures: amd64\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/ubuntu-amd64.sources \
    && dpkg --add-architecture amd64 && apt-get update \
    && apt-get install -y --no-install-recommends libc6:amd64 libstdc++6:amd64 zlib1g:amd64 \
    && rm -rf /var/lib/apt/lists/*

# The Android NDK, the macOS study's r30-beta2 (30.0.15729638), linux-x86_64 host tools.
ARG NDK_ZIP=android-ndk-r30-beta2-linux.zip
ARG NDK_SHA1=069427047d18cc6e44369cd7b8e711bfc2c7a3b5
RUN mkdir -p /opt/android-ndk && cd /opt/android-ndk \
    && curl -fsSL -o ndk.zip https://dl.google.com/android/repository/${NDK_ZIP} \
    && echo "${NDK_SHA1}  ndk.zip" | sha1sum -c - && unzip -q ndk.zip && rm ndk.zip \
    && mv android-ndk-r30-beta2 30.0.15729638

# Emscripten 3.1.73 (emsdk's arm64 Linux binaries) and binaryen version_123 (emsdk's bundled 119 rejects
# what LLVM 22's Halide emits, for CMake and buildlog alike). The SDL2 port is fetched into the
# cache now, so that builds need no network.
ARG EMSDK_REV=5eb0bde7585670252e8ba05e9d361627bffd08b5
ARG EMSCRIPTEN_VERSION=3.1.73
ARG BINARYEN_TAG=version_123
RUN git clone -q https://github.com/emscripten-core/emsdk /opt/emsdk && git -C /opt/emsdk checkout -q ${EMSDK_REV} \
    && cd /opt/emsdk && ./emsdk install ${EMSCRIPTEN_VERSION} && ./emsdk activate ${EMSCRIPTEN_VERSION} \
    && rm -rf downloads \
    && mkdir -p /opt/binaryen && curl -fsSL https://github.com/WebAssembly/binaryen/releases/download/${BINARYEN_TAG}/binaryen-${BINARYEN_TAG}-aarch64-linux.tar.gz \
        | tar -xz -C /opt/binaryen --strip-components=1 \
    && . ./emsdk_env.sh >/dev/null && EM_BINARYEN_ROOT=/opt/binaryen embuilder build sdl2 \
    && rm -rf /root/.emscripten_cache

# Bazel 8.8.1 (the bazel-flatc study's .bazelversion), the release binary with its embedded JDK.
ARG BAZEL_VERSION=8.8.1
RUN curl -fsSL -o /usr/local/bin/bazel https://github.com/bazelbuild/bazel/releases/download/${BAZEL_VERSION}/bazel-${BAZEL_VERSION}-linux-arm64 \
    && chmod +x /usr/local/bin/bazel

# An install of Halide (the case study's pin, built as the 7b ground truth is: Release, LLVM 22, GCC)
# for the HelloWasm and HelloAndroid apps: the macOS runs' BUILDLOG_HALIDE_PREFIX. The source comes in
# as a tar (linux/run.sh exports the submodule with git archive) because it must not be extracted on
# a case-insensitive file system.
ARG HALIDE_LLVM_ROOT=/usr/lib/llvm-22
COPY halide-src.tar /opt/src/
RUN mkdir -p /opt/src/halide && tar -xf /opt/src/halide-src.tar -C /opt/src/halide \
    && cmake -S /opt/src/halide -B /opt/src/halide/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DHalide_LLVM_ROOT=${HALIDE_LLVM_ROOT} -DCMAKE_INSTALL_PREFIX=/opt/halide \
        -DWITH_TESTS=OFF -DWITH_TUTORIALS=OFF -DWITH_PYTHON_BINDINGS=OFF -DWITH_DOCS=OFF \
    && cmake --build /opt/src/halide/build -j ${JOBS} && cmake --install /opt/src/halide/build \
    && rm -rf /opt/src/halide /opt/src/halide-src.tar

ENV BUILDLOG_NANOBIND_VENVS=/opt/nanobind-venvs \
    BUILDLOG_SOUFFLE=/opt/souffle/bin/souffle \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    BUILDLOG_SPIRV_HEADERS_PREFIX=/opt/spirv-headers \
    BUILDLOG_SPIRV_HEADERS_ALT_PREFIX=/opt/spirv-headers-alt \
    BUILDLOG_QT_PREFIX=/opt/qt/6.11.3/gcc_arm64 \
    BUILDLOG_HALIDE_PREFIX=/opt/halide \
    EMSDK=/opt/emsdk EM_BINARYEN_ROOT=/opt/binaryen \
    ANDROID_NDK_ROOT=/opt/android-ndk/30.0.15729638

RUN { uname -m; . /etc/os-release; echo "$PRETTY_NAME"; \
      gcc --version | head -1; clang --version | head -1; ninja --version; cmake --version | head -1; \
      echo "meson $(meson --version)"; uv --version; cat /opt/souffle/commit.txt; \
      echo "llvm $(/usr/lib/llvm-${LLVM_MAJOR}/bin/llvm-config --version) ($(dpkg-query -W -f='${Version}' llvm-${LLVM_MAJOR}))"; \
      echo "flatc $(flatc --version)"; echo "wabt $(wat2wasm --version)"; \
      echo "googletest ${GOOGLETEST_TAG}"; echo "spirv-headers $(cat /opt/spirv-headers/commit.txt)"; \
      echo "spirv-headers-alt $(cat /opt/spirv-headers-alt/commit.txt)"; \
      for t in 313 311; do echo "nanobind venv$t: $(/opt/nanobind-venvs/venv$t/bin/python -c 'import sys, importlib.metadata as m; print(sys.version.split()[0], *(p + " " + m.version(p) for p in ("scikit-build-core", "nanobind", "build", "pytest")))')"; done; \
      echo "qt $(/opt/qt/6.11.3/gcc_arm64/bin/qmake -query QT_VERSION) ($(ls /opt/qt))"; \
      echo "ndk $(sed -n 's/^Pkg.Revision = //p' ${ANDROID_NDK_ROOT}/source.properties)"; \
      echo "emscripten $(cat /opt/emsdk/upstream/emscripten/emscripten-version.txt | tr -d '\"') (emsdk ${EMSDK_REV})"; \
      echo "binaryen $(/opt/binaryen/bin/wasm-opt --version)"; echo "bazel $(bazel --version 2>/dev/null)"; \
      echo "halide $(basename $(readlink -f /opt/halide/lib/libHalide.so))"; \
      echo "libpng $(dpkg-query -W -f='${Version}' libpng-dev)"; echo "glibc $(dpkg-query -W -f='${Version}' libc6)"; \
    } > /etc/buildlog-versions
