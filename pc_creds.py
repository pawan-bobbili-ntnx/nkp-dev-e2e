#!/usr/bin/env python3
"""Is Prism Central reachable, and does it accept the credentials in the env?

    ./pc_creds.py          (or: make creds)

Five seconds, no cluster. Run it before a live scenario: the dev PC's
credentials rotate every few days, and an expired password otherwise surfaces
31 seconds into a run as a failure that reads like a network problem.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework.config import Config, ConfigError, load_env_file  # noqa: E402
from framework.logging_ import Log  # noqa: E402
from framework.pc import PrismCentral  # noqa: E402

load_env_file()
try:
    cfg = Config.from_env()
except ConfigError as exc:
    sys.exit(f"config: {exc}")
try:
    PrismCentral(cfg, Log(prefix="pc")).\
        _call("POST", "/api/nutanix/v3/clusters/list", {"kind": "cluster", "length": 1})
except Exception as exc:  # noqa: BLE001
    sys.exit(f"NOT OK: {exc}")
print(f"OK: {cfg.pc_url} accepts {cfg.pc_user}")
