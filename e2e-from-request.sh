#!/bin/bash
# The multi-repo pipeline, end to end:
#
#   ./hack/dev-e2e/e2e-from-request.sh my-change.yaml day2-operations
#
# my-change.yaml (an nkp-instant-build request):
#   base: v2.18.0                # GA pins for every repo NOT mentioned below
#   cluster: {name: dev-pawan1}
#   changes:
#     - {repo: konvoy2,   ref: my-feature}       # local branch wins; origin/ fallback
#     - {repo: kommander, ref: fix/controller}   # as many repos as you changed
#
# What happens: the orchestrator resolves each ref (local checkout first - the
# newest code lives there - then origin), builds ONLY what actually changed
# against the GA base, claims a cluster from the frozen template (~9 min;
# the template itself is the "first cluster built traditionally"), injects the
# built artifacts, and hands back a kubeconfig. The scenarios then run against
# that cluster.
set -euo pipefail
REQUEST="${1:?usage: e2e-from-request.sh <request.yaml> [scenario...]}"; shift
SCENARIOS=("${@:-request-sanity}")
IB="${NKP_INSTANT_BUILD_DIR:-$HOME/Documents/nkp/nkp-instant-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -d "$IB" ] || { echo "FATAL: nkp-instant-build not found at $IB (set NKP_INSTANT_BUILD_DIR)"; exit 1; }
"$IB/orchestrate" "$REQUEST"

# the orchestrator leaves the claimed cluster's kubeconfig in its state dir,
# named after the request's cluster name
NAME=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$REQUEST'))['cluster']['name'])")
STATE=$(python3 -c "import yaml; print(yaml.safe_load(open('$IB/config.yaml'))['state_dir'])")
KC="$STATE/$NAME-claim.conf"
[ -f "$KC" ] || { echo "FATAL: expected claim kubeconfig at $KC"; exit 1; }

echo "==> cluster from your branches is up; running: ${SCENARIOS[*]}"
E2E_KUBECONFIG="$KC" exec python3 "$HERE/run_e2e.py" "${SCENARIOS[@]}"
