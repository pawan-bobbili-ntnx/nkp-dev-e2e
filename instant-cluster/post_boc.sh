#!/bin/bash
# post-boot finisher (auto-launched by boc3cp): git-verify FIRST (earliest SSO fix), then
# kube-vip HA restore, MHC unpause (gated on machines Running), LB announcer sanity.
SCR="$(cd "$(dirname "$0")" && pwd)"

# ---- TIMESTAMP EVERY LINE (2026-07-26) -----------------------------------------------------
# post_boc's log had NO timing at all, which made its ~450s pre-scale-up block unmeasurable — the
# git pod cannot be created until this script gets through the CSI-readiness work, and that is now
# the dominant term in the tail. Every line is prefixed with seconds since post_boc started, so the
# block can be decomposed instead of guessed at. (macOS awk has no systime(); use python.)
exec > >(python3 -u -c "
import sys,time
t0=time.time()
for line in sys.stdin:
    sys.stdout.write('[+%4ds] %s' % (time.time()-t0, line)); sys.stdout.flush()
") 2>&1
trap 'sleep 2' EXIT   # let the filter drain before the script exits

# NAME comes from boc3cp's env (per-claim, concurrency-safe); fall back to legacy map if unset
NAME="${NAME:-$(python3 -c "import json;print(json.load(open('$SCR/claim-map.json'))['name'])" 2>/dev/null)}"
MAP="$SCR/claim-map-$NAME.json"
KC="$SCR/$NAME-claim.conf"
LB=$(python3 -c "import json;print(json.load(open('$MAP'))['lb_start'])" 2>/dev/null)
# Topology, resolved ONCE here and reused (LEVER-A pin, right-sizing). It used to be derived only
# just before right-sizing, several hundred lines down.
CPN=$(python3 -c "import json;m=json.load(open('$MAP'));print(len(m['cps']))" 2>/dev/null)
WKN=$(python3 -c "import json;m=json.load(open('$MAP'));print(len(m.get('workers') or []))" 2>/dev/null)

# 0. VA SWEEP (the ONLY placement that reliably works — boc9/10 showed in-window deletes
# race the keyspace state; post-boot, deleting VAs forces the attacher to do REAL attaches)
# STORAGE BRING-UP (root-cause fix, bc14): the window held git/helm STS at 0 so no stale VA
# exists. Wait for the CSI attacher+node plugins to be Running, then scale the storage STS up
# → KCM mints FRESH VolumeAttachments → attacher does real ControllerPublish → clean attach.
( # LATENCY (2026-07-26): this stage was gated by fixed sleeps (10s poll + 45s + 60s = ~105s of
  # unconditional waiting). Measured across 5 runs, the time for the git pod to reach Running is
  # the DOMINANT remaining term (221-522s) while the wrinkle-2 churn itself is constant
  # (foreign_detaches == 3 in every run) — i.e. most of it is polling cadence, not real work.
  # Every fixed sleep below is replaced by a wait on the actual condition it was approximating.
  for i in $(seq 1 90); do
    csi=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pods -n ntnx-system -l app=nutanix-csi-controller --no-headers 2>/dev/null | grep -c "Running")
    nod=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pods -n ntnx-system -l app=nutanix-csi-node --no-headers 2>/dev/null | grep -cE "[0-9]/[0-9] +Running")
    [ "$csi" -ge 1 ] 2>/dev/null && [ "$nod" -ge 1 ] 2>/dev/null && break
    sleep 3
  done
  # ROOT FIX (bc14): the CSI controller/attacher carries STALE leader-lease + in-memory VA
  # state from the freeze snapshot -> it marks VAs attached=true WITHOUT a real ControllerPublish
  # -> restored VG never attaches. Clear the stale CSI leases + bounce the controller so it
  # re-elects fresh and does REAL attaches. (Verified: 0 -> 6 VGs attached after this.)
  kubectl --kubeconfig "$KC" --request-timeout=20s delete lease -n ntnx-system --all 2>/dev/null
  kubectl --kubeconfig "$KC" --request-timeout=20s delete pod -n ntnx-system -l app=nutanix-csi-controller --wait=false 2>/dev/null
  # was: sleep 45 (guessing how long a fresh leader election takes). Wait for the ACTUAL condition:
  # a csi-controller pod fully Ready again AND the attacher lease re-created by the new leader.
  # NOTE: an earlier version used awk '$2 ~ /^([0-9]+)\/\1$/' to test "all containers ready".
  # POSIX ERE has NO backreferences, so that pattern never matched and this loop burned its full
  # 120s ceiling — WORSE than the 45s sleep it replaced. Use kubectl's own readiness wait.
  # `kubectl wait --for=condition=Ready pod -l ...` RACES the pods we just deleted: it selects the
  # TERMINATING pods, which never become Ready, so it always burned the full timeout (measured on
  # bcf2: "not Ready within 120s" = 120s wasted, worse than the 45s sleep). `rollout status` tracks
  # the Deployment's replacement pods, which is the condition we actually mean.
  kubectl --kubeconfig "$KC" --request-timeout=130s rollout status deploy/nutanix-csi-controller \
    -n ntnx-system --timeout=120s >/dev/null 2>&1 \
    && echo "  csi-controller rollout complete (was a flat 45s sleep)" \
    || echo "  WARN: csi-controller rollout not complete within 120s — continuing"
  for i in $(seq 1 30); do
    lease=$(kubectl --kubeconfig "$KC" --request-timeout=15s get lease -n ntnx-system --no-headers 2>/dev/null | grep -c 'external-attacher-leader')
    [ "$lease" -ge 1 ] 2>/dev/null && { echo "  attacher lease re-created after ${i}x2s"; break; }
    sleep 2
  done
  # WRINKLE-2 BACKSTOP (bc20): an RP restore attaches the new VG to the RP's SOURCE VM (the
  # template node). boc3cp detaches it at restore time, but that attachment can materialize a
  # few seconds LATE and slip past. Here — with the cluster up and the claim's own VM UUIDs
  # known — sweep every claim PV's VG and detach any VM that is NOT one of ours; otherwise CSI's
  # ControllerPublish task fails ("device symlink not found; VM reattach also failed") and the
  # git pod hangs in Init forever.
  ( python3 - "$KC" "$MAP" <<'PYSWEEP' 2>/dev/null
import json,os,subprocess,sys,uuid
KC,MAPF=sys.argv[1],sys.argv[2]
U=os.environ.get("NKP_NUTANIX_USER"); P=os.environ.get("NKP_NUTANIX_PASSWORD")
PC=os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required")
if not (U and P): print("  wrinkle2-sweep: no PC creds in env — SKIPPED"); sys.exit(0)
M=json.load(open(MAPF)); mine={n["clone_uuid"] for n in M["cps"]+M.get("workers",[])}
def pc(m,u,b=None):
    a=["curl","-sk","--max-time","45","-u","%s:%s"%(U,P),"-X",m,u,"-H","Content-Type: application/json","-H","NTNX-Request-Id: "+str(uuid.uuid4())]
    if b is not None: a+=["-d",json.dumps(b)]
    try: return json.loads(subprocess.run(a,capture_output=True,text=True).stdout or "{}")
    except Exception: return {}
import time
pv=subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=25s","get","pv","-o",
    "jsonpath={range .items[*]}{.spec.csi.volumeHandle}{\"\\n\"}{end}"],capture_output=True,text=True).stdout
