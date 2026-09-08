#!/usr/bin/env python3
"""PARITY GATES — verify a claimed cluster is genuinely equivalent to a freshly created one.

Runs the final gate family only (8-join / 11 / 11.5 / 12). Use it standalone to re-check a claim,
or as the last step of a claim driver. Reads claim-map.json; the kubeconfig targets the node IP
(stable across the claim).

Two day-2 assertions, selected by BOC_SKIP_VM_RENAME:
  unset : Prism VM names must equal their CAPI Machine names (the CAPX name-guard path).
  "1"   : boot-once-correct claims keep UNIQUE clone VM names — safe for many concurrent clones —
          so instead assert a READY NutanixMachine, which short-circuits the name-guard and lets
          CAPX resolve the VM by UUID.

PROVENANCE (2026-07-27): this file used to be GENERATED from stage_b_claim.py by
gen_continuations.py. It had since been hand-edited — the BOC_SKIP_VM_RENAME branch above and a
transient-fetch re-derive guard existed ONLY here, never in the generator — so regenerating, which
the README instructed after any stage_b_claim.py edit, silently destroyed 16 lines of real fixes.
The hand-edited version is the truth, so this is now a normal source file and the generator has been
removed. Edit this file directly."""
import json, os, subprocess, sys, time

# Working/state directory: the map file, logs and generated kubeconfigs live here.
SCR = os.environ.get("SPEEDSTART_DIR") or os.path.dirname(os.path.abspath(__file__))
KEY = os.path.expanduser(os.environ.get("NKP_SSH_KEY", "~/.ssh/nkp_cluster"))
def _detect_ns(_kc):
    """CAPI namespace: `kommander` on NKP 2.18+, `default` on 2.17 (2026-08-31)."""
    import subprocess as _sp
    r = _sp.run(["kubectl", "--kubeconfig", _kc, "--request-timeout=30s",
                 "get", "cluster", "-A", "--no-headers"], capture_output=True, text=True)
    for _l in (r.stdout or "").splitlines():
        _f = _l.split()
        if len(_f) > 1:
            return _f[0]
    return "kommander"

# Per-claim map (concurrency-safe): the claim name is argv[1]. A FIXED map filename would
# collide between simultaneous claims — the same bug class that retired the online path.
if len(sys.argv) < 2:
    sys.exit("usage: gates.py <claim-name>   (reads claim-map-<claim-name>.json)")
M = json.load(open(SCR + "/claim-map-%s.json" % sys.argv[1]))
TMAP = M["tmap"]          # template_uuid -> clone_uuid
NAME = M["name"]
# Derive a stable kubeconfig pointing at the seed CP (cp-1) node IP, from the template kubeconfig.
# (default to the first CP — stage A tolerates hostnames that don't end in -cp-1, so must we)
SEED_IP = next((c["ip"] for c in M["cps"] if c["host"].endswith("-cp-1")), M["cps"][0]["ip"])
# template kubeconfig: by PREFIX from the map (traditional-built templates have machine-name
# hostnames with no "-cp-" marker, so the old hostname-derivation breaks on them)
_tp = M.get("prefix") or M["cps"][0]["host"].rsplit("-cp-", 1)[0]
TMPL_KC = next(p for p in (os.path.join(os.environ.get("NKP_TEMPLATE_KC_DIR", SCR), "%s.conf" % _tp),
                          os.path.join(SCR, "%s.conf" % _tp))
               if __import__("os").path.exists(p))
KC = SCR + "/%s-claim.conf" % NAME
NS = _detect_ns(KC)
_tmpl = open(TMPL_KC).read()
import re as _re
# VIP, not the seed node IP (qa-p1 CP-roll postmortem, 2026-08-06): this line kept REGENERATING
# the claim kubeconfig with the seed's node IP — so after a CP roll deleted that node, every
# kubectl "proved" the cluster was down while curl against the literal VIP worked, and each manual
# repair of the file was silently undone by the next gates run. Two days of VPN/HTTP2/MTU
# false-blame came from this one line. The VIP survives machine replacement; that is its job.
open(KC, "w").write(_re.sub(r"https://10\.22\.20[0-9.]+:6443", "https://%s:6443" % M["new_vip"], _tmpl))
def log(m): print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

# node hostname -> current clone IP
NODEIP = {c["host"]: c["ip"] for c in M["cps"]}

CPHOSTS = [c["host"] for c in M["cps"]]

def kx(*args, timeout=15, tries=5):
    for _ in range(tries):
        r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=%ds" % (timeout - 3), *args],
                           capture_output=True, text=True)
        if r.returncode == 0: return r.stdout.strip()
        time.sleep(3)
    return None
