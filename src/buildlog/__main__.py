# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys

if sys.argv[1:2] == ["-E"]:
    # Build commands run this often: skip the CLI's imports.
    from .builtins import main as builtin_main

    raise SystemExit(builtin_main(sys.argv[2:]))

from .cli import main

raise SystemExit(main())