vgs=sorted(set(x.strip().replace("NutanixVolumes-","") for x in pv.splitlines() if "NutanixVolumes-" in x))
# LOOP, don't single-shot (bc20/bc21): the source-VM attachment can land MINUTES after the restore
# task succeeds — well past boc3cp's 60s poll and past a one-shot sweep here (bc21 swept "0
# cleared" and was still wedged 7 min later). Keep sweeping until we see 3 consecutive clean
# passes, so whenever it lands it gets cleared and CSI's ControllerPublish can succeed.
total, clean = 0, 0
for rnd in range(60):                      # ~8 min ceiling at the tighter 8s cadence
    found = 0
    for vg in vgs:
        d = pc("GET", PC+"/api/volumes/v4.0/config/volume-groups/%s/vm-attachments"%vg).get("data")
        for a in (d if isinstance(d, list) else []):
            if a.get("extId") not in mine:
                pc("POST", PC+"/api/volumes/v4.0/config/volume-groups/%s/$actions/detach-vm"%vg, {"extId": a["extId"]})
                print("  wrinkle2-sweep: detached %s from FOREIGN vm %s"%(vg[:8], a["extId"][:8]))
                found += 1
    total += found
    clean = clean+1 if found == 0 else 0
    if clean >= 3 and rnd >= 3: break
    time.sleep(8)
