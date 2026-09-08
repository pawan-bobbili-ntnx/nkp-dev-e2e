#!/bin/bash
# VECTOR: kommander-applications content change -> claim, in ~30s.
# Syncs ONE app's version dir from a local applications tree (the separate
# kommander-applications repo, or kommander/kommander-applications on main) checkout into the claim's
# own kommander.git (the only thing flux reconciles), with ${token} substitution matching
# what the kommander-operator would have rendered.
#
#   devloop-kapps.sh <claim-kubeconfig> <local-kapps-dir> <app> [<version-dir>]
#   e.g. devloop-kapps.sh qa-cd1.conf ~/Documents/nkp/release-2.18/kommander-applications reloader
#
# Traps handled here (validated live):
#   - HR metadata.labels CANNOT carry your change (AppDeployment json-patch replaces the map)
#   - kommander-operator must be suspended or it re-pushes OCI content over your edit
#   - the app's flux Kustomization (ns kommander) has a 6h interval -> explicit nudge
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); . "$HERE/_lib.sh"
KC="$1"; KAPPS="$2"; APP="$3"; VER="${4:-}"

# --common <dir>: sync a common/<dir> tree (operator manifests/chart) instead of an app.
# Content goes VERBATIM (flux postBuild resolves ${var:=default} tokens at apply time —
# unlike applications/, whose ${releaseName} family is pre-rendered into the claim git).
if [ "$APP" = "--common" ]; then
  CDIR="${4:?--common needs a dir name}"
  SRC="$KAPPS/common/$CDIR"
  [ -d "$SRC" ] || { echo "FATAL: $SRC missing"; exit 1; }
  suspend_operator; trap resume_operator EXIT
  T0=$(date +%s)
  git_push_dir "$SRC" "common/$CDIR" "devloop: sync common/$CDIR from local checkout"
  ts=$(date +%s)
  kx annotate gitrepository management -n kommander-flux reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null
  kx annotate kustomization -n kommander --all reconcile.fluxcd.io/requestedAt="$ts" --overwrite >/dev/null 2>&1 || true
  echo "[devloop] common/$CDIR pushed + operator kustomizations triggered ($(( $(date +%s)-T0 ))s)"
  echo "[devloop] verify the affected operator deploys rolled (flux applies within its interval)"
  exit 0
fi

[ -d "$KAPPS/applications/$APP" ] || { echo "FATAL: $KAPPS/applications/$APP missing"; exit 1; }
# version dirs are numeric (an app dir may hold non-version dirs like 'charts')
[ -n "$VER" ] || VER=$(ls "$KAPPS/applications/$APP" | grep -E '^[0-9]' | sort -V | tail -1)
SRC="$KAPPS/applications/$APP/$VER"
[ -d "$SRC" ] || { echo "FATAL: $SRC missing"; exit 1; }
echo "[devloop] app=$APP version=$VER src=$SRC"

# discover where this app lives in the claim's git (layout is operator-written; find, don't assume)
DEST=$(git_exec "
  export HOME=/tmp
  G=\"git -c safe.directory=*\"
  cd /tmp && rm -rf probe && \$G clone -q $BARE probe && cd probe
  find . -type d -path '*/$APP/*' -name helmrelease | head -1 | sed 's|^\./||; s|/helmrelease\$||'
" | tail -1)
[ -n "$DEST" ] || { echo "FATAL: could not locate app dir for $APP in claim git"; exit 1; }
echo "[devloop] claim-git dest: $DEST"

# render tokens the way the operator does: ${releaseName}=<app>, ${appVersion}=<ver-dir>,
# ${releaseNamespace}=kommander  (rendering happens laptop-side; git gets final content)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp -R "$SRC/." "$STAGE/"
LC_ALL=C find "$STAGE" -type f \( -name '*.yaml' -o -name '*.yml' \) | while read -r f; do
  sed -i '' -e "s|\${releaseName}|$APP|g" -e "s|\${appVersion}|$VER|g" \
            -e "s|\${releaseNamespace}|kommander|g" "$f" 2>/dev/null || \
  sed -i    -e "s|\${releaseName}|$APP|g" -e "s|\${appVersion}|$VER|g" \
            -e "s|\${releaseNamespace}|kommander|g" "$f"
done

suspend_operator; trap 'resume_operator; rm -rf "$STAGE"' EXIT
T0=$(date +%s)
git_push_dir "$STAGE" "$DEST" "devloop: sync $APP/$VER from local checkout"
trigger_app "$APP"
# wait for the app kustomization to apply the new git revision, then force the HR re-render
# (helm-controller does not watch valuesFrom CMs)
sleep 5
reconcile_hr "$APP" 300
echo "[devloop] DONE in $(( $(date +%s)-T0 ))s — verify your change, then re-run or bake"
echo "[devloop] NOTE: operator resumes on exit; keep it at 0 manually for a longer session:"
echo "          kubectl --kubeconfig $KC scale deploy kommander-operator -n kommander --replicas=0"