def ssh(ip, cmd, to=120):
    # returns (rc, stdout, stderr); bounded — a post-connect wedge must not hang the stage.
    # RETRIES connection-level failures (banner-exchange timeout, refused, no route): a single
    # transient SSH hiccup FATAL'd the qa3cp7n clean run 17s into 6Q. Command-level failures
    # (rc!=0 with a real connection) are NOT retried — call sites own that semantics.
    last = (255, "", "no attempt")
    for attempt in range(4):
        try:
            r = subprocess.run(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                                "-o", "ConnectTimeout=12", "-o", "BatchMode=yes", "konvoy@" + ip, cmd],
                               capture_output=True, text=True, timeout=to + 30)
            last = (r.returncode, r.stdout.strip(), r.stderr.strip())
            conn_fail = r.returncode == 255 and any(s in (r.stderr or "") for s in
                ("timed out", "banner exchange", "Connection refused", "No route to host", "Connection reset"))
            if not conn_fail:
                return last
        except subprocess.TimeoutExpired:
            last = (124, "", "ssh timeout after %ds" % (to + 30))
        time.sleep(6)
    return last

def kubelet_restart(ip):
    rc, _, err = ssh(ip, "sudo systemctl restart kubelet")
    if rc != 0:
        log("    kubelet restart rc=%d (%s) — retrying once" % (rc, (err or "")[:80]))
        time.sleep(5)
        ssh(ip, "sudo systemctl restart kubelet")

def _surplus_pending(ns, pod, all_lines):
    """A Pending pod is EXPECTED (not unhealthy) when it is a surplus anti-affinity replica:
    its controller keeps 2 replicas with a hard node anti-affinity, and the cluster has fewer
    nodes than replicas — on a single-node cluster cilium-operator and nutanix-csi-controller
    park one replica in Pending forever, BY DESIGN (documented in the single-node POC). The
    general, name-agnostic test: another pod of the SAME ReplicaSet (name minus the pod-suffix)
    is Running and fully ready."""
    rs = pod.rsplit("-", 1)[0]   # pod name = <replicaset>-<suffix>
    for l2 in all_lines:
        c2 = l2.split()
        if (len(c2) >= 4 and c2[0] == ns and c2[1] != pod and c2[1].startswith(rs + "-")
                and c2[3] == "Running" and "/" in c2[2]
                and c2[2].split("/")[0] == c2[2].split("/")[1]):
            return True
    return False

_rollout_kicked = set()
def _unwedge_rollout(ns, pod, all_lines):
    """SINGLE-NODE ROLLOUT DEADLOCK (measured on qa-sn1): a 2-replica anti-affinity deployment
    that gets UPDATED mid-claim (rebind touches its spec) surges a new-ReplicaSet pod that can
    never schedule — and the old-RS pod never dies, so the rollout wedges FOREVER (the new spec
    never applies). Detect: Pending pod whose deployment has a Running sibling from a DIFFERENT
    ReplicaSet. Fix: delete the old-RS pod once — the new pod takes the slot, the deployment
    converges, and the residual surplus replica is then covered by _surplus_pending."""
    rs = pod.rsplit("-", 1)[0]
    deploy = rs.rsplit("-", 1)[0]
    for l2 in all_lines:
        c2 = l2.split()
        if (len(c2) >= 4 and c2[0] == ns and c2[1].startswith(deploy + "-")
                and not c2[1].startswith(rs + "-") and c2[3] == "Running"
                and c2[1] not in _rollout_kicked):
            _rollout_kicked.add(c2[1])
            kx("delete", "pod", c2[1], "-n", ns, "--wait=false", tries=1)
            log("  unwedging single-node rollout: deleted old-RS pod %s/%s (surge pod %s can never "
                "schedule beside it)" % (ns, c2[1], pod))
            return True
    return False




cl = kx("get", "cluster", "-n", NS, "-o", "jsonpath={.items[0].metadata.name}")
if not cl: log("FATAL: cannot read cluster name"); sys.exit(1)
NEW_VIP = M["new_vip"]; LB_START = M.get("lb_start"); LB_END = M.get("lb_end")
kcp_name = kx("get", "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io", "-n", NS, "-o", "jsonpath={.items[0].metadata.name}")
_apicmd = kx("get", "pods", "-n", "kube-system", "-l", "component=kube-apiserver",
             "-o", "jsonpath={.items[0].spec.containers[0].command}", tries=3) or ""
_enc_real = "encryption-provider-config" in _apicmd
_etcd_real = kx("get", "pods", "-n", "kube-system", "-l", "component=etcd",
                "-o", "jsonpath={.items[0].spec.containers[0].image}", tries=3) or ""
_etcd_real_tag = _etcd_real.rsplit(":", 1)[-1] if ":" in _etcd_real else ""
log("CONTINUE MODE: gates only (11/11.5/12) for %s" % cl)
log("PHASE 8-join: waiting for backgrounded PVC restore")
if "_pvc_thread" not in globals():
    log("  (continue mode: restore thread not started this run — skipping join)")
    _pvc_thread = None
    _pvc_result = {"ok": True}   # restore completed in the prior (full) run; gates re-verify anyway