print("  wrinkle2-sweep: %d foreign attachment(s) cleared over %d rounds"%(total, rnd+1))
PYSWEEP
  ) &   # BACKGROUND again (2026-07-26, second revision)
  # HISTORY (do not re-flip this without measuring): making it SYNCHRONOUS did cut the mount gap
  # 279s -> 88s, but it delayed pod CREATION from 99s to 369s (the CSI-ready poll, csi bounce,
  # `rollout status`, the sweep itself and the VA purge all sit in front of the scale-up), so
  # git_up got WORSE: 397s -> 464s (bck1). It moved the wait instead of removing it.
  # With LEVER-A the wait is genuinely removed: boc3cp leaves both git VGs ATTACHED to a chosen
  # worker and we pin the pod there, so the first mount succeeds regardless of sweep timing.
  # The sweep is therefore back to being a pure background backstop for foreign attachments —
  # it never touches OUR attachment, because that worker's uuid is in `mine`.
  # purge any lingering VA (belt), then scale storage consumers up for a fresh attach
  for va in $(kubectl --kubeconfig "$KC" --request-timeout=20s get volumeattachments -o name 2>/dev/null); do
    n="${va##*/}"
    kubectl --kubeconfig "$KC" --request-timeout=20s patch volumeattachment "$n" --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]' 2>/dev/null
    kubectl --kubeconfig "$KC" --request-timeout=20s delete volumeattachment "$n" --wait=false 2>/dev/null
  done
  # BACKSTOP: the attachment can still land late (bc21). Re-run the same sweep in the background
  # now, so anything appearing after the synchronous pass is still cleared — but the consumer pods
  # below are created only AFTER the synchronous pass above reported clean.
  ( python3 - "$KC" "$MAP" <<'PYSWEEP2' 2>/dev/null &
import json,os,subprocess,sys,uuid,time
KC,MAPF=sys.argv[1],sys.argv[2]
U=os.environ.get("NKP_NUTANIX_USER"); P=os.environ.get("NKP_NUTANIX_PASSWORD")
PC=os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required")
if not (U and P): sys.exit(0)
M=json.load(open(MAPF)); mine={n["clone_uuid"] for n in M["cps"]+M.get("workers",[])}
def pc(m,u,b=None):
    a=["curl","-sk","--max-time","45","-u","%s:%s"%(U,P),"-X",m,u,"-H","Content-Type: application/json","-H","NTNX-Request-Id: "+str(uuid.uuid4())]
    if b is not None: a+=["-d",json.dumps(b)]
    try: return json.loads(subprocess.run(a,capture_output=True,text=True).stdout or "{}")
    except Exception: return {}
pv=subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=25s","get","pv","-o",
    "jsonpath={range .items[*]}{.spec.csi.volumeHandle}{\"\\n\"}{end}"],capture_output=True,text=True).stdout
vgs=sorted(set(x.strip().replace("NutanixVolumes-","") for x in pv.splitlines() if "NutanixVolumes-" in x))
n=0
for rnd in range(40):
    for vg in vgs:
        d = pc("GET", PC+"/api/volumes/v4.0/config/volume-groups/%s/vm-attachments"%vg).get("data")
        for a in (d if isinstance(d, list) else []):
            if a.get("extId") not in mine:
                pc("POST", PC+"/api/volumes/v4.0/config/volume-groups/%s/$actions/detach-vm"%vg, {"extId": a["extId"]})
                print("  wrinkle2-backstop: detached %s from FOREIGN vm %s"%(vg[:8], a["extId"][:8])); n+=1
    time.sleep(10)
