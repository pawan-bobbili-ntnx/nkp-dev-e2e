#!/bin/bash
# VECTOR: mesosphere/charts chart change -> claim, in ~4min, WITHOUT the 3-merge release path
# (charts merge -> manual ghcr mirror -> kommander-applications pin bump).
# Packages the chart from a local charts checkout, pushes it to an anonymous OCI registry
# (ttl.sh by default), then rewrites the app's OCIRepository url+tag in the claim's git.
#
#   devloop-chart.sh <claim-kubeconfig> <charts-dir> <stable|staging>/<chart> <app> [<dev-version>]
#   e.g. devloop-chart.sh qa-cd1.conf ~/Documents/nkp/release-2.18/charts staging/dex-k8s-authenticator dex-k8s-authenticator
#
# Requires: helm 3.8+ (OCI), network from cluster nodes to $DEVLOOP_REGISTRY (default ttl.sh).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); . "$HERE/_lib.sh"
KC="$1"; CHARTS="$2"; CHART_PATH="$3"; APP="$4"; DEVVER="${5:-}"
CHART_DIR="$CHARTS/$CHART_PATH"; CHART=$(basename "$CHART_PATH")
[ -f "$CHART_DIR/Chart.yaml" ] || { echo "FATAL: $CHART_DIR/Chart.yaml missing"; exit 1; }

BASEVER=$(awk '/^version:/{print $2; exit}' "$CHART_DIR/Chart.yaml")
[ -n "$DEVVER" ] || DEVVER="${BASEVER}-dev.$(date +%s)"
PULL_SECRET=""
case "$DEVLOOP_REGISTRY" in
  ghcr.io/*)
    # real-registry flow: helm login with the gh token; charts land under the configured
    # owner with the defined dev tag. Needs a token with write:packages
    # (grant once:  gh auth refresh -h github.com -s write:packages,read:packages ).
    # token preference: explicit GHCR_TOKEN (a packages-scoped PAT) > gh session token
    GHT="${GHCR_TOKEN:-$(gh auth token 2>/dev/null)}"
    GH_USER=$(curl -s -H "Authorization: token $GHT" https://api.github.com/user | sed -n 's/.*"login": *"\([^"]*\)".*/\1/p')
    [ -n "$GH_USER" ] || { echo "FATAL: could not resolve ghcr user from token"; exit 1; }
    printf '%s' "$GHT" | helm registry login ghcr.io -u "$GH_USER" --password-stdin >/dev/null \
      || { echo "FATAL: helm login to ghcr failed (token lacks packages scope?)"; exit 1; }
    OCI_NS="$DEVLOOP_REGISTRY"
    PULL_SECRET="ghcr-devloop"
    ;;
  *)
    NS_UUID=$(uuidgen | tr 'A-Z' 'a-z')
    OCI_NS="$DEVLOOP_REGISTRY/$NS_UUID"
    ;;
esac
echo "[devloop] chart=$CHART base=$BASEVER dev=$DEVVER -> oci://$OCI_NS/$CHART"

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
cp -R "$CHART_DIR" "$STAGE/$CHART"
# stamp the dev version (helm package --version also works but keep Chart.yaml authoritative)
sed -i '' -e "s|^version:.*|version: $DEVVER|" "$STAGE/$CHART/Chart.yaml" 2>/dev/null || \
sed -i    -e "s|^version:.*|version: $DEVVER|" "$STAGE/$CHART/Chart.yaml"
( cd "$STAGE" && helm package "$CHART" >/dev/null && helm push "$CHART-$DEVVER.tgz" "oci://$OCI_NS" )
echo "[devloop] chart pushed"
if [ -n "$PULL_SECRET" ]; then
  # ghcr packages are born PRIVATE (visibility is not settable via REST for containers) —
  # try the API best-effort, and ALWAYS wire a pull secret + OCIRepository secretRef so the
  # cluster can fetch either way.
  gh api -X PATCH "/user/packages/container/$(echo "$OCI_NS/$CHART" | cut -d/ -f2- | sed 's|/|%2F|g')" \
     -f visibility=public >/dev/null 2>&1 && echo "[devloop] package set public" \
     || echo "[devloop] package stays private — cluster will pull via secretRef ($PULL_SECRET)"
  kx create secret docker-registry "$PULL_SECRET" -n kommander \
     --docker-server=ghcr.io --docker-username="$GH_USER" --docker-password="$GHT" \
     --dry-run=client -o yaml | kx apply -f - >/dev/null
  echo "[devloop] pull secret $PULL_SECRET ensured in ns kommander"
fi

suspend_operator; trap 'resume_operator; rm -rf "$STAGE"' EXIT
T0=$(date +%s)
# rewrite the app's OCIRepository (url + ref.tag) inside the claim's git
git_apply "devloop: point $APP chart at oci://$OCI_NS/$CHART:$DEVVER" "
  # scope to the app's OWN directory — a global name-grep matched dependsOn lines of OTHER
  # apps (live: chart dex edited dex-k8s-authenticator.yaml via its dependsOn: dex)
  F=\$(grep -rl 'kind: OCIRepository' applications/$APP/ 2>/dev/null | grep '\.yaml\$' | head -1)
  [ -n \"\$F\" ] || { echo NO-OCIREPOSITORY-FOR-$APP; exit 1; }
  echo \"[git] editing \$F\"
  # url may be quoted or bare in the operator-rendered file — match both
  sed -i \"s|url: .*oci://.*|url: \\\"oci://$OCI_NS/$CHART\\\"|\" \"\$F\"
  sed -i \"/ref:/,/tag:/ s|tag: .*|tag: \\\"$DEVVER\\\"|\" \"\$F\"
  if [ -n \"$PULL_SECRET\" ] && ! grep -q secretRef \"\$F\"; then
    sed -i \"s|^  url: |  secretRef:\\n    name: $PULL_SECRET\\n  url: |\" \"\$F\"
  fi
"
trigger_app "$APP"
# the chart OCIRepository object is named <app>-<appVersion>-chart — read it off the HR
OCIREPO=$(kx get hr "$APP" -n kommander -o jsonpath='{.spec.chartRef.name}' 2>/dev/null)
# chart swap => OCIRepository refetch => HR upgrade; wait for the HR to report the dev version
t=0; ok=""
while [ $t -lt 360 ]; do
  V=$(kx get hr "$APP" -n kommander -o jsonpath='{.status.history[0].chartVersion}' 2>/dev/null)
  if [ "$V" = "$DEVVER" ]; then ok=1; break; fi
  if [ $((t%30)) -eq 0 ]; then
    [ -n "$OCIREPO" ] && kx annotate ocirepository "$OCIREPO" -n kommander reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite >/dev/null 2>&1 || true
    kx annotate hr "$APP" -n kommander reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite >/dev/null 2>&1 || true
  fi
  sleep 6; t=$((t+6))
done
[ -n "$ok" ] || { echo "[devloop] TIMEOUT: hr/$APP never reported chartVersion=$DEVVER (last: $V)"; exit 2; }
wait_hr "$APP" 120
echo "[devloop] DONE in $(( $(date +%s)-T0 ))s — hr/$APP now runs your dev chart $DEVVER"
echo "[devloop] ttl.sh artifacts expire in 24h — bake or re-push for longer sessions"