if _pvc_thread: _pvc_thread.join(timeout=420)
if _pvc_thread and _pvc_thread.is_alive():
    log("FATAL: PVC restore did not finish within 7min — refusing to gate on a half-restored "
        "git-operator"); sys.exit(7)
if _pvc_result.get("err"):
    log("FATAL: PVC restore failed: %s" % _pvc_result["err"]); sys.exit(7)
if not _pvc_result.get("ok"):
    log("FATAL: PVC restore thread produced no result — treating as failure"); sys.exit(7)
log("  PVC restore complete (overlapped with phases 9-10.5)")

def _heal_stalled_helmreleases(tag=""):
    """Reset HelmReleases stuck in Stalled=True.

    A release that upgraded with values that were still stale renders the old
    address, its pods never start, the upgrade times out, and remediation
    leaves Stalled=True "cannot remediate failed release". A stalled release
    has spent its retries and IGNORES requestedAt, so correcting the values
    afterwards changes nothing. Toggling suspend resets remediation.

    Called from BOTH gates: this first showed up as a stuck flux kustomization
    (PHASE 11.5), but 2026-09-01 it presented as dex-k8s-authenticator
    crashlooping on a stale LB address, which blocks the POD-HEALTH gate at
    PHASE 11 - so a heal that only ran in 11.5 could never be reached.
    """
    # NOT jsonpath: "{'\n'}" in python source is a REAL newline, and kubectl
    # rejects it with "unterminated quoted string" - so this query always
    # errored, `stalled` was always empty, and this heal has never once fired.
    # Live-proved 2026-09-02 by running the exact query by hand.
    raw = kx("get", "hr", "-A", "-o", "json", tries=1) or ""
    try:
        items = json.loads(raw).get("items", [])
    except Exception:
        items = []
    stalled = [
        "%s/%s" % (i["metadata"]["namespace"], i["metadata"]["name"])
        for i in items
        if any(c.get("type") == "Stalled" and c.get("status") == "True"
               for c in ((i.get("status") or {}).get("conditions") or []))
    ]
    for hl in stalled[:6]:
        ns, _, nm = hl.partition("/")
        log("  stalled-helmrelease self-heal%s: suspend/resume %s" % (tag, hl))
        kx("patch", "hr", nm, "-n", ns, "--type=merge", "-p", '{"spec":{"suspend":true}}', tries=2)
        time.sleep(4)
        kx("patch", "hr", nm, "-n", ns, "--type=merge", "-p", '{"spec":{"suspend":false}}', tries=2)
    return len(stalled)