print("  wrinkle2-backstop: %d late attachment(s) cleared"%n)
PYSWEEP2
  ) 2>/dev/null
  # ★ LEVER-A: pin git-operator-git-0 to the node whose VGs boc3cp left ATTACHED ★
  # Both git and admin VGs are already attached to that worker, so NodeStage finds the devices
  # present and the FIRST mount succeeds — no ControllerPublish race, no kubelet backoff.
  # The nodeSelector only has to survive until the pod is placed; flux may later reconcile it away,
  # which is harmless (a running pod is not rescheduled by a selector change).
  PIN=$(python3 -c "import json;print(json.load(open('$SCR/$NAME-gitpin.json'))['node'])" 2>/dev/null)
  # SINGLE NODE: do not pin. There is exactly one schedulable node, so the nodeSelector cannot change
  # placement — but patching the StatefulSet is NOT free. flux owns it, so the suspend/patch/resume
  # cycle makes flux revert the spec and the StatefulSet recreates the pod.
  # MEASURED (qa-sn-c7/c8, 2026-07-28): the STS was scaled to 1 at +74s, yet git-operator-git-0 was
  # not CREATED until +301s / +198s after node-Ready (STS generation had churned to 7). The pod
  # itself is ready 18s after creation — so essentially the whole SSO tail was waiting on a pod
  # recreation caused by a pin that could not have mattered. The mount guarantee is unaffected:
  # claim.py already leaves both git VGs attached to cp0, which is that one node.
  if [ "$CPN" = "1" ] && [ "$WKN" = "0" ]; then
    echo "  LEVER-A: single-node topology — skipping the pin (one schedulable node; patching the"
    echo "           StatefulSet would only make flux revert it and recreate the pod)"
    PIN=""
  elif [ -n "$PIN" ]; then
    # flux OWNS this StatefulSet and FIX-7 resumed reconciliation back in the window, so a bare
    # patch is reverted before the pod is even created (measured bcm1: pinned to ...b5jq9, pod ran
    # on ...hsn6n, and CSI then moved both VGs across — the exact cross-attach this lever exists to
    # prevent). Suspend the owning kustomization while the pod is placed, then resume: the
    # nodeSelector only needs to survive until scheduling, not forever.
    kubectl --kubeconfig "$KC" --request-timeout=20s patch kustomization git-operator -n kommander \
      --type=merge -p '{"spec":{"suspend":true}}' >/dev/null 2>&1
    kubectl --kubeconfig "$KC" --request-timeout=20s patch statefulset git-operator-git -n git-operator-system \
      --type=merge -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"$PIN\"}}}}}" >/dev/null 2>&1 \
      && echo "  LEVER-A: git-operator pinned to $PIN (flux suspended for placement)" \
      || echo "  WARN: could not pin git-operator to $PIN"
  else
    echo "  WARN: no gitpin file — git-operator will schedule freely (expect the old attach race)"
  fi
  kubectl --kubeconfig "$KC" --request-timeout=20s scale statefulset git-operator-git -n git-operator-system --replicas=1 2>/dev/null
  kubectl --kubeconfig "$KC" --request-timeout=20s scale deploy helm-repository -n caren-system --replicas=1 2>/dev/null
  # was: sleep 60 (guessing how long ControllerPublish takes). Wait for the REAL signal — every
  # VolumeAttachment reporting attached=true — then bounce the consumers so kubelet retries
  # NodeStage against an already-attached VG. If the git pod happens to come up healthy on its own
  # (attach landed before its first mount), skip the bounce entirely: bouncing a Running git pod
  # only costs another ~40-60s of init.
  # EXPECTED count matters: an earlier version waited for "every EXISTING VA attached", which was
  # trivially true when only 1 of 3 VAs had been created yet — it exited after 6s having waited for
  # nothing. Derive the expected count from the cluster's Nutanix CSI PVs and require that many.
  want_va=$(kubectl --kubeconfig "$KC" --request-timeout=20s get pv -o jsonpath='{range .items[*]}{.spec.csi.volumeHandle}{"\n"}{end}' 2>/dev/null | grep -c 'NutanixVolumes-')
  [ "$want_va" -gt 0 ] 2>/dev/null || want_va=3
  for i in $(seq 1 120); do
    tot=$(kubectl --kubeconfig "$KC" --request-timeout=15s get volumeattachments --no-headers 2>/dev/null | wc -l | tr -d ' ')
    att=$(kubectl --kubeconfig "$KC" --request-timeout=15s get volumeattachments -o jsonpath='{range .items[*]}{.status.attached}{"\n"}{end}' 2>/dev/null | grep -c true)
    [ "$att" -ge "$want_va" ] 2>/dev/null && { echo "  $att/$want_va expected VAs attached after ${i}x2s (was a flat 60s sleep)"; break; }
    [ "$i" = "120" ] && echo "  WARN: only $att/$want_va VAs attached after 240s — continuing"
    sleep 2
  done
  # Resume the kustomization once the pod has actually been PLACED (a running pod is not moved by a
  # later selector change). Always resume, even if placement timed out — leaving a kustomization
  # suspended would silently stop reconciling git-operator forever.
  if [ -n "$PIN" ]; then
    for i in $(seq 1 60); do
      NODE=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pod git-operator-git-0 -n git-operator-system -o jsonpath='{.spec.nodeName}' 2>/dev/null)
      [ -n "$NODE" ] && { echo "  LEVER-A: git pod placed on $NODE (target $PIN, match=$([ "$NODE" = "$PIN" ] && echo YES || echo NO))"; break; }
      sleep 2
    done
    kubectl --kubeconfig "$KC" --request-timeout=20s patch kustomization git-operator -n kommander \
      --type=merge -p '{"spec":{"suspend":false}}' >/dev/null 2>&1
    echo "  LEVER-A: git-operator kustomization resumed"
  fi
  gstate=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pod git-operator-git-0 -n git-operator-system --no-headers 2>/dev/null | awk '{print $3}')
  # With LEVER-A the bounce is not just unnecessary, it is HARMFUL: the pod was placed onto a node
  # whose VGs are already attached, so its first mount succeeds — but the bounce fires while it is
  # still initialising (measured bcn2: placed +181s, DELETED +187s) and throws away a good pod for
  # another full init cycle. Only bounce when the pin did NOT take effect.
  if [ "$NODE" = "$PIN" ] && [ -n "$PIN" ]; then
    echo "  git pod on its pinned node with VGs pre-attached — skipping consumer bounce"
  elif [ "$gstate" = "Running" ]; then
    echo "  git pod already Running — skipping consumer bounce"
  else
    kubectl --kubeconfig "$KC" --request-timeout=20s delete pod git-operator-git-0 -n git-operator-system --wait=false 2>/dev/null
    kubectl --kubeconfig "$KC" --request-timeout=20s delete pod -n caren-system -l app=helm-repository --wait=false 2>/dev/null
  fi
  echo "storage bring-up done (stale CSI cleared -> real attach -> consumers bounced)" ) &
