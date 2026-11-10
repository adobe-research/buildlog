# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The Linux evaluation image (linux/Dockerfile) plus the CUDA toolkit, for the
# buildlog-cuda tests that need a real nvcc and for the stdgpu study. The
# toolkit is the linux-sbsa one of NVIDIA's arm64 devel image; nothing here
# can run a GPU program (there is no GPU), only compile and link.
ARG CUDA_IMAGE=nvidia/cuda:13.4.2-devel-ubuntu24.04
FROM ${CUDA_IMAGE} AS cuda

FROM buildlog-linux:24.04
ARG CUDA_VERSION=13.4
COPY --from=cuda /usr/local/cuda-${CUDA_VERSION} /usr/local/cuda-${CUDA_VERSION}
RUN ln -s /usr/local/cuda-${CUDA_VERSION} /usr/local/cuda
ENV CUDAToolkit_ROOT=/usr/local/cuda PATH=/usr/local/cuda/bin:$PATH
