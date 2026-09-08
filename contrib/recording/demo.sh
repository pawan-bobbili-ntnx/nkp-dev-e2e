#!/usr/bin/env bash
# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Runs the E2E framework demo end to end in about a minute. Nothing here
# touches Prism Central, so it is safe to run in front of an audience.
#
#   ./hack/dev-e2e/demo.sh          # pauses between steps (press Enter)
#   ./hack/dev-e2e/demo.sh --auto   # runs straight through

set -uo pipefail
cd "$(dirname "$0")"

AUTO=${1:-}
OUT=$(mktemp -d)
BOLD=$'\033[1m'; CYAN=$'\033[1;36m'; DIM=$'\033[2m'; RESET=$'\033[0m'

step() {
  echo
  echo "${CYAN}────────────────────────────────────────────────────────────${RESET}"
  echo "${BOLD}$1${RESET}"
  echo "${DIM}$2${RESET}"
  echo "${CYAN}────────────────────────────────────────────────────────────${RESET}"
  [ "$AUTO" = "--auto" ] || read -r -p "press Enter to run..." _
}

step "1. What can I run?" \
     "Scenarios are discovered automatically - one file each, no registry to edit."
python3 run_e2e.py --list

step "2. What IS a scenario?" \
     "A YAML file in scenarios/. No Python. This is the whole test case."
cat scenarios/day2-operations.yaml

step "3. The vocabulary developers build from" \
     "Steps are named actions. Adding one is a single function in framework/steps.py."
python3 run_e2e.py --steps

step "4. What would a real scenario actually do?" \
     "--dry-run prints every command, with no side effects. Note the exact nkp CLI call."
python3 run_e2e.py cluster-lifecycle --dry-run --artifacts "$OUT/dry" 2>&1 | sed -n '1,28p'