def _heal_stale_lb(ns, pod, err, lbip, tag=""):
    """Force a re-render when a pod dials an address that is not this LB.

    The Stalled heal above cannot catch this: the release installed fine, so it
    is NOT Stalled - only the VALUE it rendered is wrong. Kicking the pod just
    re-reads the same wrong address, which is why the gate can burn its whole
    budget kicking a pod that will never converge.

    Live-caught 2026-09-02: dex-k8s-authenticator dialled <lb-address> (the
    template's LB) for 45 min while this cluster's LB was .137; both 1200s gate
    attempts expired with zero self-heals fired.
    """
    if not lbip or lbip == "<none>":
        return 0
    stale = sorted({i for i in _re.findall(r"\b10\.\d+\.\d+\.\d+\b", err)
                    if i != lbip})
    if not stale:
        return 0
    base = _re.sub(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$", "", pod)
    raw = kx("get", "hr", "-n", ns, "-o", "json", tries=1) or ""
    try:
        names = [i["metadata"]["name"] for i in json.loads(raw).get("items", [])]
    except Exception:
        names = []
    # longest match first: dex-k8s-authenticator must win over dex
    owners = sorted([h for h in names if base == h or base.startswith(h)],
                    key=len, reverse=True)
    log("  stale-LB self-heal%s: %s/%s dials %s but LB is %s -> re-render %s"
        % (tag, ns, pod, ",".join(stale), lbip, ",".join(owners) or "(no HR matched)"))
    for nm in owners[:3]:
        kx("patch", "hr", nm, "-n", ns, "--type=merge",
           "-p", '{"spec":{"suspend":true}}', tries=2)
        time.sleep(4)
        kx("patch", "hr", nm, "-n", ns, "--type=merge",
           "-p", '{"spec":{"suspend":false}}', tries=2)
    return len(owners)



def _heal_orphan_kubefed_members(tag=""):
    """Delete KubeFedClusters that no KommanderCluster claims.

    Found 2026-09-07, after three 40-minute `nkp upgrade kommander` timeouts
    on 2026-09-02: the template had been frozen from a workload-attach run
    whose workload cluster was detached properly (KommanderCluster gone) but
    whose KubeFedCluster + secret + per-cluster namespace were left behind.
    Every claim inherited a federated member with a dead endpoint
    (Offline/ClusterNotReachable). No HelmRelease was ever unready - the
    upgrade was waiting on federated propagation to a cluster that does not
    exist. The host member uses https://kubernetes.default.svc and is never
    touched; only members with an external endpoint AND no KommanderCluster
    are orphans.
    """
    raw = kx("get", "kubefedcluster", "-A", "-o", "json", tries=1) or ""
    try: members = json.loads(raw).get("items", [])
    except Exception: members = []
    raw = kx("get", "kommandercluster", "-A", "-o", "json", tries=1) or ""
    try: kc = json.loads(raw).get("items", [])
    except Exception: kc = []
    claimed = {(i.get("spec", {}).get("kubefedClusterRef") or {}).get("name") for i in kc}
    claimed |= {i["metadata"]["name"] for i in kc}
    for m in members:
        name = m["metadata"]["name"]; ns = m["metadata"]["namespace"]
        ep = (m.get("spec", {}) or {}).get("apiEndpoint", "")
        if "kubernetes.default" in ep or name in claimed:
            continue
        sec = ((m.get("spec", {}) or {}).get("secretRef") or {}).get("name", "")
        log("orphan federated member %s (endpoint %s, no KommanderCluster) - removing%s" % (name, ep, tag))
        kx("delete", "kubefedcluster", name, "-n", ns, "--ignore-not-found", tries=1)
        if sec: kx("delete", "secret", sec, "-n", ns, "--ignore-not-found", tries=1)
        for line in (kx("get", "ns", "-o", "name", tries=1) or "").splitlines():
            n = line.split("/")[-1]
            if n.startswith(name + "-"):
                kx("delete", "ns", n, "--ignore-not-found", "--timeout=90s", tries=1)

# ---- PHASE 11: gate ----
log("PHASE 11: health gate")
_heal_orphan_kubefed_members(" (health gate)")

_surplus_logged = set()
for i in range(50):
    out = kx("get", "pods", "-A", "--no-headers", timeout=25) or ""
    _lines = out.splitlines()
    bad = 0
    for l in _lines:
        c = l.split()
        if len(c) < 4: continue
        if c[3] == "Pending" and _surplus_pending(c[0], c[1], _lines):
            if c[1] not in _surplus_logged:
                _surplus_logged.add(c[1])
                log("  tolerating %s/%s: surplus anti-affinity replica (sibling Ready; expected on single-node)" % (c[0], c[1]))
            continue
        if c[3] == "Pending":
            _unwedge_rollout(c[0], c[1], _lines)   # still counted bad this round; converges next
        if c[3] not in ("Running", "Completed", "Succeeded"): bad += 1
        elif "/" in c[2] and c[2].split("/")[0] != c[2].split("/")[1] and c[3] != "Completed": bad += 1
    mach = kx("get", "machines", "-n", NS, "-o", "jsonpath={range .items[*]}{.status.phase},{end}") or ""
    if i % 3 == 0: log("  +%dm unhealthy=%d machines=%s" % (i//3, bad, mach))
    if out and bad == 0: log("  ALL PODS HEALTHY"); break
    # late-arriving broken-sandbox pods (can't dial 10.96.0.1; the fault lives with the SANDBOX,
    # container restarts never fix it) can appear AFTER the 6.65 sweep — kick them every ~3min so
    # the gate CONVERGES instead of timing out (measured: cert-manager r=9 blocked all cert Inits).
    # a crashlooping pod whose HelmRelease is Stalled will never converge on
    # its own - the gate would just burn its whole budget
    if i == 6 or i == 15:
        _heal_stalled_helmreleases(" (health gate)")
    if i % 9 == 8:
        for l in out.splitlines():
            c = l.split()
            if (len(c) >= 5 and "/" in c[2] and c[2].split("/")[0] == "0"
                    and c[3] not in ("Completed", "Succeeded") and c[4].isdigit() and int(c[4]) >= 3):
                # WHY it is crashlooping, before we destroy the evidence. A
                # pod kicked every ~3min still failing for 20min means its
                # DEPENDENCY is unreachable, not that it is backing off - the
                # kick already resets backoff. The pod's own last error names
                # the layer (no route to host = LB not announced; connection
                # refused = nothing listening; TLS/404 = routing wrong), and
                # the LB address it dialled tells us whether the SSO rewrite
                # landed. Added 2026-09-01 after four claims spent 7-24min
                # here with no evidence of which layer was slow.
                err = (kx("logs", c[1], "-n", c[0], "--tail=3", tries=1) or "").strip()
                if err:
                    log("    why %s/%s: %s" % (c[0], c[1], err.splitlines()[-1][:150]))
                    # a wrong ADDRESS is not fixed by restarting the pod
                    _lb = kx("get", "svc", "kommander-traefik", "-n", "kommander", "-o",
                             "jsonpath={.status.loadBalancer.ingress[0].ip}", tries=1) or ""
                    _heal_stale_lb(c[0], c[1], err, _lb, " (health gate)")
                kx("delete", "pod", c[1], "-n", c[0], "--wait=false", tries=1)
                log("    gate-kick %s/%s (crashlooping, r=%s)" % (c[0], c[1], c[4]))
        # and the state of the thing they usually depend on
        lbip = kx("get", "svc", "kommander-traefik", "-n", "kommander", "-o",
                  "jsonpath={.status.loadBalancer.ingress[0].ip}", tries=1) or "<none>"
        tfk = kx("get", "pods", "-n", "kommander", "-l",
                 "app.kubernetes.io/name=traefik", "--no-headers", tries=1) or ""
        ready = [x.split()[1] for x in tfk.splitlines() if len(x.split()) > 1]
        log("    ingress: traefik LB=%s pods=%s" % (lbip, ",".join(ready) or "none"))
    time.sleep(20)

# ---- PHASE 11.5: GitOps parity gate — pods-healthy is NOT enough; flux kustomizations must
# all be Ready too (a wedged kustomization is a day-2 parity break QA would trip over) ----
log("PHASE 11.5: flux kustomization gate")
# AUTO-UNWEDGE the helm rollback ghost (documented mechanism): if an HR failed an upgrade during
# the mid-claim churn, flux remediation ROLLS BACK to a release whose recorded values carry the
# TEMPLATE ingress — which can never become Ready. Fresh values are never consulted during
# rollback; the only exit is wiping release history (live objects re-adopt via meta.helm.sh) so
# the next reconcile is a fresh INSTALL with current (git-corrected) values.
_ghosted = kx("get", "hr", "-n", NS, "-o",
              "jsonpath={range .items[*]}{.metadata.name}|{range .status.conditions[?(@.type=='Ready')]}{.status}|{.message}{end}{'\\n'}{end}",
              timeout=30, tries=3) or ""
for _l in _ghosted.splitlines():
    _pp = _l.split("|", 2)
    if len(_pp) == 3 and _pp[1] == "False" and ("rollback" in _pp[2].lower() or "pending" in _pp[2].lower()):
        _app = _pp[0]
        log("  auto-unwedge hr/%s (rollback ghost: %s)" % (_app, _pp[2][:60]))
        kx("patch", "hr", _app, "-n", NS, "--type=merge", "-p", '{"spec":{"suspend":true}}', tries=2)
        _sl = kx("get", "secret", "-n", NS, "--no-headers", "-o", "name", timeout=30, tries=2) or ""
        for _sn in _sl.splitlines():
            if ("sh.helm.release.v1.%s." % _app) in _sn:
                kx("delete", _sn, "-n", NS, tries=1)
        kx("patch", "hr", _app, "-n", NS, "--type=merge", "-p", '{"spec":{"suspend":false}}', tries=2)
        kx("annotate", "hr", _app, "-n", NS,
           "reconcile.fluxcd.io/requestedAt=%d" % int(time.time()), "--overwrite", tries=2)
ks_ok = False
_last_nudge = [0]   # wall-clock of the last nudge; see the loop
for i in range(60):   # 60 x 10s == the old 20 x 30s budget
    # jsonpath, not column-scraping: a row with empty READY/STATUS cells would be invisible to
    # a whitespace split (miscounted as ready). Treat empty/absent Ready status as not-ready.
    raw = kx("get", "kustomizations", "-A", "-o",
             "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}="
             "{.status.conditions[?(@.type=='Ready')].status}{'\\n'}{end}", timeout=30) or ""
    bad_ks = [ln.split("=")[0] for ln in raw.splitlines() if ln and ln.split("=", 1)[-1] != "True"]
    log("  kustomizations not-ready: %d %s" % (len(bad_ks), bad_ks[:4]))
    if raw and not bad_ks:
        log("  ALL KUSTOMIZATIONS READY"); ks_ok = True; break
    # FORCE RECONCILE, do not wait for the interval. Kommander's kustomizations
    # carry `interval: 10m`, so a not-ready one that simply needs another pass
    # sits idle until its next tick - which is why this gate took 2.4 min on one
    # claim and 13.1 min on the next: pure luck about where the claim landed in
    # the 10-minute cycle. Annotating requestedAt makes flux reconcile NOW. The
    # same technique is already used for HelmReleases in the unwedge above.
    # ONLY nudge what is IDLE. A kustomization mid-reconcile reports
    # Ready=Unknown "Reconciliation in progress"; annotating it restarts that
    # reconcile from the top, so poking every poll livelocks exactly the big
    # ones (apps-kommander, cluster, kommander, kommander-appmanagement) - they
    # never get to finish. Measured live 2026-08-31: four stuck at
    # "Reconciliation in progress" for the whole gate. Nudge only Ready=False,
    # and only once every 60s so a slow-but-progressing reconcile is left alone.
    _now = int(time.time())
    if _now - _last_nudge[0] >= 60:
        _nudged = 0
        for _nsk in bad_ks:
            _ns2, _, _nm2 = _nsk.partition("/")
            _st = kx("get", "kustomization", _nm2, "-n", _ns2, "-o",
                     "jsonpath={.status.conditions[?(@.type=='Ready')].status}", tries=1) or ""
            if _st.strip() == "False":
                kx("annotate", "kustomization", _nm2, "-n", _ns2,
                   "reconcile.fluxcd.io/requestedAt=%d" % _now, "--overwrite", tries=1)
                _nudged += 1
        if _nudged:
            log("  forced reconcile on %d stalled kustomization(s)" % _nudged)
        _last_nudge[0] = _now
    # and their sources, so a freshly hydrated git repo is actually re-read
    if i == 0:
        _srcs = kx("get", "gitrepository,ocirepository", "-A", "-o",
                   "jsonpath={range .items[*]}{.kind}/{.metadata.namespace}/{.metadata.name}{'\\n'}{end}",
                   timeout=30, tries=2) or ""
        for _sl in _srcs.splitlines():
            _k, _, _rest = _sl.partition("/")
            _ns3, _, _nm3 = _rest.partition("/")
            if _nm3:
                kx("annotate", _k.lower(), _nm3, "-n", _ns3,
                   "reconcile.fluxcd.io/requestedAt=%d" % _now, "--overwrite", tries=1)

    # STALE-STATUS SELF-HEAL: flux can latch a stale StatefulSet "InProgress" health-check even
    # after the workload is healthy, never re-evaluating. suspend/resume forces a fresh check.
    if i >= 1:
        for _nsk in bad_ks:
            _ns2, _, _nm2 = _nsk.partition("/")
            _msg = kx("get", "kustomization", _nm2, "-n", _ns2, "-o",
                      "jsonpath={.status.conditions[?(@.type=='Ready')].message}", tries=1) or ""
            if "InProgress" in _msg or "timeout waiting" in _msg:
                log("  stale-status self-heal: suspend/resume %s" % _nsk)
                kx("patch", "kustomization", _nm2, "-n", _ns2, "--type=merge", "-p", '{"spec":{"suspend":true}}', tries=2)
                time.sleep(4)
                kx("patch", "kustomization", _nm2, "-n", _ns2, "--type=merge", "-p", '{"spec":{"suspend":false}}', tries=2)
    # STALLED HELMRELEASE SELF-HEAL: a release that tried to upgrade with values
    # that were still stale (the SSO repair corrects them moments later) renders
    # the old address, its pods never start, the upgrade times out and
    # remediation leaves Stalled=True "cannot remediate failed release". A
    # stalled release has spent its retries and IGNORES requestedAt, so the
    # corrected values are never read and the kustomization above can never go
    # Ready. Toggling suspend resets the remediation state. Live-caught
    # 2026-09-01: cm/dex-k8s-authenticator held <lb-address> while its own
    # values source already said .119; one suspend toggle rendered the right
    # address and the gate cleared within seconds.
    if i >= 1:
        # kubectl jsonpath cannot nest a filter inside a filter ("unterminated
        # filter"), so list every release with its Stalled status and pick here
        _stalled = kx("get", "hr", "-A", "-o",
                      "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}"
                      "{' '}{.status.conditions[?(@.type=='Stalled')].status}{'\n'}{end}",
                      tries=1) or ""
        _stalled = [x.split()[0] for x in _stalled.splitlines()
                    if x.strip().endswith(" True") and "/" in x]
        for _hl in _stalled[:6]:
            _hns, _, _hnm = _hl.partition("/")
            log("  stalled-helmrelease self-heal: suspend/resume %s" % _hl)
            kx("patch", "hr", _hnm, "-n", _hns, "--type=merge", "-p", '{"spec":{"suspend":true}}', tries=2)
            time.sleep(4)
            kx("patch", "hr", _hnm, "-n", _hns, "--type=merge", "-p", '{"spec":{"suspend":false}}', tries=2)
    time.sleep(10)
if not ks_ok:
    log("  WARNING: kustomization gate exhausted — GitOps parity NOT fully confirmed: %s" % bad_ks)

# ---- PHASE 12: CAPI PARITY GATE (the one that actually proves day-2 works) ----
# THE lesson of the 2026-07-10 audit: pods-healthy + kommander-Reconciled + kustomizations-Ready
# ALL pass on a cluster whose CAPI Cluster is paused with a stuck lifecycle hook — i.e. a cluster
# that CANNOT be upgraded/scaled/remediated. Only the CAPI Cluster's own conditions prove parity.
# Assert: not paused, Available=True, TopologyReconciled=True, all Machines Running w/ NodeRefs.
log("PHASE 12: CAPI parity gate (Available + TopologyReconciled — proves day-2)")
if not cl:  # the one-shot fetch at start can fail transiently; an empty name dooms every query below
    cl = kx("get", "cluster", "-n", NS, "-o", "jsonpath={.items[0].metadata.name}", tries=5)
    log("  (re-derived cluster name: %s)" % cl)
def capi_state():
    p = kx("get", "cluster", cl, "-n", NS, "-o", "jsonpath={.spec.paused}", tries=2) or "?"
    av = kx("get", "cluster", cl, "-n", NS, "-o",
            "jsonpath={range .status.conditions[?(@.type=='Available')]}{.status}{end}", tries=2) or "?"
    tr = kx("get", "cluster", cl, "-n", NS, "-o",
            "jsonpath={range .status.conditions[?(@.type=='TopologyReconciled')]}{.status}{end}", tries=2) or "?"
    return p, av, tr
capi_ok = False
for i in range(20):
    paused, avail, topo = capi_state()
    log("  paused=%s Available=%s TopologyReconciled=%s" % (paused, avail, topo))
    if paused == "false" and avail == "True" and topo == "True":
        capi_ok = True; break
    # a re-queued hook can reappear after unpause — clear it again and re-poke
    ph2 = kx("get", "cluster", cl, "-n", NS, "-o",
             "jsonpath={.metadata.annotations.runtime\\.cluster\\.x-k8s\\.io/pending-hooks}", tries=1) or ""
    if "AfterControlPlaneInitialized" in ph2:
        kx("annotate", "cluster", cl, "-n", NS, "runtime.cluster.x-k8s.io/pending-hooks-", tries=2)
        log("    (re-cleared reappearing pending-hook)")
    if paused != "false":
        kx("patch", "cluster", cl, "-n", NS, "--type=merge", "-p", '{"spec":{"paused":false}}', tries=2)
    time.sleep(20)
mach = kx("get", "machines", "-n", NS, "-o",
          "jsonpath={range .items[*]}{.status.phase}/{.status.nodeRef.name} {end}") or ""
bad_m = [m for m in mach.split() if not m.startswith("Running/") or m.endswith("/")]
if not capi_ok:
    log("FATAL: CAPI parity gate FAILED — cluster is NOT day-2 capable (paused/not-Available/"
        "topology-not-reconciled). This is the defect the pods-healthy gate used to hide.")
    sys.exit(9)
if bad_m:
    log("FATAL: machines not Running-with-NodeRef: %s" % bad_m); sys.exit(9)

# day-2 ROLL assertion: the topology must not re-declare anything the template contradicts, or the
# first CP roll renders a machine that can never join (etcd 3.5.24 vs 3.6.6 / secretbox vs
# plaintext). Phase 7.1 strips them; re-verify, because CAREN can re-default them on reconcile.
_fin = kx("get", "cluster", cl, "-n", NS, "-o",
          "jsonpath={range .spec.topology.variables[?(@.name=='clusterConfig')]}{.value}{end}", tries=2)
if _fin:
    _fv = json.loads(_fin)
    _back = []
    if "encryptionAtRest" in _fv and not _enc_real:
        _back.append("encryptionAtRest declared but machines are PLAINTEXT")
    if _enc_real and "encryptionAtRest" not in _fv:
        _back.append("machines ENCRYPTED but topology lost the declaration (new CP would render "
                     "without decryption config)")
    _dt = ((_fv.get("etcd") or {}).get("image") or {}).get("tag", "")
    if _dt and _etcd_real_tag and _dt != _etcd_real_tag:
        _back.append("etcd tag %s declared vs %s running" % (_dt, _etcd_real_tag))
    if _back:
        log("FATAL: topology<->reality INCOHERENT after unpause: %s. A day-2 CP roll would render "
            "a machine that can NEVER join. Not day-2 capable." % "; ".join(_back))
        sys.exit(9)
    log("  topology<->reality coherent (enc=%s etcd=%s) — day-2 roll is safe"
        % (_enc_real, _etcd_real_tag))

# NO-ROLL assertion (finding 2): the whole point of cloning is to KEEP the converged control plane.
# If KCP considers any cloned CP out-of-date it will replace all 3 — silently, and the cluster still
# ends up "healthy", so nothing else here would catch it. Assert the CP was NOT rolled: KCP must be
# up-to-date, not rolling, and still have exactly the CPs we cloned (no CAPI-minted replacements).
log("PHASE 12: no-roll assertion (the cloned control plane must have SURVIVED the unpause)")
roll_ok = False
for _ in range(15):
    mu = kx("get", "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io", kcp_name, "-n", NS, "-o",
            "jsonpath={range .status.conditions[?(@.type=='MachinesUpToDate')]}{.status}{end}", tries=2) or "?"
    ro = kx("get", "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io", kcp_name, "-n", NS, "-o",
            "jsonpath={range .status.conditions[?(@.type=='RollingOut')]}{.status}{end}", tries=2) or ""
    reps = kx("get", "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io", kcp_name, "-n", NS, "-o", "jsonpath={.status.replicas}", tries=2) or "?"
    log("  MachinesUpToDate=%s RollingOut=%s replicas=%s" % (mu, ro or "<absent>", reps))
    if mu == "True" and ro != "True" and reps == str(len(CPHOSTS)):
        roll_ok = True; break
    time.sleep(20)
if not roll_ok:
    log("FATAL: KCP is rolling (or wants to roll) the control plane after unpause. The cloned CPs "
        "were NOT preserved — this defeats the entire one-snapshot->N-clones design. Most likely "
        "the PHASE 10.3 re-mint did not fully match KCP.spec; diff a cloned CP's KubeadmConfig "
        "against .spec.kubeadmConfigSpec to find the residual field.")
    sys.exit(9)
log("  control plane SURVIVED the unpause: %s cloned CPs, up-to-date, not rolling" % len(CPHOSTS))

# day-2 DELETE assertion: every VM's Prism name must equal its CAPI Machine name, or CAPX's
# name-guard will refuse to delete it and any roll/upgrade/scale-in wedges forever (finding 5).
# Phase 7.2 does the rename; this re-verifies it survived, so a mismatch can never ship silently.
if os.environ.get("BOC_SKIP_VM_RENAME") == "1":
    # boot-once-correct path: clone VMs keep unique claim names (no PC name-duplicates with the
    # frozen template or other clones). Day-2 deletes stay safe: a READY NutanixMachine
    # short-circuits the CAPX name-guard and resolves by UUID. Assert that instead of name parity.
    rdy = kx("get", "nutanixmachine", "-n", NS, "-o",
             "jsonpath={.items[0].status.ready}", tries=3)
    if rdy != "true":
        log("FATAL: NutanixMachine not ready=true — CAPX name-guard would NOT short-circuit and "
            "VM names differ from machine names; day-2 delete would wedge.")
        sys.exit(9)
    log("  day-2 delete OK — NutanixMachine ready=true (CAPX resolves by UUID; VM keeps claim name)")
elif "NKP_NUTANIX_USER" in os.environ:
    rv = subprocess.run(["/usr/bin/python3", "-u", SCR + "/rename_vms_to_machines.py", KC],
                        env={**os.environ}, capture_output=True, text=True)
    out = rv.stdout or ""
    if rv.returncode != 0 or "RENAME" in out:   # any RENAME here = drift after phase 7.2
        log("FATAL: VM names do not match CAPI Machine names — CAPX name-guard would block "
            "every day-2 delete (roll/upgrade/scale-in). out=%s" % out.strip()[-250:])
        sys.exit(9)
    log("  day-2 delete OK — all VM names == CAPI Machine names (CAPX name-guard satisfied)")

# DAY-2 SAFETY GATE: --force-new-cluster must NOT survive the claim (a future CP scale-out
# learner joining a force-new-cluster member would be destroyed). Waves removed it; verify.
_fnc = ssh(NODEIP[CPHOSTS[0]],
           "sudo grep -c 'force-new-cluster' /etc/kubernetes/manifests/etcd.yaml 2>/dev/null || echo 0")[1]
if (_fnc or "0").strip().splitlines()[-1] != "0":
    log("FATAL: --force-new-cluster still armed in the etcd manifest — day-2 CP scale-out "
        "would destroy the cluster. Waves flag-drop did not land.")
    sys.exit(9)
log("  etcd manifest clean: --force-new-cluster removed (day-2 scale-out safe)")
_cpe12 = kx("get", "cluster", cl, "-n", NS, "-o", "jsonpath={.spec.controlPlaneEndpoint.host}", tries=2) or ""
if _cpe12 != NEW_VIP:
    log("FATAL: controlPlaneEndpoint=%s != claim VIP %s — day-2 joins broken" % (_cpe12, NEW_VIP)); sys.exit(9)
log("  Cluster.controlPlaneEndpoint == claim VIP (day-2 joins safe)")
# evidence log (not a gate): does the 6.68 right-sizing survive to steady state?
_r_csi = kx("get", "deploy", "nutanix-csi-controller", "-n", "ntnx-system",
            "-o", "jsonpath={.spec.replicas}", tries=2) or "?"
_r_cil = kx("get", "deploy", "cilium-operator", "-n", "kube-system",
            "-o", "jsonpath={.spec.replicas}", tries=2) or "?"
log("  single-node right-sizing end-state: csi-controller replicas=%s cilium-operator replicas=%s"
    % (_r_csi, _r_cil))
log("  CAPI PARITY OK — Available=True, TopologyReconciled=True, machines=%s" % mach)
log("=== STAGE B done. nkpcluster: %s ===" % (kx("get", "nkpcluster", "-A", "--no-headers") or "?"))
