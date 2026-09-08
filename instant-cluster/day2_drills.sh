#!/bin/bash
# DAY-2 PARITY DRILLS — executes real day-2 operations on a claimed cluster.
# Usage: day2_drills.sh <kubeconfig> <phase>
# Phases: helm | appdeploy | certs | scaleout | scalein | mdelete | cproll
set -u
KC="$1"; PHASE="$2"
k(){ kubectl --kubeconfig "$KC" --request-timeout=30s "$@"; }
case "$PHASE" in
helm)
  # HELM OWNERSHIP: every release's storage must decode (gzip json), carry a manifest, and its
  # history must be sane — this is the direct integrity check on the payloads the claim rewrote.
  k get secrets -A -o json | python3 -c '
import json,sys,base64,gzip,collections
d=json.load(sys.stdin); rel=collections.defaultdict(list); bad=[]
for i in d["items"]:
    if i.get("type")!="helm.sh/release.v1": continue
    name=i["metadata"]["name"]
    try:
        blob=gzip.decompress(base64.b64decode(base64.b64decode(i["data"]["release"])))
        r=json.loads(blob)
        assert r.get("manifest") and r.get("chart") and r.get("info",{}).get("status")
        rel[r["name"]].append((r["version"],r["info"]["status"]))
    except Exception as e:
        bad.append((name,str(e)[:40]))
for n,vs in sorted(rel.items()):
    vs.sort()
    dep=[v for v,s in vs if s=="deployed"]
    print("  %-28s revisions=%d deployed=%s latest=%s" % (n,len(vs),len(dep),vs[-1]))
print("HELM-RESULT releases=%d corrupt=%d %s" % (len(rel),len(bad),bad[:3] if bad else ""))'
  ;;
appdeploy)
  # APP LIFECYCLE via the designed override hook: set a pod label through <app>-overrides,
  # verify it reaches the running pod, then revert.
  k create cm reloader-overrides -n kommander --from-literal=values.yaml='podLabels: {parity-drill: "1"}' \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k patch appdeployment reloader -n kommander --type=merge \
    -p '{"spec":{"configOverrides":{"name":"reloader-overrides"}}}' >/dev/null
  k annotate kustomization reloader -n kommander reconcile.fluxcd.io/requestedAt="drill-$(date +%s)" --overwrite >/dev/null
  for i in $(seq 1 30); do
    L=$(k get pods -n kommander -l app=reloader-reloader -o jsonpath='{.items[0].metadata.labels.parity-drill}' 2>/dev/null)
    [ -z "$L" ] && L=$(k get pods -n kommander -l parity-drill=1 -o name 2>/dev/null | head -1)
    [ -n "$L" ] && { echo "APPDEPLOY-RESULT override reached pod at +$((i*10))s"; break; }
    sleep 10
  done
  [ -n "$L" ] || echo "APPDEPLOY-RESULT TIMEOUT"
  k patch appdeployment reloader -n kommander --type=json -p '[{"op":"remove","path":"/spec/configOverrides"}]' >/dev/null 2>&1
  k delete cm reloader-overrides -n kommander >/dev/null 2>&1
  ;;
certs)
  IP=$(k get nodes -o jsonpath='{.items[?(@.metadata.labels.node-role\.kubernetes\.io/control-plane=="")].status.addresses[?(@.type=="InternalIP")].address}' | awk '{print $1}')
  ssh -i ~/.ssh/nkp_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    konvoy@$IP 'sudo kubeadm certs check-expiration 2>/dev/null | grep -E "^[a-z]" | head -12'
  echo "CERTS-RESULT rc=$?"
  ;;
scaleout)
  CUR=$(k get cluster -n kommander -o jsonpath='{.items[0].spec.topology.workers.machineDeployments[0].replicas}')
  k patch cluster -n kommander qa-nrm1 --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/topology/workers/machineDeployments/0/replicas\",\"value\":$((CUR+1))}]"
  echo "scale $CUR -> $((CUR+1)) requested; waiting for new machine Running + node Ready"
  for i in $(seq 1 60); do
    RUN=$(k get machines -n kommander --no-headers 2>/dev/null | grep -c Running)
    NODES=$(k get nodes --no-headers 2>/dev/null | grep -cw Ready)
    echo "  t=$((i*15))s machines-Running=$RUN nodes-Ready=$NODES"
    [ "$RUN" = "$((CUR+2))" ] && [ "$NODES" = "$((CUR+2))" ] && { echo "SCALEOUT-RESULT OK at t=$((i*15))s"; exit 0; }
    sleep 15
  done
  echo "SCALEOUT-RESULT TIMEOUT"
  ;;
scalein)
  CUR=$(k get cluster -n kommander -o jsonpath='{.items[0].spec.topology.workers.machineDeployments[0].replicas}')
  k patch cluster -n kommander qa-nrm1 --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/topology/workers/machineDeployments/0/replicas\",\"value\":$((CUR-1))}]"
  for i in $(seq 1 60); do
    TOT=$(k get machines -n kommander --no-headers 2>/dev/null | wc -l | tr -d ' ')
    [ "$TOT" = "$CUR" ] && { echo "SCALEIN-RESULT OK at t=$((i*15))s (machine+VM deleted)"; exit 0; }
    sleep 15
  done
  echo "SCALEIN-RESULT TIMEOUT"
  ;;
mdelete)
  M=$(k get machines -n kommander --no-headers | grep -v control-plane | grep md- | head -1 | awk '{print $1}')
  [ -n "$M" ] || M=$(k get machines -n kommander --no-headers | tail -1 | awk '{print $1}')
  echo "deleting machine $M (MachineSet must replace it)"
  k delete machine "$M" -n kommander --wait=false
  for i in $(seq 1 70); do
    RUN=$(k get machines -n kommander --no-headers 2>/dev/null | grep -c Running)
    TOT=$(k get machines -n kommander --no-headers 2>/dev/null | wc -l | tr -d ' ')
    echo "  t=$((i*15))s machines=$TOT running=$RUN"
    [ "$RUN" = "4" ] && [ "$TOT" = "4" ] && ! k get machine "$M" -n kommander >/dev/null 2>&1 && \
      { echo "MDELETE-RESULT OK at t=$((i*15))s (replaced)"; exit 0; }
    sleep 15
  done
  echo "MDELETE-RESULT TIMEOUT"
  ;;
cproll)
  k patch kubeadmcontrolplane -n kommander --type=merge \
    "$(k get kcp -n kommander -o jsonpath='{.items[0].metadata.name}')" \
    -p "{\"spec\":{\"rolloutAfter\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
  echo "CP roll requested (single-CP surge: new CP joins, old removed)"
  OLD=$(k get machines -n kommander --no-headers | grep -v md- | awk '{print $1}')
  for i in $(seq 1 90); do
    CPS=$(k get machines -n kommander --no-headers 2>/dev/null | grep -vc md-)
    RUN=$(k get machines -n kommander --no-headers 2>/dev/null | grep -v md- | grep -c Running)
    HAVEOLD=$(k get machine "$OLD" -n kommander >/dev/null 2>&1 && echo yes || echo no)
    echo "  t=$((i*20))s cp-machines=$CPS running=$RUN old-present=$HAVEOLD"
    [ "$CPS" = "1" ] && [ "$RUN" = "1" ] && [ "$HAVEOLD" = "no" ] && { echo "CPROLL-RESULT OK at t=$((i*20))s"; exit 0; }
    sleep 20
  done
  echo "CPROLL-RESULT TIMEOUT"
  ;;
esac
