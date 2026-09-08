#!/usr/bin/env python3
# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Delete leftover e2e VMs by name prefix.

`nkp delete cluster` and `delete_claim` are the primary cleanup paths; this
catches what they miss (interrupted runs, scale-out drills, failed clones).
Orphan workers silently eat PE memory until claims fail with
RESOURCE_SHORTAGE, so run this when a run aborts half-way.

    NUTANIX_USER=... NUTANIX_PASSWORD=... ./sweep_debris.py <prefix> [...]

Refuses prefixes shorter than 8 characters so a typo cannot match half the PC.
"""
from __future__ import annotations

import os
import sys

# Sweeping talks only to Prism Central; the nkp binary the shared Config
# insists on is irrelevant here, so satisfy the check with a harmless default.
os.environ.setdefault("NKP_BIN", "/usr/bin/true")

from framework.config import Config
from framework.logging_ import Log
from framework.pc import PrismCentral


def main() -> int:
    prefixes = sys.argv[1:]
    if not prefixes:
        print(__doc__)
        return 2
    for p in prefixes:
        if len(p) < 8:
            print(f"refusing short prefix {p!r} (min 8 chars)")
            return 2
    log = Log("sweep")
    config = Config.from_env()
    pc = PrismCentral(config, log)
    total = 0
    for prefix in prefixes:
        n = pc.sweep(prefix)
        log.info(f"{prefix}: swept {n} VM(s)")
        total += n
    log.info(f"total swept: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