echo "storage bring-up started (background)"

# 0.4 LIVE MetalLB POOL REBIND — the render ROOT (qa3-g1 postmortem, 2026-07-29).
# DELTA-2 sets the LB range on the CAPI *topology variable* only; the LIVE IPAddressPool in the
# cluster is a separate object, and if it still holds the TEMPLATE's range then traefik keeps the
# template's ingress IP, KommanderCluster.status.ingress.address is (correctly) derived from that,
# and appmanagement re-renders every SSO override CM back to the stale LB no matter how often the
# CMs are patched — the dex chain then crashloops on an ingress nothing answers. Measured on
# qa3-g1: pool patched -> traefik re-assigned in <20s -> status + overrides self-corrected.
# (The direct pool patch was bc-era PHASE 6.8; it lived in a standalone that was dropped as
# unwired — genuinely unwired, which is how the merged pipeline lost this step. Single-node runs
# never surfaced it.) Patch the SOURCE; controllers make it durable — no verify-loop needed.
# RETRY REQUIRED (qa3-g2, 2026-07-29): a single attempt at +14s failed — metallb's own admission
# webhook is not serving that early after boot, so the patch bounces. Background-retry until the
# webhook is up; verify by reading the value back (a patch acked through a half-up webhook chain
# is not proof). Bounded: 20 rounds x 15s.
(
  for i in $(seq 1 20); do
    POOL=$(kubectl --kubeconfig "$KC" --request-timeout=15s get ipaddresspool -n metallb-system -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    if [ -n "$POOL" ] && [ -n "$LB" ]; then
      kubectl --kubeconfig "$KC" --request-timeout=15s patch ipaddresspool "$POOL" -n metallb-system \
        --type=merge -p "{\"spec\":{\"addresses\":[\"$LB-$LB\"]}}" >/dev/null 2>&1
      GOT=$(kubectl --kubeconfig "$KC" --request-timeout=15s get ipaddresspool "$POOL" -n metallb-system -o jsonpath='{.spec.addresses[0]}' 2>/dev/null)
      if [ "$GOT" = "$LB-$LB" ]; then
        echo "metallb pool '$POOL' -> $LB-$LB (live render root; verified, round $i)"
        exit 0
      fi
    fi
    sleep 15
  done
  echo "WARN: metallb pool never accepted $LB-$LB after 20 rounds — SSO will re-render to the template LB"
) &

# 0.5 IMMEDIATE SSO CONFIG PATCH (no flux wait): the rendered override CMs + dex secret carry
# the TEMPLATE LB from the freeze — patch them NOW so the dex chain reads correct config on its
# very next restart. The git fix below makes it durable (flux re-renders to the same value).
TL=$(python3 - <<PY
import re
# template LB: read from the boc env passthrough or default
import os
print(os.environ.get("NKP_TEMPLATE_LB","<lb-address>"))
PY
)
python3 - "$KC" "$TL" "$LB" <<'PY'
import subprocess, base64, json, sys
KC, TL, LB = sys.argv[1], sys.argv[2], sys.argv[3]
def kx(*a, inp=None):
    return subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=30s",*a],capture_output=True,text=True,input=inp)
n=0
for cm in [c.split("/")[-1] for c in kx("get","cm","-n","kommander","-o","name").stdout.splitlines()]:
    y=kx("get","cm",cm,"-n","kommander","-o","yaml").stdout
    if TL in y:
        kx("apply","-f","-",inp=y.replace(TL,LB)); n+=1
r=kx("get","secret","dex","-n","kommander","-o","json")
try:
    d=json.loads(r.stdout); ch=False
    for k,v in list(d.get("data",{}).items()):
        dec=base64.b64decode(v).decode(errors="ignore")
        if TL in dec:
            d["data"][k]=base64.b64encode(dec.replace(TL,LB).encode()).decode(); ch=True
    if ch:
        for f in ("resourceVersion","uid","creationTimestamp","managedFields"): d["metadata"].pop(f,None)
        kx("apply","-f","-",inp=json.dumps(d)); n+=1
except Exception: pass
print("SSO config patched: %d objects" % n)
PY
# restart the WHOLE dex chain: dex-the-provider is Running-but-serving-stale-issuer (it never
# crashes, so a crash-bounce misses it) — an explicit rollout is required to reload config.
kubectl --kubeconfig "$KC" --request-timeout=20s rollout restart deploy dex dex-k8s-authenticator traefik-forward-auth-mgmt kube-oidc-proxy -n kommander 2>/dev/null

# 1. GIT-VERIFY (as soon as the git server serves — the tail-killer)
for i in $(seq 1 60); do
  gp=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pod git-operator-git-0 -n git-operator-system --no-headers 2>/dev/null)
  echo "$gp" | grep -q "4/4.*Running" && break
  sleep 10
