#!/bin/bash
# VECTOR (experimental): product-catalog change -> claim, in ~2min.
# Builds the catalog collection artifact from a local nkp-nutanix-product-catalog checkout,
# pushes it to an anonymous OCI registry, then points the claim's catalog at it via the
# kommander app's *-overrides ConfigMap (the designed hook — survives reconciles; the
# federation DefaultCatalogCollectionHandler re-renders the OCIRepository from these values).
#
#   devloop-catalog.sh <claim-kubeconfig> <catalog-dir> [<release-spec>]
#   e.g. devloop-catalog.sh qa-cd1.conf ~/Documents/nkp/release-2.18/catalog
#   revert: devloop-catalog.sh <claim-kubeconfig> revert
#
# Requires: the nkp binary (NKP_BIN, default ~/Documents/nkp/nkp) with `experimental catalog`.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); . "$HERE/_lib.sh"
KC="$1"
NKP="${NKP_BIN:-$HOME/Documents/nkp/nkp}"

if [ "${2:-}" = "revert" ] || [ "${2:-}" = "" ] && [ "${2:-x}" = "revert" ]; then :; fi
if [ "${2:-}" = "revert" ]; then
  kx delete cm kommander-overrides -n kommander --ignore-not-found
  echo "[devloop] kommander-overrides removed — catalog reverts to release collection on next reconcile"
  exit 0
fi

CATDIR="$2"; SPEC="${3:-$CATDIR/.release/stable.yaml}"
[ -f "$SPEC" ] || { echo "FATAL: release spec $SPEC missing"; exit 1; }
NS_UUID=$(uuidgen | tr 'A-Z' 'a-z')
DEST="$DEVLOOP_REGISTRY/$NS_UUID"
TAG=$(awk '/tagName:/{print $2; exit}' "$SPEC" | tr -d '"')
echo "[devloop] building collection from $SPEC (tag $TAG) -> $DEST"
( cd "$CATDIR" && "$NKP" experimental catalog release --release-spec "$SPEC" --push-to-registry "$DEST" )

# override the collection URI via the kommander app overrides CM (discover exact key path
# from the live defaults CM so this survives schema drift)
DEFCM=$(kx get cm -n kommander -o name | grep -E 'kommander-[0-9].*-config-defaults' | head -1)
[ -n "$DEFCM" ] || { echo "FATAL: kommander config-defaults CM not found"; exit 1; }
kx get "$DEFCM" -n kommander -o jsonpath='{.data.values\.yaml}' > /tmp/devloop-kdef.yaml
python3 - "$DEST" "$TAG" <<'PY' > /tmp/devloop-kover.yaml
import sys, yaml
dest, tag = sys.argv[1], sys.argv[2]
vals = yaml.safe_load(open("/tmp/devloop-kdef.yaml"))
def find(node, path=()):
    if isinstance(node, list):
        for i in node:
            if isinstance(i, dict) and "resourceURI" in i and "nkp-nutanix-product-catalog" in str(i.get("resourceURI","")):
                return path, node
    if isinstance(node, dict):
        for k, v in node.items():
            r = find(v, path + (k,))
            if r: return r
    return None
hit = find(vals)
assert hit, "catalog collection entry not found in kommander defaults"
path, lst = hit
new = [dict(i, resourceURI=f"oci://{dest}/nkp-nutanix-product-catalog/collection", tag=tag)
       if isinstance(i, dict) and "nkp-nutanix-product-catalog" in str(i.get("resourceURI","")) else i
       for i in lst]
out = cur = {}
for k in path[:-1]: cur[k] = {}; cur = cur[k]
cur[path[-1]] = new
print(yaml.safe_dump(out, default_flow_style=False))
PY
kx create cm kommander-overrides -n kommander --from-file=values.yaml=/tmp/devloop-kover.yaml \
   --dry-run=client -o yaml | kx apply -f - >/dev/null
kx annotate hr kommander -n kommander reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite >/dev/null
echo "[devloop] overrides applied; waiting for catalog OCIRepository to repoint"
t=0
while [ $t -lt 300 ]; do
  URL=$(kx get ocirepository nkp-nutanix-product-catalog -n kommander -o jsonpath='{.spec.url}' 2>/dev/null)
  case "$URL" in *"$NS_UUID"*) echo "[devloop] catalog now served from $URL"; exit 0;; esac
  sleep 10; t=$((t+10))
done
echo "[devloop] TIMEOUT: catalog OCIRepository still at $URL"; exit 2
