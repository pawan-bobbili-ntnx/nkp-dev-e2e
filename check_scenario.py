#!/usr/bin/env python3
"""Gate a scenario before it costs a cluster.

    ./check_scenario.py <name>

Runs, in order, everything that can be checked without Prism Central:

  1. the framework self-test - loads EVERY scenario file, so a typo in yours
     is reported as config ("unknown step X, did you mean Y") rather than a
     stack trace forty minutes into a live run;
  2. a dry run of your scenario - prints the exact plan, interpolates every
     ${VAR}, exercises every step's argument handling, touches nothing.

Exit status is non-zero on the first failure, so this is safe in a pre-commit
hook or CI. A green result means "well-formed", not "passes live" - only a
real run proves the assertions.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(label: str, cmd: list[str]) -> None:
    print(f"\n==> {label}")
    if subprocess.run(cmd, cwd=HERE).returncode:
        sys.exit(f"\nFAIL: {label}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    name = sys.argv[1].removesuffix(".yaml")
    if not (HERE / "scenarios" / f"{name}.yaml").is_file():
        known = sorted(p.stem for p in (HERE / "scenarios").glob("*.yaml"))
        sys.exit(f"no scenarios/{name}.yaml\nknown: {', '.join(known)}")
    run("framework self-test (loads every scenario, pins the decision logic)",
        [sys.executable, "selftest.py"])
    run(f"dry-run of {name} (the full plan, nothing touched)",
        [sys.executable, "run_e2e.py", name, "--dry-run"])
    print(f"\nOK: {name} is well-formed.\n"
          f"  live:      ./run_e2e.py {name}\n"
          f"  keep it:   ./run_e2e.py {name} --keep      # leaves the cluster for you")
    return 0


if __name__ == "__main__":
    sys.exit(main())
