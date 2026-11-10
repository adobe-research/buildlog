# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Keep pytest out of the Halide source export, sysroot and scratch dirs of the Docker
# scenarios (all gitignored), e.g. xvol/src/python_bindings/**/*_test.py.
collect_ignore_glob = ["xvol/*", "sysroot/*", "raw/*"]
