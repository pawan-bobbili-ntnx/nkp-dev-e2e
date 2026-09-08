#!/bin/bash
# Shared helpers for the NKP dev-loop scripts (CD vectors on an instant-cluster claim).
# Every vector talks to ONE claim, identified by its kubeconfig ($KC). See DEVLOOP.md.
# Layout facts these helpers rely on (validated live):
#   - flux reconciles ONLY the claim's local kommander.git (git-operator-git-0 pod)
#   - kommander-operator re-pushes OCI content over git edits -> must be scaled to 0 first
#   - per-app flux Kustomizations live in ns kommander with a 6h interval -> must be nudged
#   - the git pod shell is busybox (no GNU grep/awk extensions)

GIT_POD=git-operator-git-0
GIT_NS=git-operator-system
GIT_CTR=git-server-fcgi
BARE=/volumes/git/kommander/kommander.git
DEVLOOP_REGISTRY="${DEVLOOP_REGISTRY:-ttl.sh}"

kx(){ kubectl --kubeconfig "$KC" --request-timeout=30s "$@"; }

_operator_suspended=0
suspend_operator(){
  # the kommander-operator re-seeds git from the OCI artifact on reconcile — stop it first
  kx scale deploy kommander-operator -n kommander --replicas=0 >/dev/null
  _operator_suspended=1
  echo "[devloop] kommander-operator suspended (replicas=0) — git edits are now durable"
}
resume_operator(){
  [ "$_operator_suspended" = "1" ] || return 0
  kx scale deploy kommander-operator -n kommander --replicas=1 >/dev/null
  echo "[devloop] kommander-operator resumed — NOTE: its next OCI re-push may overwrite dev edits"
}

git_exec(){ kx exec "$GIT_POD" -n "$GIT_NS" -c "$GIT_CTR" -- sh -c "$1"; }
git_exec_stdin(){ kx exec -i "$GIT_POD" -n "$GIT_NS" -c "$GIT_CTR" -- sh -c "$1"; }

# git_apply <commit-msg> <script>  — run <script> inside a fresh clone at /tmp/devloop
# (cwd = repo root), then commit+push whatever changed. Script runs under `set -e`.
git_apply(){
  local msg="$1" script="$2"
  git_exec "
    set -e; export HOME=/tmp
    G=\"git -c user.name=devloop -c user.email=devloop@nkp -c safe.directory=*\"
    cd /tmp && rm -rf devloop && \$G clone -q $BARE devloop && cd devloop
    $script
    \$G add -A
    \$G diff --cached --quiet && { echo '[git] no changes to commit'; exit 0; }
    \$G commit -q -m \"$(printf '%s' "$msg")\" && \$G push -q origin main
    echo '[git] pushed'
  "
}

# git_push_dir <local-dir> <repo-rel-dest> <commit-msg>
# Replaces <repo-rel-dest> in the claim's git with the contents of <local-dir>
# (streamed via tar). Caller is responsible for any token substitution BEFORE calling.
git_push_dir(){
  local src="$1" dest="$2" msg="$3"
  # COPYFILE_DISABLE stops macOS bsdtar emitting AppleDouble "._<name>"
  # companions for every file carrying an extended attribute (anything
  # touched on APFS has com.apple.provenance). They land in the cluster's
  # git, and flux then parses ._foo.yaml as a manifest and fails the whole
  # kustomization: "MalformedYAMLError: control characters are not allowed".
  # Live-caught 2026-09-01 - it blocked istio-helm-pre-install and every
  # kustomization depending on it. Note `tar tz` will NOT show these: bsdtar
  # reassembles AppleDouble on read, so verify with a non-Apple reader.
  COPYFILE_DISABLE=1 tar -C "$src" --exclude '._*' -cz . | git_exec_stdin "
    set -e; export HOME=/tmp
    G=\"git -c user.name=devloop -c user.email=devloop@nkp -c safe.directory=*\"
    cd /tmp && rm -rf devloop && \$G clone -q $BARE devloop && cd devloop
    rm -rf '$dest' && mkdir -p '$dest' && tar -xz -C '$dest'
    \$G add -A
    \$G diff --cached --quiet && { echo '[git] no changes to commit'; exit 0; }
    \$G commit -q -m \"$(printf '%s' "$msg")\" && \$G push -q origin main
    echo '[git] pushed $dest'
  "
}

