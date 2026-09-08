#!/bin/bash
# VECTOR: controller/app image change -> claim, in ~3min.
# Thin wrapper over the validated override_image.py (3 strategies: app-overrides CM /
# HelmChartProxy / direct Deployment patch), adding an optional local-image push leg so a
# `docker build`t image on the laptop can reach the cluster without a private registry.
#
#   devloop-image.sh <claim-kubeconfig> <component>=<image-ref>          # image already pullable
#   devloop-image.sh <claim-kubeconfig> <component> --local <docker-tag> # push local image to ttl.sh first
#   devloop-image.sh <claim-kubeconfig> revert                           # undo everything
#
# component names + sub-image targeting (component@subimage=) are override_image.py's;
# run `override_image.py <kubeconfig> images <component>` to list.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); . "$HERE/_lib.sh"
KC="$1"; shift
OVR="$HERE/../override_image.py"
[ -f "$OVR" ] || OVR="$(dirname "$HERE")/override_image.py"
[ -f "$OVR" ] || { echo "FATAL: override_image.py not found next to devloop/"; exit 1; }

if [ "${1:-}" = "revert" ]; then exec python3 "$OVR" "$KC" revert; fi

if [ "${2:-}" = "--local" ]; then
  COMP="$1"; LOCAL="$3"
  NS_UUID=$(uuidgen | tr 'A-Z' 'a-z')
  REF="$DEVLOOP_REGISTRY/$NS_UUID/${COMP//@/-}:dev"
  echo "[devloop] pushing local image $LOCAL -> $REF"
  docker tag "$LOCAL" "$REF" && docker push "$REF" >/dev/null
  exec python3 "$OVR" "$KC" apply "$COMP=$REF"
fi
exec python3 "$OVR" "$KC" apply "$@"
