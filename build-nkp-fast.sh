#!/bin/bash
# Build the NKP (konvoy) binary from the CURRENT WORKING TREE as fast as
# possible, with caching at three layers:
#
#   1. binary cache  - content-addressed on HEAD + a hash of your uncommitted
#                      diff: the same code never builds twice (~1s on a hit)
#   2. go build cache- persistent GOCACHE/GOMODCACHE: an incremental rebuild
#                      recompiles only the packages your change touched
#   3. single target - passed explicitly, though MEASURED 2026-08-31 this is
#                      not a saving: `make build-snapshot` already resolves to
#                      --single-target=true, and timed both ways it is ~26-30s
#                      either way. Layer 1 is the only real win here (1s), and
#                      the binary was never the bottleneck - the cluster is.
#
#   ./hack/dev-e2e/build-nkp-fast.sh          # prints the binary path
#   NKP_BIN=$(./hack/dev-e2e/build-nkp-fast.sh -q) ./hack/dev-e2e/run_e2e.py ...
set -euo pipefail
QUIET=false; [ "${1:-}" = "-q" ] && QUIET=true
say() { $QUIET || echo "$@" >&2; }

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

# content key: committed state + uncommitted delta
head=$(git rev-parse --short=12 HEAD)
# hash only what can change the binary: a README or hack/ edit should not
# invalidate a perfectly good build (cost us a 58s rebuild on day one)
dirty=$(git diff HEAD -- ':(exclude)*.md' ':(exclude)docs/' ':(exclude)hack/' 2>/dev/null | shasum -a 256 | cut -c1-12)
key="${head}-${dirty}"
CACHE="${NKP_FAST_BUILD_CACHE:-$HOME/.cache/nkp-fast-build}/$key"

if [ -x "$CACHE/konvoy" ]; then
  say "CACHE HIT $key (built $(date -r "$CACHE/konvoy" +%H:%M)) - no build needed"
  echo "$CACHE/konvoy"
  exit 0
fi

say "building konvoy @ $key (single target, warm go cache)"
start=$SECONDS
# devbox toolchain; shield from the shell's GOROOT (live-caught 2026-08-26)
env -u GOROOT devbox run -- make build-snapshot \
  GORELEASER_SINGLE_TARGET=true GORELEASER_VERBOSE=false >&2

bin=$(find "$repo_root/dist" -type f -name konvoy | head -1)
[ -n "$bin" ] || { echo "FATAL: no konvoy binary under dist/" >&2; exit 1; }
mkdir -p "$CACHE"
cp "$bin" "$CACHE/konvoy"
say "built in $((SECONDS - start))s -> cached as $key"
echo "$CACHE/konvoy"