# trigger flux: GitRepository fetch + the app's own Kustomization (ns kommander, 6h interval)
trigger_app(){
  local app="$1" ts; ts=$(date +%s)
  kx annotate gitrepository management -n kommander-flux \
     reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null
  # per-app Kustomizations live in ns kommander; nudge exact match first, fall back to all
  if kx get kustomization "$app" -n kommander >/dev/null 2>&1; then
    kx annotate kustomization "$app" -n kommander reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null
  else
    kx annotate kustomization -n kommander --all reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null 2>&1
  fi
  echo "[devloop] flux triggered (gitrepository + kustomization/$app)"
}

# wait_hr <app> <timeout-s> — wait until the app's HelmRelease is Ready=True AND its
# observedGeneration caught up (a no-op reconcile also satisfies this; pair with your own check)
wait_hr(){
  local app="$1" to="${2:-300}" t=0 st
  while [ $t -lt "$to" ]; do
    st=$(kx get hr "$app" -n kommander -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    [ "$st" = "True" ] && { echo "[devloop] hr/$app Ready (t+${t}s)"; return 0; }
    sleep 5; t=$((t+5))
  done
  echo "[devloop] TIMEOUT waiting for hr/$app Ready" >&2; return 1
}

# reconcile_hr <app> <timeout-s> — force the HelmRelease to re-render NOW and wait until it
# actually handled this request (valuesFrom CM edits do NOT auto-trigger helm-controller —
# validated live: git+kustomization+CM all updated, deployment unchanged until this nudge).
reconcile_hr(){
  # An app renders ONE HelmRelease named after it, or SEVERAL named
  # <app>-<component> (istio-helm: base cni gateway istiod ztunnel). Poke every
  # one and wait for all under one deadline. None at all means the app is not
  # enabled yet; the content in git IS the delivery, so that is not a failure.
  # (2026-09-07: `annotate hr istio-helm` under set -e killed a delivery whose
  # git side had already succeeded.)
  local app="$1" to="${2:-300}" ts t=0 at st hrs h pending
  ts=$(date +%s)
  hrs=$(kx get hr -n kommander -o name 2>/dev/null | sed 's|.*/||' | grep -E "^${app}(-|$)" || true)
  if [ -z "$hrs" ]; then
    echo "[devloop] no HelmRelease named $app or $app-* (app not enabled yet) - content is in git, first render will be yours"
    return 0
  fi
  for h in $hrs; do
    kx annotate hr "$h" -n kommander reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null
  done
  while [ $t -lt "$to" ]; do
    pending=""
    for h in $hrs; do
      at=$(kx get hr "$h" -n kommander -o jsonpath='{.status.lastHandledReconcileAt}' 2>/dev/null)
      st=$(kx get hr "$h" -n kommander -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
      [ "$at" = "$ts" ] && [ "$st" = "True" ] || pending="$pending $h"
    done
    [ -z "$pending" ] && { echo "[devloop] re-rendered + Ready (t+${t}s):$(echo " $hrs" | tr '\n' ' ')"; return 0; }
    sleep 5; t=$((t+5))
  done
  echo "[devloop] TIMEOUT after ${to}s: not re-rendered/Ready:$pending (request $ts)" >&2; return 1
}

# find_app_file <name-regex> <kind> — locate an app's yaml inside the clone (prints repo-rel path)
# runs IN the git pod; usage embedded in git_apply scripts via: F=$(find_yaml 'name: reloader' HelmRelease)