done
STALE=$(kubectl --kubeconfig "$KC" --request-timeout=90s exec git-operator-git-0 -n git-operator-system -c git-server-fcgi -- sh -c "cd /tmp && rm -rf p && git clone -q /volumes/git/kommander/kommander.git p 2>/dev/null && grep -roh '10\.22\.[0-9.]*' p/clusters/*/apps/kommander/management/dex/ 2>/dev/null | sort -u | head -1" 2>/dev/null | tail -1)
if [ -n "$STALE" ] && [ "$STALE" != "$LB" ]; then
  kubectl --kubeconfig "$KC" --request-timeout=120s exec git-operator-git-0 -n git-operator-system -c git-server-fcgi -- sh -c "set -e; cd /tmp && rm -rf rw && git clone -q /volumes/git/kommander/kommander.git rw && cd rw && grep -rl '$STALE' clusters | xargs -r sed -i 's#$STALE#$LB#g' && git -c user.name=postboc -c user.email=p@b add -A && (git -c user.name=postboc -c user.email=p@b commit -q -m 'post-boc: ingress $STALE -> $LB' || true) && git push -q origin main && echo GIT-INGRESS-FIXED"
  kubectl --kubeconfig "$KC" --request-timeout=20s annotate gitrepository -n kommander-flux management reconcile.fluxcd.io/requestedAt=$(date +%s) --overwrite 2>/dev/null
  sleep 45
  echo "git fixed"
else
  echo "git ingress already $LB"
fi

# 1b. PULL THE SSO CHAIN OUT OF BACKOFF — in BOTH branches (the green-tail bimodality, 2026-07-29).
# Pods in the dex chain that started before the git server was Ready sit in CrashLoopBackOff, and
# kubelet's backoff doubles up to 5 minutes. The bounce above used to live ONLY in the stale-ingress
# branch; on the (now common) "ingress already correct" path nothing pulled them out, so green time
# was bimodal: ~240s when no pod happened to be in backoff, 420-480s when one was (measured qa-sn-c7
# 480s vs c8 240s, d1 480s vs d2 240s — same template, same run). A deleted deployment pod is
# recreated immediately with backoff RESET, so this converts a up-to-5min backoff into a ~15s retry.
# Background + bounded: 40 rounds x 15s (~10min), stop after 2 consecutive clean rounds.
# EXTENDED 12->40 (qa3-g3, 2026-07-30): kommander-appmanagement panicked transiently during the LB
# re-render churn and entered CrashLoopBackOff at ~t+300s — AFTER the 3-minute sweep had exited
# clean. Its panic cause was already gone (one delete -> 2/2 Running in 20s), so backoff was the
# only thing holding green for 9+ minutes. The early-exit keeps the common case cheap.
(
  clean=0
  for i in $(seq 1 40); do
    crashers=$(kubectl --kubeconfig "$KC" --request-timeout=20s get pods -n kommander --no-headers 2>/dev/null \
               | grep -E "CrashLoopBackOff" | awk '{print $1}')
    if [ -z "$crashers" ]; then
      clean=$((clean+1)); [ "$clean" -ge 2 ] && { echo "  sso-backoff-sweep: clean (round $i)"; break; }
    else
      clean=0
      for p in $crashers; do
        kubectl --kubeconfig "$KC" --request-timeout=15s delete pod "$p" -n kommander --wait=false 2>/dev/null
        echo "  sso-backoff-sweep: bounced $p out of CrashLoopBackOff"
      done
    fi
    sleep 15
  done
) &

# 2. kube-vip HA restore
for ip in $(python3 -c "import json;m=json.load(open('$MAP'));print(' '.join(c['ip'] for c in m['cps'][1:]))"); do
  ssh -i ~/.ssh/nkp_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 konvoy@$ip \
   'sudo test -f /root/kube-vip.yaml.parked && sudo sed -i "s#value: \"15\"#value: \"60\"#; s#value: \"10\"#value: \"40\"#; s#value: \"2\"#value: \"5\"#" /root/kube-vip.yaml.parked && sudo mv /root/kube-vip.yaml.parked /etc/kubernetes/manifests/kube-vip.yaml && echo "$(hostname): kube-vip restored"' 2>/dev/null
done

# CAPI objects live in `kommander` on NKP 2.18+ but in `default` on 2.17, so the
# namespace is detected once rather than assumed (2026-08-31).
CNS=$(kubectl --kubeconfig "$KC" --request-timeout=20s get cluster -A --no-headers 2>/dev/null | awk 'NR==1{print $1}')
[ -n "$CNS" ] || CNS=kommander
echo "post_boc: CAPI namespace = $CNS"


# 3. MHC unpause when machines all Running
for i in $(seq 1 40); do
  run=$(kubectl --kubeconfig "$KC" --request-timeout=15s get machines -n "$CNS" --no-headers 2>/dev/null | grep -c Running)
  tot=$(kubectl --kubeconfig "$KC" --request-timeout=15s get machines -n "$CNS" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  [ "$run" = "$tot" ] && [ "$tot" != "0" ] && break
  sleep 20
done
for m in $(kubectl --kubeconfig "$KC" --request-timeout=15s get machinehealthchecks -n kommander -o name 2>/dev/null); do
  kubectl --kubeconfig "$KC" --request-timeout=15s annotate "$m" -n "$CNS" cluster.x-k8s.io/paused- 2>/dev/null
done
echo "MHCs unpaused (machines $run/$tot Running)"

# 4. LB announcer sanity
NP=$(kubectl --kubeconfig "$KC" --request-timeout=15s get svc kommander-traefik -n kommander -o jsonpath='{.spec.ports[?(@.port==443)].nodePort}' 2>/dev/null)
SEED=$(python3 -c "import json;print(json.load(open('$MAP'))['cps'][0]['ip'])")
MAC=$(ssh -i ~/.ssh/nkp_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 konvoy@$SEED "sudo arping -c 3 -I eth0 $LB 2>/dev/null | grep -oE '\[[0-9A-F:]+\]' | head -1 | tr -d '[]'" 2>/dev/null | tr 'A-F' 'a-f')
for ip in $(python3 -c "import json;m=json.load(open('$MAP'));print(' '.join(x['ip'] for x in m['cps']+m.get('workers',[])))"); do
  m=$(ssh -i ~/.ssh/nkp_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 konvoy@$ip "ip link show eth0 | awk '/ether/{print \$2}'" 2>/dev/null)
  if [ "$m" = "$MAC" ]; then
    code=$(ssh -i ~/.ssh/nkp_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 konvoy@$ip "curl -sk --max-time 4 -o /dev/null -w '%{http_code}' https://127.0.0.1:$NP/" 2>/dev/null)
    echo "LB announcer=$ip nodePort=$code"
    if [ "$code" = "000" ]; then
      HOST=$(python3 -c "
import json
m=json.load(open('$MAP'))
for x in m['cps']+m.get('workers',[]):
    if x['ip']=='$ip': print(x['host'])")
      SP=$(kubectl --kubeconfig "$KC" --request-timeout=15s get pods -n metallb-system -o wide --no-headers 2>/dev/null | awk -v h="$HOST" '$0 ~ h && /speaker/ {print $1}')
      [ -n "$SP" ] && kubectl --kubeconfig "$KC" --request-timeout=15s delete pod "$SP" -n metallb-system --wait=false && echo "bounced broken announcer speaker"
    fi
    break
  fi
done
# 5. GIT RE-HYDRATE (deterministic — replaces the whack-a-mole SSO rebind + fragile at-rest
# gitfix): rebuild the git-operator repos from the freeze bundle with the ingress LB rewritten
# to $LB, prune-safely (suspend flux -> rebuild -> resume). This makes the git source correct-
# by-construction regardless of restore integrity, so the stale-template-LB CMs / wrong dex
# issuer / crashlooping SSO chain (all downstream of a corrupt or stale git repo) converge on
# their own. bc17 corruption + .254-stuck was PROVEN to be exactly this class.
# Prefer the bundles + src-LB captured by freeze.py for THIS template (self-contained);
# fall back to the prototype artifacts if this template predates bundle-at-freeze.
PFX="${NKP_TEMPLATE_PREFIX:-$(python3 -c "import json;print(json.load(open('$MAP')).get('prefix',''))" 2>/dev/null)}"
BMETA="$SCR/$PFX-git-bundles.json"
if [ -n "$PFX" ] && [ -f "$BMETA" ]; then
  BDIR=$(python3 -c "import json;print(json.load(open('$BMETA'))['dir'])" 2>/dev/null)
  BSRC=$(python3 -c "import json;print(json.load(open('$BMETA'))['src_lb'])" 2>/dev/null)
  echo "git_hydrate: using freeze-captured bundles for $PFX (src LB $BSRC)"
fi
BSRC="${BSRC:-${NKP_BUNDLE_SRC_LB:-<lb-address>}}"
BDIR="${BDIR:-${NKP_BUNDLE_DIR:-$SCR/git-artifacts}}"
if [ -f "$BDIR/kommander.bundle" ]; then
  echo "git_hydrate: rebuilding repos from $BDIR (src LB $BSRC -> $LB)"
  python3 -u "$SCR/git_hydrate.py" "$KC" "$BSRC" "$LB" "$BDIR" 2>&1 | sed 's/^/  /'
else
  echo "git_hydrate: no bundle at $BDIR — SKIPPED (falling back to flux-native reconcile)"
fi

# ---- TOPOLOGY: single-node right-sizing -------------------------------------------------------
# THE ONLY PLACE NODE COUNT IS BRANCHED ON. Everything else in the freeze/claim pipeline is a loop
# over N; this is genuinely topology-dependent and cannot be: on ONE node the second replica of
# cilium-operator / csi-controller is unschedulable forever (hard anti-affinity on hostname) and
# csi's maxSurge:0 rollout deadlocks at replicas=2. Injects replicas:1 into the HCP valuesTemplates.
# (Was run by the retired single-node driver; re-wired here so one pipeline serves every topology.)
# CPN/WKN are resolved once near the top of this script (the LEVER-A pin needs them too).
if [ "$CPN" = "1" ] && [ "$WKN" = "0" ]; then
  echo "topology: single-node (1 CP, 0 workers) -> right-sizing"
  # Check the helper EXISTS before pretending to run it. On qa-sn-c7 this file was missing from the
  # working dir; the interpreter's "can't open file" flowed through the `sed` filter like ordinary
  # output and the step reported success, so right-sizing silently did not happen. On a template
  # that is not already right-sized that leaves cilium-operator / csi-controller Pending forever.
  if [ ! -f "$SCR/rightsize_hcp.py" ]; then
    echo "  ERROR: $SCR/rightsize_hcp.py is MISSING — single-node right-sizing DID NOT RUN."
    echo "  Restore it from hack/instant-cluster or the durable mirror, then run:"
    echo "    python3 rightsize_hcp.py $KC"
  else
    python3 "$SCR/rightsize_hcp.py" "$KC" > /tmp/rightsize.$$ 2>&1
    _rs=$?
    sed 's/^/  /' /tmp/rightsize.$$; rm -f /tmp/rightsize.$$
    [ $_rs -eq 0 ] || echo "  ERROR: rightsize_hcp.py exited $_rs — expect Pending cilium-operator / csi-controller."
  fi
else
  echo "topology: ${CPN:-?} CP / ${WKN:-?} worker(s) -> no right-sizing needed"
fi

# HISTORY PRUNE (parity audit qa-p1, 2026-08-05): scale-0 ReplicaSets and old ControllerRevisions
# retain the TEMPLATE's identity in their pod templates — live specs are clean, but a day-2
# `kubectl rollout undo` would resurrect the template VIP/LB. Deleting stale history is equivalent
# to revision-limit pruning; the CURRENT revision never matches (live specs verified clean).
TVIP=$(python3 -c "import json;print(json.load(open('$MAP'))['old_vip'])" 2>/dev/null)
TLB="${NKP_TEMPLATE_LB:-}"
(
python3 - "$KC" "$TVIP" "$TLB" <<'PYPRUNE' 2>/dev/null
import json, subprocess, sys
KC, TVIP, TLB = sys.argv[1], sys.argv[2], sys.argv[3]
def k(*a):
    return subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=30s",*a],
                          capture_output=True, text=True)
marks = [m for m in (TVIP, TLB) if m]
n = 0
r = k("get","rs","-A","-o","json")
for it in (json.loads(r.stdout).get("items") or []) if r.returncode==0 else []:
    if (it.get("spec",{}).get("replicas") or 0) != 0: continue
    blob = json.dumps(it)
    if any(m in blob for m in marks):
        k("delete","rs",it["metadata"]["name"],"-n",it["metadata"]["namespace"],"--wait=false"); n += 1
r = k("get","controllerrevisions","-A","-o","json")
for it in (json.loads(r.stdout).get("items") or []) if r.returncode==0 else []:
    blob = json.dumps(it)
    if any(m in blob for m in marks):
        k("delete","controllerrevisions",it["metadata"]["name"],"-n",it["metadata"]["namespace"],"--wait=false"); n += 1
print("  history-prune: %d stale rollback object(s) removed" % n)
PYPRUNE
) &

echo "post_boc complete"
