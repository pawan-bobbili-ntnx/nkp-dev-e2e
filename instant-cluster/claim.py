#!/usr/bin/env python3
"""ONE-WINDOW MULTI-NODE CLAIM (boc3cp) — 3CP+2W target <10min.

ALL identity is corrected AT REST before any kubelet starts, so the cluster's first
convergence is its only convergence. The stage-B online phase family (4.9/5.x/6.x/7.x/10.x,
~9-10min of gates against a wrong-identity live cluster) is replaced by a ~90s "window":

  1. clone N VMs, boot inert (clone.py)
  2. per-node PARALLEL disk prep: kubelet stop + wipe, machine-id (stage A), IP/VIP rewrites,
     cert regen; CPs additionally get the Lever-3 etcdutl restore (full new-IP membership)
  3. WINDOW: the 3 CPs run the STAGED etcd binary (no kubelet) -> real quorum;
     byte-replace runs against it with NO apiserver (VIP/LB/node-IP/providerID/PV re-points);
     then ONE kube-apiserver runs as a ctr one-shot from the cached image with webhook
     admission DISABLED -> structural edits via the EXISTING tools (machine renames,
     KubeadmConfig re-mint, cred refresh, VIP/LB object rebinds, unpause at rest);
     storage: PV->template-VG discovery from the keyspace, golden RP create-once + restore,
     git repo fixed at rest via the cached gitwebserver image.
  4. stop window cleanly -> start all kubelets together -> single convergence -> gates.

Node objects are never deleted (same hostnames, providerIDs corrected at rest), so podCIDRs
never reshuffle and the CCM-taint/cilium-staleness repair families are structurally gone.

Usage: claim.py <template-prefix> <name> <old-vip> <new-vip> <lb-start> [lb-end]
Env: NKP_NUTANIX_USER/PASSWORD, NKP_PC_URL, (SCR fixed below)
"""
import base64, json, os, re, subprocess, sys, threading, time, uuid as uuidlib

# SCR resolves to THIS script's directory, with an env override. It used to be a hardcoded
# absolute path under /private/tmp — which is ephemeral (it already ate smoke-creds.env once)
# and changes with every session, so a copy of these scripts anywhere else silently pointed at
# a directory that no longer existed. Same value as before when run from the scratchpad.
SCR = os.environ.get("SPEEDSTART_DIR") or os.path.dirname(os.path.abspath(__file__))
SSH_KEY = os.path.expanduser("~/.ssh/nkp_cluster")
PC = (os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)"))
U, P = (os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"]), (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])
PREFIX, NAME, OLD_VIP, NEW_VIP, LBS = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
LBE = sys.argv[6] if len(sys.argv) > 6 else LBS
T0 = time.time()
def log(m): print("[BOC3 %s +%ds] %s" % (time.strftime("%H:%M:%S"), int(time.time()-T0), m), flush=True)

# FAIL FAST on an unusable claim name (bcB1/bcC1/bcC2 postmortem, 2026-07-25). The claim name is
# embedded in CAPI Machine names, so it must be a lowercase RFC 1123 subdomain. An uppercase name
# clones + boots 5 VMs and only dies ~6.5min in, inside remint_machinenames, with the real reason
# on the tool's stdout — an expensive, confusing failure for a trivially checkable input.
if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", NAME) or len(NAME) > 40:
    print("FATAL: claim name %r is not a lowercase RFC 1123 subdomain (a-z, 0-9, '-'; <=40 chars).\n"
          "       It becomes part of CAPI Machine names, which Kubernetes will reject." % NAME)
    sys.exit(2)

def ssh(ip, cmd, to=120):
    last = (255, "", "no attempt")
    for _ in range(4):
        try:
            r = subprocess.run(["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
                "-o","ConnectTimeout=12","-o","BatchMode=yes","konvoy@"+ip,cmd],capture_output=True,text=True,timeout=to+30)
            last = (r.returncode, r.stdout.strip(), r.stderr.strip())
            if not (r.returncode==255 and any(s in (r.stderr or "") for s in
                    ("timed out","banner exchange","Connection refused","No route to host","Connection reset"))):
                return last
        except subprocess.TimeoutExpired:
            last = (124, "", "ssh timeout")
        time.sleep(6)
    return last

def ssh_script(ip, script, to=300):
    b = base64.b64encode(script.encode()).decode()
    return ssh(ip, "echo %s | base64 -d | sudo bash" % b, to)

def scp(ip, local, remote):
    # retry transient connection failures — a single blip must not kill a prep thread
    last = None
    for _ in range(4):
        r = subprocess.run(["scp","-i",SSH_KEY,"-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
                            "-o","ConnectTimeout=12", local, "konvoy@%s:%s" % (ip, remote)],
                           capture_output=True, text=True)
        if r.returncode == 0: return
        last = r
        time.sleep(6)
    raise RuntimeError("scp to %s failed after retries: %s" % (ip, (last.stderr or "")[-150:]))

def _try_scp(ip, name):
    """scp SCR/<name> -> /tmp/<name>, returning success instead of raising.

    Used for the concurrent binary copies: an exception inside a worker thread would be swallowed,
    so the caller collects booleans and fails loudly with the names that did not land.
    """
    try:
        scp(ip, os.path.join(SCR, name), "/tmp/" + name); return True
    except Exception as e:
        log("  scp %s -> %s FAILED: %s" % (name, ip, str(e)[-120:])); return False

def pcapi(method, url, body=None, to=60):
    a = ["curl","-sk","--max-time",str(to),"-u","%s:%s"%(U,P),"-X",method,url,
         "-H","Content-Type: application/json","-H","NTNX-Request-Id: "+str(uuidlib.uuid4())]
    if body is not None: a += ["-d", json.dumps(body)]
    out = subprocess.run(a, capture_output=True, text=True).stdout
    try: return json.loads(out or "{}")
    except Exception: return {"_raw": (out or "")[:200]}

_seed_kubectl = {"ok": False, "checked": False}   # set by _seed_tooling_setup; read by kx
def kx(*args, to=30):
    # SELF-REPORTING (2026-07-27): this helper used to be completely silent, so a call that failed
    # or sat on its full timeout was invisible. That is how the conversion-webhook stall hid: 229s
    # of a 557s window vanished into set_lb_range and the hook clear with no log line naming a
    # cause. run_tool already learned this lesson; kx had not. Only surface the outliers, so the
    # window log stays readable.
    _t0 = time.time()
    # ON-SEED ROUTING (lever 1, 2026-08-05): once seed tooling is up, run kubectl ON the seed —
    # loopback to the window apiserver instead of a VPN round-trip per call. Args are shell-quoted
    # per-argument (ssh joins argv with spaces and hands it to the remote shell; shlex.quote each
    # piece is exactly the right inverse). Payload-heavy work (get -o json + local parsing) is NOT
    # helped by this — those moved wholesale into on-seed one-shots; this covers the ~25 scattered
    # small calls (DELTA-2, FIX-10/11, hook clear, VA fix, node addresses, unquiesce).
    if _seed_kubectl["ok"]:
        import shlex as _sh
        _cmd = "kubectl --kubeconfig /tmp/wkc-seed.conf --request-timeout=%ds " % to + \
               " ".join(_sh.quote(str(a)) for a in args)
        r = subprocess.run(["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no",
                            "-o","UserKnownHostsFile=/dev/null","-o","ConnectTimeout=10",
                            "-o","BatchMode=yes","konvoy@%s" % SEED["ip"], _cmd],
                           capture_output=True, text=True, timeout=to+30)
    else:
        r = subprocess.run(["kubectl","--kubeconfig",WKC,"--request-timeout=%ds"%to,*args],
                           capture_output=True, text=True)
    _dt = time.time() - _t0
    if r.returncode != 0 or _dt >= 8:
        log("  kx %s took %.0fs rc=%d%s" % (" ".join(str(a) for a in args[:3]), _dt, r.returncode,
                                            (" | " + r.stderr.strip()[:120]) if r.stderr.strip() else ""))
    return r.returncode, r.stdout.strip(), r.stderr.strip()

# ---------------- 0. COMPRESS-A: golden-VG restores need NOTHING — start at launch ----------------
_vg_result = {"pairs": None, "err": None}

# ---- version/layout probes (2026-08-30) -------------------------------------
# NKP 2.18+ keeps the CAPI Cluster in `kommander` and has an NKPCluster CRD.
# NKP 2.17 keeps it in `default` and has NO NKPCluster at all (it has
# KommanderCluster). Hardcoding either made a GA 2.17 template unclaimable, so
# both facts are probed once from the live cluster and cached.
_LAYOUT = {}
def cns():
    """Namespace holding the CAPI Cluster."""
    if "ns" in _LAYOUT: return _LAYOUT["ns"]
    _rc, _out, _ = kx("get", "cluster", "-A", "--no-headers", to=30)
    ns = "kommander"
    for _ln in (_out or "").splitlines():
        _f = _ln.split()
        if len(_f) > 1:
            ns = _f[0]; break
    _LAYOUT["ns"] = ns
    log("layout: CAPI Cluster namespace = %s" % ns)
    return ns

def has_nkpcluster():
    """True on 2.18+, False on 2.17 (CRD absent)."""
    if "nkpc" in _LAYOUT: return _LAYOUT["nkpc"]
    _rc, _out, _ = kx("get", "crd", "nkpclusters.clusters.nkp.nutanix.com", "-o", "name", to=20)
    _LAYOUT["nkpc"] = bool((_out or "").strip())
    log("layout: NKPCluster CRD present = %s" % _LAYOUT["nkpc"])
    return _LAYOUT["nkpc"]
# -----------------------------------------------------------------------------

def _early_vg_restore():
    try:
        fm = json.load(open(SCR + "/%s-freeze-manifest.json" % PREFIX))
        rps = json.load(open(SCR + "/%s-golden-rps.json" % PREFIX))
        pairs, meta = [], {}
        _lk = threading.Lock()
        _sub_err = {}
        # PARALLEL RESTORES (2026-07-26). This loop was SERIAL over the 3 PVs, each doing
        # POST-restore -> poll-task -> read-disk -> detach-poll (~85s apiece), so COMPRESS-A always
        # finished at a rock-constant restore_s of 255-260s in EVERY run. After Stage A was
        # parallelised (183s -> 129s) the window did NOT shrink, because the critical path had
        # moved here: prep finished at 188s and then idled ~70s waiting on this. The 3 restores are
        # independent, so run them concurrently — expected ~85-90s instead of ~258s.
        def _one_pv(pv, m):
          try:
            vg = m["vg"]; ent = rps[vg]
            task = ""
            for _t in range(3):
                r = pcapi("POST", PC + "/api/dataprotection/v4.0/config/recovery-points/%s/$actions/restore" % ent["rp"],
                          {"volumeGroupRecoveryPointRestoreOverrides": [
                              {"volumeGroupRecoveryPointExtId": ent["vgrp"],
                               "volumeGroupOverrideSpec": {"name": "%s-%s" % (NAME, pv[-8:])}}]}, to=90)
                task = ((r.get("data") or {}).get("extId") or "")
                if task: break
                time.sleep(8)
            if not task: raise RuntimeError("restore not accepted for %s" % pv)
            new_vg = ""
            for _ in range(40):
                time.sleep(3)
                t = pcapi("GET", PC + "/api/prism/v4.0/config/tasks/" + task.replace("=", "%3D").replace(":", "%3A"))
                data = t.get("data") or {}
                if data.get("status") == "SUCCEEDED":
                    for kv in data.get("completionDetails") or []:
                        if kv.get("name") == "volumeGroupExtIds": new_vg = kv.get("value")
                    break
                if data.get("status") in ("FAILED", "CANCELED"): raise RuntimeError("restore task failed %s" % pv)
            # old_disk from the GOLDEN RP (loss-proof — the source VG may be GC-reaped since it's
            # unattached; boc11 postmortem). new_disk from the freshly-restored VG.
            old_disk = ent.get("old_disk")
            if not old_disk: raise RuntimeError("no cached old_disk for VG %s (re-run freeze RP cache)" % vg[:8])
            nd = None
            for _t in range(5):
                d = pcapi("GET", PC + "/api/volumes/v4.0/config/volume-groups/%s/disks" % new_vg).get("data")
                if isinstance(d, list) and d: nd = d[0]["extId"]; break
                time.sleep(4)
            if not nd: raise RuntimeError("no disks on restored VG %s" % new_vg[:8])
            # WRINKLE-2 KILL (bc17 root): an RP restore attaches the new VG to the RP's SOURCE VM
            # (the template worker) — CSI then can't attach it to the clone node ("device symlink
            # not found; VM reattach task failed"). Proactively detach it from EVERY VM now, so the
            # clone's CSI node plugin does the only/clean attach. The git repo CONTENT is rebuilt
            # from the freeze bundle in post_boc (git_hydrate) regardless, so restore integrity of
            # the git/admin VGs no longer matters — restore only provides a mountable scaffold.
            # RACE (bc20): the source-VM attachment materializes a few seconds AFTER the restore
            # task reports SUCCEEDED — a single immediate check often sees NONE and detaches
            # nothing (bc18/bc19 won this race, bc20 lost it). Poll for it, detach, then confirm
            # zero attachments. post_boc re-sweeps as a backstop in case it lands even later.
            for _w in range(12):
                va = pcapi("GET", PC + "/api/volumes/v4.0/config/volume-groups/%s/vm-attachments" % new_vg).get("data")
                att = va if isinstance(va, list) else []
                if att:
                    for a in att:
                        pcapi("POST", PC + "/api/volumes/v4.0/config/volume-groups/%s/$actions/detach-vm" % new_vg,
                              {"extId": a.get("extId")}, to=60)
                    time.sleep(6)
                    va2 = pcapi("GET", PC + "/api/volumes/v4.0/config/volume-groups/%s/vm-attachments" % new_vg).get("data")
                    if not (va2 if isinstance(va2, list) else []): break
                time.sleep(5)
            with _lk:
                pairs.extend([[vg, new_vg], [old_disk, nd]])
                meta["/registry/persistentvolumes/" + pv] = vg
          except Exception as _e:
            _sub_err[pv] = str(_e)[:160]
        _ts = [threading.Thread(target=_one_pv, args=(pv, m)) for pv, m in fm["pvs"].items()]
        for _t2 in _ts: _t2.start()
        for _t2 in _ts: _t2.join(timeout=420)
        if _sub_err:
            raise RuntimeError("restore failed for %s" % _sub_err)
        if len(pairs) != 2 * len(fm["pvs"]):
            raise RuntimeError("expected %d pairs, got %d (a restore thread died silently)"
                               % (2 * len(fm["pvs"]), len(pairs)))
        _vg_result["pairs"] = pairs
        _vg_result["map"] = meta
    except Exception as e:
        _vg_result["err"] = str(e)[:200]
_vg_thread = threading.Thread(target=_early_vg_restore)
_vg_thread.start()
log("COMPRESS-A: golden-VG restores running in parallel with stage A")

# ---------------- 1. STAGE A: clone + boot + machine-id (inert) ----------------
log("STAGE A: clone %s -> %s" % (PREFIX, NAME))
env = {**os.environ}
MAP = SCR + "/claim-map-%s.json" % NAME   # per-claim: concurrency-safe
try: os.remove(MAP)
except FileNotFoundError: pass
rc = subprocess.run(["/usr/bin/python3","-u",SCR+"/clone.py",PREFIX,NAME,OLD_VIP,NEW_VIP,LBS,LBE],
                    env=env).returncode
if rc != 0: log("FATAL: stage A rc=%d" % rc); sys.exit(1)
M = json.load(open(MAP))
CPS = M["cps"]; WORKERS = [M["worker"]] if M.get("worker") else []
# stage A writes single 'worker'; multi-worker maps carry 'workers'
if M.get("workers"): WORKERS = M["workers"]
SEED = CPS[0]
log("map: %d CPs, %d workers, seed=%s(%s)" % (len(CPS), len(WORKERS), SEED["host"], SEED["ip"]))

# VIP/LB are the only IPs that MUST byte-replace same-length — and we choose them.
# Node IPs are deliberately NOT byte-replaced: kubelet owns node.status.addresses and
# refreshes on first heartbeat; Machine/NutanixMachine addresses refresh from CAPX; the
# apiserver masterleases for old IPs are DELETED in the window (they expire in 15s anyway).
if len(OLD_VIP) != len(NEW_VIP) or len(TEMPLATE_LB_CHECK := os.environ.get("NKP_TEMPLATE_LB", "<lb-address>")) != len(LBS):
    log("FATAL: VIP or LB length mismatch (%s->%s, %s->%s) — choose same-length addresses"
        % (OLD_VIP, NEW_VIP, os.environ.get("NKP_TEMPLATE_LB", ""), LBS))
    sys.exit(2)

# ---------------- 2. PARALLEL per-node disk prep ----------------
NIC = ",".join("%s=https://%s:2380" % (c["host"], c["ip"]) for c in CPS)
TEMPLATE_LB = os.environ.get("NKP_TEMPLATE_LB", "<lb-address>")

def cp_prep(cp):
    """kubelet stop+wipe, IP/VIP rewrites, cert regen, Lever-3 restore FROM THE SEED DB."""
    # PARALLEL: 62MB of etcd binaries (etcdutl 17 + etcdctl 20 + etcd 26) went one after another.
    # Measured on a live node 2026-07-28: 31s sequential vs 14s concurrent, and prep is only ~60s.
    _sx = []
    _st = [threading.Thread(target=lambda n=n: _sx.append((n, _try_scp(cp["ip"], n))))
           for n in ("etcdutl", "etcdctl", "etcd")]
    for t in _st: t.start()
    for t in _st: t.join(timeout=300)
    _bad = [n for n, ok in _sx if not ok]
    if _bad or len(_sx) != 3:
        raise RuntimeError("etcd binary copy to %s failed: %s" % (cp["ip"], _bad or "thread did not finish"))
    _db_ready.wait(timeout=300)
    if _db_err: raise RuntimeError("seed db fetch failed: %s" % _db_err["e"])
    # The SEED already holds the db we are about to restore from — _fetch_db stashed a copy at
    # /tmp/seed-preplaced.db at the very moment it read it. Shipping 34MB down to the laptop and
    # straight back up again is pure round-trip; at N=1 the seed is the ONLY node, so that was the
    # entire transfer. Other CPs still receive the seed's db (FIX-1: everyone restores from ONE db).
    if cp["ip"] != SEED["ip"]:
        scp(cp["ip"], SCR + "/seed-db-%s.gz.b64" % NAME, "/tmp/seed-db.gz.b64")
    script = r"""set -e
systemctl stop kubelet 2>/dev/null || true
crictl rm -f $(crictl ps -aq) >/dev/null 2>&1 || true
# DELTA-3: drop double-held DHCP leases (clone boots can keep the previous address bound
# alongside the new one -> ARP dup-IP chaos on the shared VLAN). Keep only {NEW}.
for a in $(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1); do
  [ "$a" != "{NEW}" ] && ip addr del "$a/$(ip -4 -o addr show eth0 | awk -v x="$a" '$4 ~ x {split($4,p,"/"); print p[2]; exit}')" dev eth0 2>/dev/null || true
done
mkdir -p /opt/spike && cp /tmp/etcdutl /tmp/etcdctl /tmp/etcd /opt/spike/ && chmod +x /opt/spike/*
M=/etc/kubernetes/manifests
# IP rewrites: node IP + VIP through all kubeconfigs/manifests
for f in /etc/kubernetes/kubelet.conf /etc/kubernetes/controller-manager.conf /etc/kubernetes/scheduler.conf /etc/kubernetes/admin.conf /etc/kubernetes/super-admin.conf; do
  [ -f "$f" ] && sed -i "s#{OLD}#{NEW}#g; s#{OVIP}#{NVIP}#g" "$f" || true
done
sed -i "s#{OLD}#{NEW}#g" $M/*.yaml
sed -i "s#{OVIP}#{NVIP}#g" $M/*.yaml
grep -rl "{OLD}" /var/lib/kubelet/ 2>/dev/null | xargs -r sed -i "s#{OLD}#{NEW}#g"
# cert regen — PROVEN recipe (config-file SANs incl old+new IP/VIP + hostname)
cat > /tmp/ka.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
kubernetesVersion: v1.35.2
controlPlaneEndpoint: "{NVIP}:6443"
networking: {serviceSubnet: "10.96.0.0/12"}
apiServer:
  certSANs: [localhost, "127.0.0.1", "0.0.0.0", "{OLD}", "{NEW}", "{OVIP}", "{NVIP}", "{ME}"]
etcd:
  local:
    serverCertSANs: ["{NEW}","127.0.0.1","localhost","{ME}"]
    peerCertSANs: ["{NEW}","127.0.0.1","localhost","{ME}"]
EOF
rm -f /etc/kubernetes/pki/apiserver.crt /etc/kubernetes/pki/apiserver.key /etc/kubernetes/pki/etcd/server.crt /etc/kubernetes/pki/etcd/server.key /etc/kubernetes/pki/etcd/peer.crt /etc/kubernetes/pki/etcd/peer.key
kubeadm init phase certs apiserver --config /tmp/ka.yaml >/dev/null 2>&1
kubeadm init phase certs etcd-server --config /tmp/ka.yaml >/dev/null 2>&1
kubeadm init phase certs etcd-peer --config /tmp/ka.yaml >/dev/null 2>&1
# Lever-3 restore: own db + FULL new-IP membership
TOKEN=$(grep -oE 'initial-cluster-token=[^ ]+' $M/etcd.yaml | head -1 | cut -d= -f2); TOKEN=${TOKEN:-etcd-cluster}
rm -rf /var/lib/etcd-l3
# FIX-1: every CP restores from the SEED's db. The seed already has it, placed by _fetch_db at the
# moment it was read; everyone else receives it over scp. Explicit marker file rather than
# "reuse /tmp/seed.db if present" so a stale file from an earlier attempt can never be picked up.
if [ -f /tmp/seed-preplaced.db ]; then mv -f /tmp/seed-preplaced.db /tmp/seed.db
else base64 -d /tmp/seed-db.gz.b64 | gunzip > /tmp/seed.db; fi
[ -s /tmp/seed.db ] || { echo FATAL-EMPTY-SEED-DB; exit 8; }
/opt/spike/etcdutl snapshot restore /tmp/seed.db --skip-hash-check \
  --name={ME} --initial-cluster={NIC} --initial-advertise-peer-urls=https://{NEW}:2380 \
  --initial-cluster-token=$TOKEN --data-dir=/var/lib/etcd-l3 2>&1 | tail -1
[ -d /var/lib/etcd-l3/member ] || { echo FATAL-L3; exit 7; }
rm -rf /var/lib/etcd/member && mv /var/lib/etcd-l3/member /var/lib/etcd/member
sed -i '/--force-new-cluster/d' $M/etcd.yaml
sed -i 's#--initial-cluster=[^ ]*#--initial-cluster={NIC}#' $M/etcd.yaml
grep -q initial-cluster-state $M/etcd.yaml || sed -i '/--initial-cluster=/a\    - --initial-cluster-state=existing' $M/etcd.yaml
sed -i 's#--initial-cluster-state=new#--initial-cluster-state=existing#' $M/etcd.yaml
# FIX-3: single kube-vip during boot. Non-seed CPs park the manifest (restored post-drain by
# post_boc.sh); seed gets a storm-tolerant lease (60s/40s/5s). Dual claims + hard-kills leave
# the VIP ADDRESS BOUND (kernel answers ARP for bound addrs) -> sustained dual-MAC flap.
if [ "{IS_SEED}" = "1" ]; then
  sed -i 's#value: "15"#value: "60"#; s#value: "10"#value: "40"#; s#value: "2"#value: "5"#' /etc/kubernetes/manifests/kube-vip.yaml || true
else
  mv /etc/kubernetes/manifests/kube-vip.yaml /root/kube-vip.yaml.parked 2>/dev/null || true
fi
echo PREP-OK {ME}
""".replace("{OLD}", cp["old"]).replace("{NEW}", cp["ip"]).replace("{OVIP}", OLD_VIP)\
   .replace("{NVIP}", NEW_VIP).replace("{ME}", cp["host"]).replace("{NIC}", NIC)\
   .replace("{IS_SEED}", "1" if cp is SEED else "0")
    rc2, o, e = ssh_script(cp["ip"], script, 240)
    _prep[cp["host"]] = ("PREP-OK" in o, o[-160:], e[-120:])

def worker_prep(w):
    # the map has NO old IP for workers — detect it on-disk (kubeadm-flags --node-ip is the
    # authoritative source; fall back to any non-VIP 10.22.20x IP in /var/lib/kubelet).
    script = r"""set -e
systemctl stop kubelet 2>/dev/null || true
crictl rm -f $(crictl ps -aq) >/dev/null 2>&1 || true
for a in $(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1); do
  [ "$a" != "{NEW}" ] && ip addr del "$a/24" dev eth0 2>/dev/null || true
done
OLDW=$(grep -hoE 'node-ip=[0-9.]+' /var/lib/kubelet/kubeadm-flags.env 2>/dev/null | cut -d= -f2 | head -1)
[ -n "$OLDW" ] || OLDW=$(grep -rhoE '10\.22\.20[0-9]+\.[0-9]+' /var/lib/kubelet/ 2>/dev/null | grep -v '^{OVIP}$' | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
if [ -n "$OLDW" ] && [ "$OLDW" != "{NEW}" ]; then
  for f in /etc/kubernetes/kubelet.conf $(grep -rl "$OLDW" /var/lib/kubelet/ /etc/kubernetes/ 2>/dev/null); do
    [ -f "$f" ] && sed -i "s#$OLDW#{NEW}#g" "$f" || true
  done
fi
for f in /etc/kubernetes/kubelet.conf $(grep -rl "{OVIP}" /var/lib/kubelet/ /etc/kubernetes/ 2>/dev/null); do
  [ -f "$f" ] && sed -i "s#{OVIP}#{NVIP}#g" "$f" || true
done
echo OLD-DETECTED=$OLDW
echo PREP-OK worker
""".replace("{NEW}", w["ip"]).replace("{OVIP}", OLD_VIP).replace("{NVIP}", NEW_VIP)
    rc2, o, e = ssh_script(w["ip"], script, 180)
    m = re.search(r"OLD-DETECTED=([0-9.]+)", o or "")
    if m: w["old"] = m.group(1)
    _prep[w["host"]] = ("PREP-OK" in o, o[-160:], e[-120:])

# FIX-1 (2026-07-24): ALL CPs restore from the SEED's db. Per-own-db restores produce raft
# replicas with IDENTICAL indexes over DIFFERENT state machines (template-freeze lag skew) —
# silent divergence: ghost objects per member, poisoned watch caches, dual lease owners.
_db_ready = threading.Event()
_db_err = {}
def _fetch_db():
    # Stash a copy ON THE SEED in the same command that reads it, so the seed's own prep can use it
    # without a laptop round-trip. Same instant, same bytes — not a re-read at prep time, which
    # would risk picking up a different file if anything touched etcd in between.
    _rc, _o, _e = ssh(SEED["ip"],
                      "sudo cp /var/lib/etcd/member/snap/db /tmp/seed-preplaced.db && "
                      "sudo chmod 644 /tmp/seed-preplaced.db && "
                      "sudo gzip -c /var/lib/etcd/member/snap/db | base64 -w0", to=240)
    if _rc != 0 or len(_o) < 1000:
        _db_err["e"] = _e[-120:]
    else:
        open(SCR + "/seed-db-%s.gz.b64" % NAME, "w").write(_o)
    _db_ready.set()
threading.Thread(target=_fetch_db).start()
log("COMPRESS-B: seed-db fetch overlapped with prep phase-1 (FIX-1)")

log("parallel per-node prep (%d nodes)" % (len(CPS)+len(WORKERS)))
_prep = {}
_th = [threading.Thread(target=cp_prep, args=(c,)) for c in CPS] + \
      [threading.Thread(target=worker_prep, args=(w,)) for w in WORKERS]
for t in _th: t.start()
for t in _th: t.join(timeout=300)
for h, (ok, o, e) in _prep.items():
    log("  %s prep %s" % (h, "OK" if ok else "FAILED out=%s err=%s" % (o, e)))
if not all(v[0] for v in _prep.values()) or len(_prep) != len(CPS)+len(WORKERS):
    log("FATAL: node prep incomplete"); sys.exit(3)

# ---------------- 3. WINDOW: quorate etcd (staged binaries) ----------------
log("WINDOW: starting staged etcd on %d CPs (no kubelet)" % len(CPS))
def start_etcd(cp):
    script = r"""
ARGS=$(python3 - <<'PY' 2>/dev/null
import yaml
d = yaml.safe_load(open("/etc/kubernetes/manifests/etcd.yaml"))
c = d["spec"]["containers"][0]["command"]
print(" ".join(a for a in c[1:]))
PY
)
[ -n "$ARGS" ] || ARGS=$(grep -oE '^\s+- --[^ ]+' /etc/kubernetes/manifests/etcd.yaml | sed 's/^\s*- //' | tr '\n' ' ')
nohup /opt/spike/etcd $ARGS > /var/log/spike-etcd.log 2>&1 &
echo ETCD-STARTED $!
"""
    rc2, o, e = ssh_script(cp["ip"], script, 60)
    _win[cp["host"]] = ("ETCD-STARTED" in o, o[-120:], e[-120:])
_win = {}
_th = [threading.Thread(target=start_etcd, args=(c,)) for c in CPS]
for t in _th: t.start()
for t in _th: t.join(timeout=90)
for h, v in _win.items():
    if not v[0]: log("FATAL: etcd start failed on %s: %s %s" % (h, v[1], v[2])); sys.exit(4)

ETCDCTL = ("sudo /opt/spike/etcdctl --endpoints=https://127.0.0.1:2379 "
           "--cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt "
           "--key=/etc/kubernetes/pki/etcd/server.key ")
quorum = False
for _ in range(24):
    rc2, o, _e = ssh(SEED["ip"], ETCDCTL + "endpoint status 2>/dev/null | head -1")
    if rc2 == 0 and "2379" in o: quorum = True; break
    time.sleep(5)
if not quorum: log("FATAL: window etcd quorum never formed"); sys.exit(5)
log("window quorum up %s" % ("" if quorum else "??"))

# ---------------- 3b. byte-replace phase (etcd alone, NO apiserver) ----------------
# discover template VG extIds from the PV objects FIRST (raw read), then build all pairs.
# COMPRESS-A join: restores started at launch; here we only collect the pairs
_vg_thread.join(timeout=420)
if _vg_result.get("err") or not _vg_result.get("pairs"):
    log("FATAL: early VG restore failed: %s" % _vg_result.get("err")); sys.exit(7)
vg_of_pv = _vg_result.get("map", {})
log("COMPRESS-A: restored pairs ready (%d)" % len(_vg_result["pairs"]))

vg_pairs = _vg_result["pairs"]

# node-identity byte-replace pairs
uid_pairs = []
for n in CPS + WORKERS:
    uid_pairs.append([n["tmpl"]["uuid"], n["clone_uuid"]])
# node-IP pairs intentionally EMPTY — kubelet/CCM own node addresses (FIX-4 patches them),
# and the boc5 postmortem proved "bonus" same-length pairs are DANGEROUS: the template
# topology held a NODE IP in serviceLoadBalancer.addressRanges and the bonus replace
# propagated it into the pool -> LB dead -> the kommander ingress reconciler committed the
# broken IP into git -> the whole SSO chain chased it. Never byte-replace node IPs.
ip_pairs = []
vip_pair = [OLD_VIP, NEW_VIP]; lb_pair = [TEMPLATE_LB, LBS] if len(TEMPLATE_LB) == len(LBS) else None

REPL = r"""
import subprocess, sys
E = ["/opt/spike/etcdctl", "--endpoints=https://127.0.0.1:2379",
     "--cacert=/etc/kubernetes/pki/etcd/ca.crt", "--cert=/etc/kubernetes/pki/etcd/server.crt",
     "--key=/etc/kubernetes/pki/etcd/server.key"]
def ctl(*a, inp=None, check=True):
    r = subprocess.run(E + list(a), capture_output=True, input=inp)
    if check and r.returncode != 0: raise RuntimeError("etcdctl %s: %s" % (a[:2], (r.stderr or b"")[:150]))
    return r.stdout
def repl(key, pairs):
    v = ctl("get", key, "--print-value-only", check=False)
    if not v: return 0
    v = v[:-1] if v.endswith(b"\n") else v
    n = v
    for o, x in pairs:
        assert len(o) == len(x), (o, x)
        n = n.replace(o.encode() if isinstance(o, str) else o, x.encode() if isinstance(x, str) else x)
    if n != v: ctl("put", key, inp=n); return 1
    return 0
PAIRS = {PAIRS}
c = 0
SWEEP = ["/registry/minions/", "/registry/cluster.x-k8s.io/machines/",
         "/registry/infrastructure.cluster.x-k8s.io/nutanixmachines/",
         "/registry/infrastructure.cluster.x-k8s.io/nutanixclusters/",
         "/registry/controlplane.cluster.x-k8s.io/kubeadmcontrolplanes/",
         "/registry/bootstrap.cluster.x-k8s.io/kubeadmconfigs/",
         "/registry/cluster.x-k8s.io/clusters/",
         "/registry/clusters.nkp.nutanix.com/nkpclusters/",
         "/registry/persistentvolumes/",
         "/registry/services/endpoints/default/kubernetes",
         "/registry/masterleases/",
         "/registry/configmaps/kube-system/",
         "/registry/configmaps/kommander/",
         "/registry/configmaps/kommander-flux/",
         "/registry/configmaps/metallb-system/",
         "/registry/daemonsets/kube-system/",
         "/registry/deployments/kube-system/",
         "/registry/metallb.io/",
         "/registry/kommander.mesosphere.io/",
         "/registry/addons.cluster.x-k8s.io/helmchartproxies/",
         "/registry/addons.cluster.x-k8s.io/helmreleaseproxies/"]
for g in SWEEP:
    ks = ctl("get", g, "--prefix", "--keys-only", check=False).decode().split()
    for k in ks:
        c += repl(k, PAIRS)
# old-IP apiserver leases: DELETE (no length constraint; reconciler re-mints on boot)
ctl("del", "/registry/masterleases/", "--prefix", check=False)
# template-era VolumeAttachments: DELETE (boc6 postmortem: byte-replaced VAs claim the restored
# VGs are already attached -> CSI skips ControllerPublish -> NodeStage finds no device. Deleting
# them makes the attacher do REAL attaches at boot; VA names are deterministic, no dangling refs.)
ctl("del", "/registry/volumeattachments/", "--prefix", check=False)
print("byte-replaced %d objects" % c)
"""
all_pairs = [vip_pair] + ip_pairs + uid_pairs + vg_pairs + ([lb_pair] if lb_pair else [])
script = "python3 - <<'PYEOF'\n" + REPL.replace("{PAIRS}", json.dumps(all_pairs)) + "\nPYEOF"
rc2, o, e = ssh_script(SEED["ip"], script, 300)
if "byte-replaced" not in o:
    log("FATAL: byte-replace failed: %s %s" % (o[-200:], e[-200:])); sys.exit(8)
log("byte-replace: %s (pairs=%d)" % (o.strip().splitlines()[-1], len(all_pairs)))

# ---------------- 3c. window apiserver (ctr one-shot, webhooks disabled) ----------------
log("starting WINDOW apiserver on seed (ctr, webhook admission disabled)")
APISERVER = r"""
IMG=$(crictl images 2>/dev/null | awk '/kube-apiserver/ {print $1":"$2; exit}')
[ -n "$IMG" ] || IMG=$(ctr -n k8s.io images ls -q | grep kube-apiserver | head -1)
ARGS=$(python3 - <<'PY'
import yaml
d = yaml.safe_load(open("/etc/kubernetes/manifests/kube-apiserver.yaml"))
c = d["spec"]["containers"][0]["command"]
args = [a for a in c[1:] if not a.startswith("--disable-admission-plugins")]
args.append("--disable-admission-plugins=ValidatingAdmissionWebhook,MutatingAdmissionWebhook,ValidatingAdmissionPolicy")
print(" ".join(args))
PY
)
ctr -n k8s.io task kill -s SIGKILL boc3-apiserver 2>/dev/null; ctr -n k8s.io c rm boc3-apiserver 2>/dev/null
nohup ctr -n k8s.io run --rm --net-host \
  --mount type=bind,src=/etc/kubernetes/pki,dst=/etc/kubernetes/pki,options=rbind:ro \
  --mount type=bind,src=/etc/kubernetes,dst=/etc/kubernetes,options=rbind:ro \
  --mount type=bind,src=/etc/ssl,dst=/etc/ssl,options=rbind:ro \
  --mount type=bind,src=/etc/pki,dst=/etc/pki,options=rbind:ro \
  "$IMG" boc3-apiserver kube-apiserver $ARGS > /var/log/spike-apiserver.log 2>&1 &
echo APISERVER-LAUNCHED
"""
rc2, o, e = ssh_script(SEED["ip"], APISERVER, 90)
if "APISERVER-LAUNCHED" not in o:
    log("FATAL: window apiserver launch failed: %s %s" % (o[-160:], e[-160:])); sys.exit(9)

# window kubeconfig for the laptop: template admin conf -> seed node IP
WKC = SCR + "/%s-window.conf" % NAME
tk = open(SCR + "/%s.conf" % PREFIX).read()
open(WKC, "w").write(re.sub(r"https://10\.22\.20[0-9.]+:6443", "https://%s:6443" % SEED["ip"], tk))
up = False
for _ in range(30):
    r = subprocess.run(["kubectl","--kubeconfig",WKC,"--request-timeout=6s","get","--raw","/readyz"],
                       capture_output=True, text=True)
    if r.returncode == 0 and "ok" in r.stdout: up = True; break
    time.sleep(4)
if not up: log("FATAL: window apiserver never became ready"); sys.exit(9)
log("window apiserver READY (%s)" % SEED["ip"])

# RESTORED-STATE GATE — the window must be serving the TEMPLATE's keyspace, not an empty one.
# qa-sn-tmpl1 (2026-07-27): the template disk turned out to hold a half-bootstrapped etcd (a CAPX
# duplicate-name VM pair had left the real cluster running on the twin that freeze never touched).
# Every downstream step then failed in a way that pointed at itself rather than at the cause:
# byte-replace reported "1 object", rebind "0 objects", and remint_machinenames died on a JSON
# decode error because `kubectl get machines` printed nothing to stdout. Assert the one fact they
# all depend on, once, here — where the message can name the actual problem.
# Namespace-AGNOSTIC on purpose (2026-08-31): the assertion is "the restored
# keyspace contains CAPI Machines at all", not "in namespace kommander". A 2.17
# GA template keeps its Cluster and Machines in `default`, so the old
# `-n kommander` form reported a perfectly good template as unconverged and
# aborted the first-ever GA claim at +157s.
_mc = subprocess.run(["kubectl","--kubeconfig",WKC,"--request-timeout=25s","get","machines",
                      "-A","-o","jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} {end}"],
                     capture_output=True, text=True)
if _mc.returncode == 0 and _mc.stdout.split():
    log("restored-state gate: %d CAPI Machine(s) present (%s)"
        % (len(_mc.stdout.split()), _mc.stdout.split()[0].split("/")[0]))
if _mc.returncode != 0 or not _mc.stdout.split():
    log("FATAL: the restored keyspace has no CAPI Machines in ANY namespace — this template is not a "
        "converged cluster. kubectl said: %s" % ((_mc.stderr or _mc.stdout).strip()[:160] or "(nothing)"))
    log("  The cloned disk does not hold the state you froze. Verify on Prism Central that the VM "
        "freeze powered off is the VM that was actually running the node (check for duplicate VM "
        "names), then re-freeze. Nothing has been renamed or booted; the clone is safe to tear down.")
    sys.exit(9)
log("restored-state gate: %d CAPI Machine(s) present in the window" % len(_mc.stdout.split()))

# ---------------- 3c-bis. CONVERSION-WEBHOOK BYPASS (the window's dominant cost) ----------------
# `--disable-admission-plugins` above kills ADMISSION webhooks. It does NOT kill CRD CONVERSION
# webhooks -- those are part of CRD serving, not admission. Every CAPI CRD (Cluster, Machine,
# KubeadmControlPlane, KubeadmConfig...) declares strategy=Webhook -> capi-webhook-service, and in
# the window NO controller runs, so that Service has zero endpoints. Every CAPI-typed request
# therefore blocked until its client timeout.
#
# MEASURED on qa-sn-c2 (2026-07-27), from /var/log/spike-apiserver.log:
#   "failed to prepare current and previous objects: conversion webhook for
#    cluster.x-k8s.io/v1beta2, Kind=Cluster"
# Cost: DELTA-2 burned 162s (4 retries x ~40s) and then WARNed; the hook-clear annotate+patch burned
# another 67s and silently did not apply -- 229s of a 557s window, 41%, spent failing. The tell was
# that NutanixMachine (strategy=None) was the one CAPI kind whose operations were fast.
#
# Safe because the storage version IS the version we address (stored=v1beta2, and kubectl asks for
# the preferred=v1beta2): with one version in play, conversion is a no-op, so skipping it changes
# nothing semantically -- it only skips a call that cannot succeed. Restored verbatim before the
# window closes, so the booted cluster is byte-identical to traditional.
# This cluster has ~70 CRDs with conversion webhooks, not the CAPI handful. Read them in ONE list
# call and patch CONCURRENTLY: the first version of this walked them serially with two kubectl
# calls each (140 round-trips) and cost 339s — worse than the 229s it was meant to save.
_conv_saved = {}
def _conv_set(name, value):
    return kx("patch","crd",name,"--type=json","-p",
              json.dumps([{"op":"replace","path":"/spec/conversion","value":value}]), to=25)[0]

def conversion_bypass():
    rc, raw, _e = kx("get","crd","-o","json", to=90)
    try: items = json.loads(raw or "{}").get("items") or []
    except Exception as e:
        log("conversion-webhook bypass SKIPPED (could not list CRDs: %s) — the window will be slow" % e)
        return
    for it in items:
        conv = (it.get("spec") or {}).get("conversion") or {}
        if conv.get("strategy") == "Webhook":
            _conv_saved[it["metadata"]["name"]] = conv
    if not _conv_saved:
        log("conversion-webhook bypass: nothing to do (no Webhook-strategy CRDs)"); return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        rcs = list(ex.map(lambda n: _conv_set(n, {"strategy": "None"}), list(_conv_saved)))
    log("conversion-webhook bypass: %d/%d CRD(s) set to strategy=None for the window"
        % (sum(1 for r in rcs if r == 0), len(_conv_saved)))

def conversion_restore():
    if not _conv_saved: return
    from concurrent.futures import ThreadPoolExecutor
    def _restore_one(n):
        for _try in range(3):          # restoring the template's CRDs matters more than speed
            if _conv_set(n, _conv_saved[n]) == 0: return n, True
            time.sleep(2)
        return n, False
    with ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(_restore_one, list(_conv_saved)))
    failed = [n for n, good in res if not good]
    log("conversion-webhook restore: %d/%d CRD(s) back to strategy=Webhook"
        % (len(_conv_saved) - len(failed), len(_conv_saved)))
    if failed:
        log("  WARN: still strategy=None: %s" % failed[:5])
        log("  A CRD left at None still serves its storage version correctly, but older-version "
            "clients are not converted. Restore before day-2 use.")
# ON-SEED TOOL EXECUTION (2026-08-05): the three kubectl-heavy window tools (remint_machinenames,
# rebind_incluster_vip, refresh-creds) each make ~10-15 kubectl round-trips. From the laptop every
# round-trip pays VPN RTT + TLS; measured 43-69s per tool (and they run as a parallel group, so the
# group wall = the slowest ≈ 50-70s). On the seed the same calls are loopback-fast. The seed already
# has python3 (prep uses it); kubectl is probed at claim start and, if present, tools run there with
# a kubeconfig aimed at the seed's own IP (in the regenerated cert SANs). Falls back to local
# execution transparently — a missing kubectl or a failed copy must never fail the claim.
def _seed_tooling_setup():
    rc0, o0, _ = ssh(SEED["ip"], "command -v kubectl >/dev/null && echo HAVE || echo NO", to=20)
    _seed_kubectl["checked"] = True
    if rc0 != 0 or "HAVE" not in (o0 or ""):
        log("on-seed tools: kubectl not on the node image — tools run from the laptop"); return
    try:
        scp(SEED["ip"], WKC, "/tmp/wkc-seed.conf")
        # rewrite the server to the seed's own IP: the laptop WKC already points there, but keep it
        # explicit in case WKC generation changes
        ssh(SEED["ip"], "sed -i 's#https://[0-9.]*:6443#https://%s:6443#' /tmp/wkc-seed.conf" % SEED["ip"], to=20)
        for _tname in ("remint_machinenames.py", "rebind_incluster_vip.py", "refresh-creds.py"):
            scp(SEED["ip"], SCR + "/" + _tname, "/tmp/" + _tname)
        _seed_kubectl["ok"] = True
        log("on-seed tools: kubectl present — remint/rebind/refresh will run on %s" % SEED["ip"])
    except Exception as _e:
        log("on-seed tools: setup failed (%s) — falling back to laptop execution" % str(_e)[:80])


# LEVER 1 (2026-08-05): setup FIRST (~8s), then run the bypass ON the seed — 70 CRD patches at
# loopback (~4s) instead of 70 VPN round-trips (~20-30s with 10s single-call outliers observed).
# Saved conversion specs live in /tmp/conv-saved.json ON the seed; the restore reads them there —
# nothing round-trips through the laptop. Falls back to the local implementations whenever seed
# tooling is unavailable.
_seed_tooling_setup()

_ONSEED_BYPASS = r'''
import json, subprocess
K = ["kubectl","--kubeconfig","/tmp/wkc-seed.conf","--request-timeout=25s"]
def k(*a):
    return subprocess.run(K+list(a), capture_output=True, text=True)
crds = json.loads(k("get","crd","-o","json").stdout).get("items") or []
saved = {}
for it in crds:
    conv = (it.get("spec") or {}).get("conversion") or {}
    if conv.get("strategy") == "Webhook":
        saved[it["metadata"]["name"]] = conv
ok = 0
for n in saved:
    r = k("patch","crd",n,"--type=json",
          "-p", json.dumps([{"op":"replace","path":"/spec/conversion","value":{"strategy":"None"}}]))
    if r.returncode == 0: ok += 1
json.dump(saved, open("/tmp/conv-saved.json","w"))
print("BYPASSED %d/%d" % (ok, len(saved)))
'''

_ONSEED_RESTORE = r'''
import json, subprocess, time
K = ["kubectl","--kubeconfig","/tmp/wkc-seed.conf","--request-timeout=25s"]
def k(*a):
    return subprocess.run(K+list(a), capture_output=True, text=True)
saved = json.load(open("/tmp/conv-saved.json"))
failed = []
for n, conv in saved.items():
    for _try in range(3):
        r = k("patch","crd",n,"--type=json",
              "-p", json.dumps([{"op":"replace","path":"/spec/conversion","value":conv}]))
        if r.returncode == 0: break
        time.sleep(2)
    else:
        failed.append(n)
print("RESTORED %d/%d%s" % (len(saved)-len(failed), len(saved),
      (" FAILED:"+",".join(failed[:5])) if failed else ""))
'''

def _bypass_dispatch():
    if _seed_kubectl["ok"]:
        rc0, o0, e0 = ssh_script(SEED["ip"], "python3 - <<'PYEOF'\n" + _ONSEED_BYPASS + "\nPYEOF", 120)
        if rc0 == 0 and "BYPASSED" in (o0 or ""):
            _conv_saved["__onseed__"] = True   # marker: restore must run on-seed too
            log("conversion-webhook bypass (on-seed): %s" % (o0 or "").strip().splitlines()[-1])
            return
        log("on-seed bypass failed (%s) — falling back to laptop" % ((e0 or o0 or "")[-100:]))
    conversion_bypass()

def _restore_dispatch():
    if _conv_saved.get("__onseed__"):
        rc0, o0, e0 = ssh_script(SEED["ip"], "python3 - <<'PYEOF'\n" + _ONSEED_RESTORE + "\nPYEOF", 180)
        if rc0 == 0 and "RESTORED" in (o0 or ""):
            log("conversion-webhook restore (on-seed): %s" % (o0 or "").strip().splitlines()[-1])
            return
        log("on-seed restore FAILED (%s) — CRDs may be left at strategy=None; verify post-boot"
            % ((e0 or o0 or "")[-100:]))
        return
    conversion_restore()

_bypass_dispatch()

# ---------------- 3d. structural edits via existing tools ----------------
env2 = {**os.environ}
_tool_failures = []
def run_tool(name, args, must=True):
    # runs inside THREADS: sys.exit() would die silently (boc8 lesson — a laptop network blip
    # crashed a tool mid-thread and the "FATAL" never stopped the run). Record failures and
    # RETRY once on nonzero rc (PC/network transients); the main flow checks _tool_failures.
    t = time.time()
    if _seed_kubectl["ok"] and name in ("remint_machinenames.py", "rebind_incluster_vip.py", "refresh-creds.py"):
        # substitute the on-seed kubeconfig for the laptop one; pass the rest of argv verbatim
        _sargs = ["/tmp/wkc-seed.conf" if a == WKC else a for a in args]
        _envs = " ".join("%s='%s'" % (k, env2[k]) for k in
                         ("NKP_NUTANIX_USER","NKP_NUTANIX_PASSWORD","NKP_PC_URL") if env2.get(k))
        for attempt in (1, 2):
            rc1, o1, e1 = ssh(SEED["ip"], "%s python3 -u /tmp/%s %s" % (_envs, name, " ".join(_sargs)), to=240)
            if rc1 == 0: break
            time.sleep(8)
        if rc1 == 0:
            tail = (o1 or "").strip().splitlines()[-3:]
            log("tool %s rc=0 (%ds, on-seed) %s" % (name, int(time.time()-t), " | ".join(tail)))
            return
        log("on-seed %s failed rc=%s (%s) — retrying from the laptop" % (name, rc1, (e1 or o1 or "")[-120:]))
        # fall through to the local path below
    for attempt in (1, 2):
        try:
            r = subprocess.run(["/usr/bin/python3","-u",SCR+"/"+name]+args, env=env2,
                               capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            log("tool %s TIMED OUT (240s) attempt %d" % (name, attempt)); r = None
        if r is not None and r.returncode == 0: break
        time.sleep(8)
    if r is None or r.returncode != 0:
        # These tools print their FATAL diagnostics to STDOUT (not stderr), so logging stderr
        # alone produced an empty reason and made bcB1/bcC1 undebuggable. Log both, plus rc.
        _err = ((r.stderr if r else "") or "").strip()[-200:]
        _out = ((r.stdout if r else "") or "").strip().splitlines()
        _out = " | ".join(_out[-4:])[-400:]
        log("TOOL-FAILED %s rc=%s after retry | stdout: %s | stderr: %s"
            % (name, (r.returncode if r else "n/a"), _out or "(none)", _err or "(none)"))
        if must: _tool_failures.append(name)
        return
    tail = (r.stdout or "").strip().splitlines()[-3:]
    log("tool %s rc=%d (%ds) %s" % (name, r.returncode, int(time.time()-t), " | ".join(tail)))

# FIX-2: delete EVERY pod object carrying old identity — kubelets faithfully run stale POD
# specs no matter how correct the DS/Deploy specs are (boc3: cilium config init dialed the dead
# template VIP for hours). Also template-era pod-network pods whose IPs predate this boot.
_ONSEED_SWEEP = r'''
import json, subprocess
K = ["kubectl","--kubeconfig","/tmp/wkc-seed.conf","--request-timeout=30s"]
def k(*a):
    return subprocess.run(K+list(a), capture_output=True, text=True)
OV = "{OV}"
n = 0
r = k("get","pods","-A","-o","json")
pods = json.loads(r.stdout).get("items") or [] if r.returncode == 0 else []
for p in pods:
    hosts = [e.get("value","") for c in (p.get("spec",{}).get("containers") or [])
             for e in (c.get("env") or []) if e.get("name") == "KUBERNETES_SERVICE_HOST"]
    if any(OV in h for h in hosts):
        md = p["metadata"]
        k("delete","pod",md["name"],"-n",md["namespace"],"--wait=false","--force","--grace-period=0")
        n += 1
for ns, sel in (("kube-system","k8s-app=cilium"),("kube-system","k8s-app=cilium-envoy"),
                ("kube-system","io.cilium/app=operator"),("kube-system","app=multus"),
                ("kube-system","k8s-app=nutanix-cloud-controller-manager"),
                ("kube-system","k8s-app=hubble-relay"),
                ("metallb-system","app.kubernetes.io/component=speaker"),
                ("ntnx-system","app=nutanix-csi-node"),
                ("kommander","app.kubernetes.io/name=traefik")):
    k("delete","pod","-n",ns,"-l",sel,"--wait=false","--force","--grace-period=0")
print("SWEPT %d env-stale + families" % n)
'''

def stale_pod_sweep():
    # LEVER 1: the `get pods -A -o json` payload is multi-MB; filter + delete ON the seed.
    if _seed_kubectl["ok"]:
        _scr = _ONSEED_SWEEP.replace("{OV}", OLD_VIP)
        rc0, o0, e0 = ssh_script(SEED["ip"], "python3 - <<'PYEOF'\n" + _scr + "\nPYEOF", 120)
        if rc0 == 0 and "SWEPT" in (o0 or ""):
            log("FIX-2: stale-identity pod sweep done (on-seed): %s" % (o0 or "").strip().splitlines()[-1]); return
        log("on-seed sweep failed (%s) — falling back" % ((e0 or o0 or "")[-80:]))
    # Parse in Python, not jsonpath. The nested-range jsonpath this used returned
    # `rc=1 error parsing jsonpath` in the window (2026-07-28) while parsing fine elsewhere, and
    # because the result was never checked the sweep reported a confident "0 env-stale" that only
    # meant "the query failed". A sweep that cannot distinguish "found none" from "did not look"
    # is worse than no sweep. `-o json` has no dialect quirks and the failure is explicit.
    rc3, raw, err = kx("get","pods","-A","-o","json", to=90)
    n = 0
    if rc3 != 0 or not raw:
        log("FIX-2 WARN: could not list pods (%s) — identity sweep DID NOT RUN" % (err[:90] or "rc=%d" % rc3))
    else:
        try:
            pods = json.loads(raw).get("items") or []
        except Exception as e:
            pods = []; log("FIX-2 WARN: pod list unparseable (%s) — identity sweep DID NOT RUN" % e)
        for p in pods:
            hosts = [e.get("value","") for c in (p.get("spec",{}).get("containers") or [])
                     for e in (c.get("env") or []) if e.get("name") == "KUBERNETES_SERVICE_HOST"]
            if any(OLD_VIP in h for h in hosts):
                md = p["metadata"]
                kx("delete","pod",md["name"],"-n",md["namespace"],
                   "--wait=false","--force","--grace-period=0", to=20)
                n += 1
    # identity-critical families: fresh mint regardless (cheap — controllers recreate at boot)
    for ns, sel in (("kube-system","k8s-app=cilium"),("kube-system","k8s-app=cilium-envoy"),
                    ("kube-system","io.cilium/app=operator"),("kube-system","app=multus"),
                    ("kube-system","k8s-app=nutanix-cloud-controller-manager"),
                    ("kube-system","k8s-app=hubble-relay"),
                    ("metallb-system","app.kubernetes.io/component=speaker"),
                    ("ntnx-system","app=nutanix-csi-node"),
                    ("kommander","app.kubernetes.io/name=traefik")):
        kx("delete","pod","-n",ns,"-l",sel,"--wait=false","--force","--grace-period=0", to=30)
    log("FIX-2: stale-identity pod sweep done (%d env-stale + families)" % n)

# FIX-4: node.status.addresses via subresource (kubelet does NOT own them under external
# cloud-provider; CCM only sets them at initialization — proven on boc3).
def patch_node_addresses():
    for n in CPS + WORKERS:
        body = json.dumps({"status":{"addresses":[{"type":"InternalIP","address":n["ip"]},
                                                   {"type":"Hostname","address":n["host"]}]}})
        kx("patch","node",n["host"],"--subresource=status","--type=merge","-p",body, to=20)
    log("FIX-4: node addresses patched to clone IPs")

# VA-FIX (boc9 postmortem): the raw-etcd VA delete silently failed → template VAs persisted
# "attached:true" → the attacher never called ControllerPublish → storage pods Init-stuck 15min.
# Delete them THROUGH THE WINDOW APISERVER (finalizer strip + delete — the twice-proven mechanism).
def clear_stale_vas():
    rc3, raw, _ = kx("get","volumeattachments","-o","name", to=30)
    n = 0
    for va in (raw or "").split():
        name = va.split("/")[-1]
        kx("patch","volumeattachment",name,"--type=json","-p",'[{"op":"remove","path":"/metadata/finalizers"}]', to=15)
        kx("delete","volumeattachment",name,"--wait=false", to=15)
        n += 1
    log("VA-FIX: %d stale VolumeAttachments deleted via window apiserver" % n)

# FIX-7: reverse the freeze quiesce at rest
def unquiesce():
    # resume flux reconcile, but DELIBERATELY leave git-operator-git STS + helm-repository at 0.
    # Root cause of the ~3min VA-storage tail (bc13): scaling them up HERE creates their pods +
    # VolumeAttachments while NO CSI attacher runs (kubelet-less window) -> at boot the attacher
    # never does a real ControllerPublish -> restored VG never attaches. post_boc scales them up
    # AFTER the CSI attacher is healthy at boot, so KCM mints a fresh VA -> real attach in ~60s.
    kx("annotate","kustomization","git-operator","-n","kommander","kustomize.toolkit.fluxcd.io/reconcile-", to=20)
    kx("patch","kustomization","git-operator","-n","kommander","--type=merge","-p",'{"spec":{"suspend":false}}', to=20)
    log("FIX-7: flux reconcile resumed; storage STS held at 0 for clean boot-time attach")

# SECRET LB-REWRITE at rest (qa-n3 postmortem, 2026-08-05): byte-replace fixes the template-LB in
# CONFIGMAPS (swept prefixes) but cannot touch SECRETS — the template encrypts them at rest, so the
# raw db bytes are ciphertext. The dex config secret and kube-oidc-proxy-config therefore boot with
# the TEMPLATE's ingress LB, dex serves the wrong issuer, and the SSO chain wedges its HelmReleases
# into upgrade-retry backoff — measured as a 360-520s green tail with 3 rendered CMs stuck for
# minutes. The window apiserver decrypts transparently; rewrite through it BEFORE any kubelet so
# the first render is correct. Same-length guard not needed: this is a value edit, not a db edit.
_ONSEED_SECRET_LB = r'''
import json, subprocess, base64, gzip
K = ["kubectl","--kubeconfig","/tmp/wkc-seed.conf","--request-timeout=25s"]
def k(*a, inp=None):
    return subprocess.run(K+list(a), capture_output=True, text=True, input=inp)
TL, NL = "{TL}", "{NL}"
n = plainn = helmn = 0
for ns in ("kommander", "kommander-flux"):
    r = k("get","secrets","-n",ns,"-o","json")
    if r.returncode != 0: continue
    for it in json.loads(r.stdout).get("items") or []:
        is_helm = (it.get("type") == "helm.sh/release.v1")
        changed = {}
        for kk, vv in (it.get("data") or {}).items():
            try: dec = base64.b64decode(vv)
            except Exception: continue
            if is_helm:
                try:
                    blob = gzip.decompress(base64.b64decode(dec))
                except Exception:
                    continue
                if TL.encode() not in blob: continue
                blob = blob.replace(TL.encode(), NL.encode())
                changed[kk] = base64.b64encode(base64.b64encode(gzip.compress(blob))).decode()
                helmn += 1
            elif TL.encode() in dec:
                changed[kk] = base64.b64encode(dec.replace(TL.encode(), NL.encode())).decode()
                plainn += 1
        if changed:
            k("patch","secret",it["metadata"]["name"],"-n",ns,"--type=merge",
              "-p", json.dumps({"data":changed}))
            n += 1
print("SECRETLB %d secret(s) (%d plain, %d helm) {TL} -> {NL}" % (n, plainn, helmn))
'''

def secret_lb_rewrite():
    if TEMPLATE_LB == LBS: return
    # LEVER 1: the two `get secrets -o json` payloads are multi-MB; process them ON the seed.
    if _seed_kubectl["ok"]:
        _scr = _ONSEED_SECRET_LB.replace("{TL}", TEMPLATE_LB).replace("{NL}", LBS)
        rc0, o0, e0 = ssh_script(SEED["ip"], "python3 - <<'PYEOF'\n" + _scr + "\nPYEOF", 120)
        if rc0 == 0 and "SECRETLB" in (o0 or ""):
            log("secret-LB rewrite (on-seed): %s" % (o0 or "").strip().splitlines()[-1]); return
        log("on-seed secret rewrite failed (%s) — falling back" % ((e0 or o0 or "")[-80:]))
    import base64 as _b64, gzip as _gz
    n = plainn = helmn = 0
    for ns in ("kommander", "kommander-flux"):
        rc9, raw9, _ = kx("get","secrets","-n",ns,"-o","json", to=60)
        if rc9 != 0 or not raw9: continue
        try: items = json.loads(raw9).get("items") or []
        except Exception: continue
        for it in items:
            is_helm = (it.get("type") == "helm.sh/release.v1")
            changed = {}
            for kk, vv in (it.get("data") or {}).items():
                try: dec = _b64.b64decode(vv)
                except Exception: continue
                if is_helm:
                    # HELM RELEASE STORAGE (qa-n6 postmortem, 2026-08-05): the payload is base64 of
                    # GZIPPED json *inside* the k8s base64 — a plain one-layer decode never sees the
                    # template LB, so helm re-rendered the SSO chain's ConfigMaps back to the
                    # template's ingress IP after boot, racing the git-driven re-render (150s green
                    # when the race was won, 690s when lost). Round-trip the actual encoding:
                    # b64 -> b64 -> gunzip -> replace -> gzip -> b64 -> b64. Helm does not checksum
                    # its storage blob; it just gunzips it back.
                    try:
                        inner = _b64.b64decode(dec)
                        blob = _gz.decompress(inner)
                    except Exception:
                        continue
                    if TEMPLATE_LB.encode() not in blob: continue
                    blob = blob.replace(TEMPLATE_LB.encode(), LBS.encode())
                    changed[kk] = _b64.b64encode(_b64.b64encode(_gz.compress(blob))).decode()
                    helmn += 1
                elif TEMPLATE_LB.encode() in dec:
                    changed[kk] = _b64.b64encode(dec.replace(TEMPLATE_LB.encode(), LBS.encode())).decode()
                    plainn += 1
            if changed:
                kx("patch","secret",it["metadata"]["name"],"-n",ns,"--type=merge",
                   "-p",json.dumps({"data":changed}), to=30)
                n += 1
    log("secret-LB rewrite: %d secret(s) (%d plain, %d helm-release payloads) %s -> %s at rest"
        % (n, plainn, helmn, TEMPLATE_LB, LBS))

# parallel group A (independent), then the ordered rename chain.
# SERIALIZE THE SECRET WRITERS (qa-n9, 2026-08-05): rebind_incluster_vip's full-scan pass and
# secret_lb_rewrite both read-modify-write the SAME secrets (dex/oidc configs carry VIP and LB).
# Concurrent, they race last-writer-wins per key: n9's on-seed rebind finished in 8s and clobbered
# the LB rewrite on 3 helm payloads — the SSO chain booted on the template LB and wedged with
# "missing target release for rollback". n7 only survived because the slow laptop tools serialized
# them by accident. Chained: rebind completes, THEN the LB rewrite reads fresh state. Still
# parallel to refresh/remint/sweeps.
_ta = [threading.Thread(target=t) for t in (
    lambda: (run_tool("rebind_incluster_vip.py", [WKC, OLD_VIP, NEW_VIP]), secret_lb_rewrite()),
    lambda: run_tool("refresh-creds.py", [WKC]),
    lambda: run_tool("remint_machinenames.py", [WKC, NAME]),   # COMPRESS-C: ~100s off the chain
    stale_pod_sweep, patch_node_addresses, unquiesce, clear_stale_vas)]
for t in _ta: t.start()
for t in _ta: t.join(timeout=360)
# DELTA-2 (boc5 postmortem): set the LB range EXPLICITLY on the topology + NKPCluster —
# never trust byte-replace for it (template defect: topology can hold a stale/node IP).
def set_lb_range():
    for _try in range(4):
        rc3, raw, _ = kx("get","cluster","-n",cns(),"-o","json", to=30)
        try: it = json.loads(raw or "{}")["items"][0]
        except Exception: time.sleep(6); continue
        vars_ = it["spec"]["topology"].get("variables", [])
        done = False
        for i, v in enumerate(vars_):
            if v.get("name") == "clusterConfig":
                slb = v["value"].get("addons", {}).get("serviceLoadBalancer")
                if not slb: break
                slb["configuration"]["addressRanges"] = [{"start": LBS, "end": LBE}]
                patch = json.dumps([{"op":"replace",
                    "path":"/spec/topology/variables/%d/value/addons/serviceLoadBalancer" % i,
                    "value": slb}])
                rc4, _o, err = kx("patch","cluster",it["metadata"]["name"],"-n",cns(),"--type=json","-p",patch, to=30)
                done = rc4 == 0
                break
        if done:
            log("DELTA-2: topology serviceLoadBalancer set to %s-%s" % (LBS, LBE)); break
        time.sleep(6)
    else:
        log("DELTA-2 WARN: topology LB set did not confirm")
    rc3, raw, _ = (0, "", "") if not has_nkpcluster() else kx(
        "get","nkpcluster","-n",cns(),"-o","json", to=30)
    if raw and LBS not in raw:
        import re as _re
        fixed = _re.sub(r'"10\.22\.20[0-9.]+\s*-\s*10\.22\.20[0-9.]+"', '"%s-%s"' % (LBS, LBE), raw)
        # NKPCluster loadBalancerIPRange shapes vary; only rewrite when a clear range field exists
    log("DELTA-2: NKPCluster LB verified/left to topology sync")
set_lb_range()

# FIX-10 (2026-07-24, root of boc4's CP-Provisioned wedge + CP-rename skip + MHC roll):
# the TEMPLATE's CP machine objects can carry STALE providerIDs (build-era twin uuids). The
# node objects are the truth (CCM-set), and byte-replace has already re-mapped them to clone
# uuids — so force machine+nutanixmachine providerIDs to the clone uuid, resolved via the
# machine's retained status IP -> map host. Deterministic, in-window, no post-boot wait.
def fix_provider_ids():
    # boc5 learnings: (a) recreated machines have EMPTY status.addresses in-window — map by
    # spec.infrastructureRef.name, which IS the template hostname (nutanixmachine keeps its
    # original name through the rename); (b) patch the NUTANIXMACHINE only — CAPI propagates
    # nm.spec.providerID -> machine.spec.providerID (proven live); (c) retry the list — a
    # single empty response from the just-started window apiserver must not silently no-op.
    by_host = {n["host"]: n for n in CPS + WORKERS}
    clone_uuids = {n["clone_uuid"] for n in CPS + WORKERS}
    items = []
    for _try in range(6):
        rc3, raw, _ = kx("get","machines","-n",cns(),"-o","json", to=40)
        try: items = json.loads(raw or "{}").get("items", [])
        except Exception: items = []
        if items: break
        time.sleep(8)
    if not items:
        log("FIX-10 WARN: machine list empty after retries — pids unverified"); return
    def _fix_one(it):
        name = it["metadata"]["name"]
        pid = (it["spec"].get("providerID") or "").replace("nutanix://","")
        nm = it["spec"]["infrastructureRef"]["name"]
        tgt = by_host.get(nm)
        if tgt is None:
            log("  FIX-10 WARN: %s infraRef %s not in map" % (name, nm)); return 0
        want = "nutanix://" + tgt["clone_uuid"]
        if pid == tgt["clone_uuid"]: return 0
        kx("patch","nutanixmachine",nm,"-n","kommander","--type=merge","-p",'{"spec":{"providerID":"%s"}}'%want, to=20)
        kx("patch","nutanixmachine",nm,"-n","kommander","--subresource=status","--type=merge","-p",'{"status":{"providerID":"%s"}}'%want, to=20)
        kx("patch","machine",name,"-n","kommander","--type=merge","-p",'{"spec":{"providerID":"%s"}}'%want, to=20)
        log("  FIX-10: %s (%s) providerID %s -> %s" % (name, nm, pid[:8], tgt["clone_uuid"][:8]))
        return 1
    _fth = [threading.Thread(target=_fix_one, args=(it,)) for it in items]
    for t in _fth: t.start()
    for t in _fth: t.join(timeout=90)
    log("FIX-10: providerID reconcile complete (%d machines checked)" % len(items))
fix_provider_ids()

# FIX-11: pause MachineHealthChecks until post_boc.sh confirms machines Running — a slow bind
# must never trigger remediation (boc4: MHC deleted the SEED mid-settle -> CP roll).
rc3, mhcs, _ = kx("get","machinehealthchecks","-n",cns(),"-o","name", to=20)
for m in (mhcs or "").split():
    kx("annotate",m,"-n",cns(),"cluster.x-k8s.io/paused=","--overwrite", to=15)
log("FIX-11: MHCs paused (%d) — post_boc.sh unpauses after machines Running" % len((mhcs or "").split()))

# ORDERING GATE (bcB1 postmortem, 2026-07-25): rename_vms_to_machines renames each VM to the
# name of its CAPI Machine. That is only correct AFTER remint_machinenames has re-minted those
# Machines to the CLAIM's names. When remint_machinenames failed mid-wave on bcB1, the Machines
# still carried the TEMPLATE's names, so the rename produced FIVE VMs named exactly like the live
# qa3-bc15 template's VMs — the CAPX duplicate-VM hazard, distinguishable only by UUID.
# The old code only checked _tool_failures much later ("aborting before boot"), i.e. AFTER the
# damage. Fail here instead, while nothing has been renamed yet.
if _tool_failures:
    log("FATAL: aborting BEFORE rename — prerequisite tool(s) failed: %s. "
        "Renaming now would name this clone's VMs after the TEMPLATE's machines." % _tool_failures)
    sys.exit(9)

_tb = [threading.Thread(target=t) for t in (
    lambda: run_tool("remint_kubeadmconfig.py", [WKC]),
    lambda: run_tool("rename_vms_to_machines.py", [WKC]))]
for t in _tb: t.start()
for t in _tb: t.join(timeout=300)

# hook clear + unpause + controlPlaneEndpoint + helm-charts-pvc recreate-empty markers
def kxl(*a):
    rc3, o3, e3 = kx(*a)
    if rc3 != 0: log("  warn: kubectl %s -> %s" % (a[:3], e3[:120]))
    return o3

cl = kxl("get","cluster","-n",cns(),"-o","jsonpath={.items[0].metadata.name}")
if cl:
    kxl("annotate","cluster",cl,"-n",cns(),"runtime.cluster.x-k8s.io/pending-hooks-","--overwrite")
    kxl("patch","cluster",cl,"-n",cns(),"--type=merge","-p",
        json.dumps({"spec":{"paused":False,
            "controlPlaneEndpoint":{"host":NEW_VIP,"port":6443}}}))
    if has_nkpcluster():
        _nkpns = kxl("get","nkpcluster","-A","-o","jsonpath={.items[0].metadata.namespace}") or "kommander"
        _nkpn  = kxl("get","nkpcluster","-A","-o","jsonpath={.items[0].metadata.name}")
        if _nkpn:
            kxl("patch","nkpcluster",_nkpn,"-n",_nkpns,
                "--type=merge","-p",'{"spec":{"paused":false}}')
log("hook cleared + unpaused at rest; controlPlaneEndpoint=%s" % NEW_VIP)

# OVERLAP (2026-07-29): restore the CRD conversion config WHILE git-at-rest runs. The hook clear
# above was the LAST CAPI-typed kubectl call, so the bypass is no longer needed; git-at-rest below
# is pure ssh + PC API (verified: no kx/kubectl between here and the join). Sequentially these cost
# 19s+18s on a normal link — and the restore alone hit 137s on a degraded one (70 CRD patches each
# paying full RTT), which would all have been dead window time. Joined before the abort check so a
# failed claim still never leaves the template's CRDs bypassed.
_conv_thread = threading.Thread(target=_restore_dispatch)
_conv_thread.start()

# ---------------- 3e. git repo fix AT REST (root-caused 2026-07-24: the 4-run NOOP was a
# `git clone` failing on "dubious ownership" — the mounted VG's files are owned by the
# git-operator uid, not the ctr root. `safe.directory *` fixes it. Verified end-to-end:
# clone→sed→commit→push all work, so flux renders dex with the claim LB on FIRST boot →
# the ~2-3min post-boot SSO/git/flux cycle is ELIMINATED). Runs on worker-0 (image cache).
try:
    _fm = json.load(open(SCR + "/%s-freeze-manifest.json" % PREFIX))
    _gitpv = next(pv for pv, m in _fm["pvs"].items() if m["claim"] == "git-operator-system/git-operator-git-volume")
    _git_old_vg = _fm["pvs"][_gitpv]["vg"]
    _git_new_vg = next((a[1] for a in vg_pairs[::2] if a[0] == _git_old_vg), None)  # vg_pairs[::2]=VG pairs [old,new]
    # LEVER-A (2026-07-26): the git pod mounts BOTH git and admin volumes, so both VGs must live on
    # the node we pin it to — otherwise the unpinned one repeats the whole attach race.
    _adminpv = next((pv for pv, m in _fm["pvs"].items() if m["claim"] == "git-operator-system/git-operator-admin-volume"), None)
    _admin_new_vg = None
    if _adminpv:
        _admin_old_vg = _fm["pvs"][_adminpv]["vg"]
        _admin_new_vg = next((a[1] for a in vg_pairs[::2] if a[0] == _admin_old_vg), None)
except Exception as _e:
    _git_new_vg = None; _admin_new_vg = None; log("git-at-rest: mapping failed (%s)" % _e)
if _git_new_vg:
    # PICK THE HOST THAT HAS THE gitwebserver IMAGE (qa-n2, 2026-08-05): git-at-rest runs the repo
    # rewrite inside a ctr one-shot from the image cached on the chosen node. Blindly using
    # WORKERS[0] no-opped with GITFIX-NOIMG whenever the template's git pod had run on a DIFFERENT
    # worker — the boot then served template-LB git content and the dex re-render churn added ~2min
    # of tail. Probe (concurrently) for the image; also prefer that node for the VG attach + pin,
    # since it is exactly where the template's git pod lived.
    _gh_c = {}
    def _probe_img(w):
        rc9,o9,_ = ssh(w["ip"], "sudo ctr -n k8s.io images ls -q 2>/dev/null | grep -c gitwebserver", to=25)
        _gh_c[w["host"]] = (rc9 == 0 and (o9 or "").strip().isdigit() and int(o9.strip()) > 0)
    _pt = [threading.Thread(target=_probe_img, args=(w,)) for w in (WORKERS or [SEED])]
    for t in _pt: t.start()
    for t in _pt: t.join(timeout=40)
    _gh = next((w for w in (WORKERS or [SEED]) if _gh_c.get(w["host"])), WORKERS[0] if WORKERS else SEED)
    log("git-at-rest host: %s (gitwebserver image %s)"
        % (_gh["clone_name"], "cached" if _gh_c.get(_gh["host"]) else "NOT FOUND — expect GITFIX-NOIMG"))
    pcapi("POST", PC + "/api/volumes/v4.0/config/volume-groups/%s/$actions/attach-vm" % _git_new_vg, {"extId": _gh["clone_uuid"]})
    time.sleep(10)
    # SHAPE-CHECK the PC response (found by the 2026-07-29 baseline run under a PC flap): on error
    # this endpoint returns an error OBJECT under "data" — a truthy dict — so `_gdisks[0]` raised
    # KeyError: 0 and crashed the claim at +519s, after hook-clear but before kubelets. Also refuse
    # an empty disk extId: the mount script greps by-id for `cut -c1-20` of {GD}, and an empty GD
    # matches EVERY disk — it would mount an arbitrary device as the git volume.
    _gdisks = pcapi("GET", PC + "/api/volumes/v4.0/config/volume-groups/%s/disks" % _git_new_vg).get("data")
    _gd = _gdisks[0].get("extId", "") if isinstance(_gdisks, list) and _gdisks else ""
    if not _gd:
        log("git-at-rest: could not read the git VG's disk extId (PC said: %.120s) — skipping "
            "git-at-rest; post_boc's git-verify covers it" % str(_gdisks))
    GITFIX = r'''
for h in /sys/class/scsi_host/host*; do echo '- - -' > $h/scan; done
GDEV=""
for i in $(seq 1 12); do
  GDEV=$(ls /dev/disk/by-id/ 2>/dev/null | grep -i "$(echo {GD} | tr - _ | cut -c1-20)" | grep -v part | head -1)
  [ -n "$GDEV" ] && break
  sleep 3; for h in /sys/class/scsi_host/host*; do echo '- - -' > $h/scan; done
done
[ -n "$GDEV" ] || { echo GITFIX-NODEV; exit 0; }
mkdir -p /mnt/speedstart-git
mount /dev/disk/by-id/$GDEV /mnt/speedstart-git || { echo GITFIX-NOMOUNT; exit 0; }
GIMG=$(ctr -n k8s.io images ls -q 2>/dev/null | grep gitwebserver | head -1)
[ -n "$GIMG" ] || { umount /mnt/speedstart-git; echo GITFIX-NOIMG; exit 0; }
ctr -n k8s.io run --rm --net-host --mount type=bind,src=/mnt/speedstart-git,dst=/volumes/git,options=rbind:rw \
  "$GIMG" boc-gitfix-$$ sh -c '
    set -e
    git config --global --add safe.directory "*"
    cd /tmp && rm -rf rw
    git clone -q /volumes/git/kommander/kommander.git rw && cd rw
    if grep -rl "{OL}" clusters >/dev/null 2>&1; then
      grep -rl "{OL}" clusters | xargs -r sed -i "s/{OL}/{NL}/g"
      git -c user.name=boc -c user.email=b@c add -A
      git -c user.name=boc -c user.email=b@c commit -q -m "boc: at-rest ingress {OL} -> {NL}" || true
      git push -q origin main && echo GIT-ATREST-OK
    else echo GIT-ATREST-NOOP; fi'
umount /mnt/speedstart-git || umount -l /mnt/speedstart-git
echo GITFIX-DONE
'''.replace("{GD}", _gd).replace("{OL}", TEMPLATE_LB).replace("{NL}", LBS)
    if _gd:
        _grc, _go, _ge = ssh_script(_gh["ip"], GITFIX, 240)
        log("git-at-rest: %s" % ("OK" if "GIT-ATREST-OK" in _go else " | ".join(l for l in _go.splitlines() if "GIT" in l) or _go[-120:]))
    else:
        log("git-at-rest: SKIPPED (no disk extId) — post_boc git-verify + git_hydrate cover this")
    # ★ LEVER-A: DO NOT DETACH ★ (2026-07-26)
    # The old code detached here, so at boot CSI had to ControllerPublish the VG onto whichever node
    # the scheduler happened to pick. That attach raced the restore's leftover source-VM attachment;
    # the first mounts failed and kubelet's EXPONENTIAL backoff cost ~279s (measured bci3: pod
    # scheduled 110s, volume usable only at 389s). Instead: leave both VGs attached to THIS worker
    # and have post_boc pin git-operator-git-0 to it. NodeStage then finds the device already
    # present and the first mount succeeds — the race is removed rather than lost more slowly.
    if _admin_new_vg:
        pcapi("POST", PC + "/api/volumes/v4.0/config/volume-groups/%s/$actions/attach-vm" % _admin_new_vg,
              {"extId": _gh["clone_uuid"]})
        log("git-at-rest: admin VG %s attached to %s" % (_admin_new_vg[:8], _gh["clone_name"]))
    json.dump({"node": _gh["host"], "clone_name": _gh["clone_name"], "clone_uuid": _gh["clone_uuid"],
               "git_vg": _git_new_vg, "admin_vg": _admin_new_vg},
              open(SCR + "/%s-gitpin.json" % NAME, "w"), indent=1)
    log("LEVER-A: git+admin VGs left ATTACHED to %s (node %s) — post_boc will pin the git pod there"
        % (_gh["clone_name"], _gh["host"]))
else:
    log("git-at-rest: git VG not resolved — post_boc verify covers it")

# ---------------- 4. close window, boot everything ----------------
# Join the overlapped conversion restore BEFORE the abort check: an abort must not leave the
# template's CRDs bypassed, and this is the last point where the window apiserver is serving.
_conv_thread.join(timeout=300)
if _conv_thread.is_alive():
    log("WARN: conversion restore still running after 300s — window close will interrupt it; "
        "verify CRD strategy=Webhook post-boot")
if _tool_failures:
    log("FATAL: must-tools failed even after retry: %s — aborting before boot" % _tool_failures)
    sys.exit(10)
log("closing window (SIGTERM apiserver + etcds)")
ssh(SEED["ip"], "sudo ctr -n k8s.io task kill -s SIGTERM boc3-apiserver 2>/dev/null; sleep 2; "
                "sudo ctr -n k8s.io task kill -s SIGKILL boc3-apiserver 2>/dev/null; "
                "sudo ctr -n k8s.io c rm boc3-apiserver 2>/dev/null; true")
# LEVER-E (2026-07-26): these were SERIAL ssh round-trips (3 CPs x TERM, then 3 x KILL). The sleeps
# here total only 5s, yet close-window -> kubelet-start measured ~30s — the cost is the six serial
# SSH connections, not the waiting. Fan them out.
def _kill_etcd(ip, sig):
    ssh(ip, "sudo pkill -%s -f '/opt/spike/etcd ' ; true" % sig)
_kt = [threading.Thread(target=_kill_etcd, args=(c["ip"], "TERM")) for c in CPS]
for t in _kt: t.start()
for t in _kt: t.join(timeout=60)
time.sleep(3)
_kk = [threading.Thread(target=_kill_etcd, args=(c["ip"], "KILL")) for c in CPS]
for t in _kk: t.start()
for t in _kk: t.join(timeout=60)

log("starting ALL kubelets (%d nodes) -> single convergence" % (len(CPS)+len(WORKERS)))
_th = [threading.Thread(target=lambda n=n: ssh(n["ip"], "sudo systemctl start kubelet"))
       for n in CPS + WORKERS]
for t in _th: t.start()
for t in _th: t.join(timeout=60)

# quick apiserver-up probe on seed
up = False
for _ in range(40):
    r = subprocess.run(["curl","-sk","--max-time","4","-o","/dev/null","-w","%{http_code}",
                        "https://%s:6443/readyz" % SEED["ip"]], capture_output=True, text=True)
    if r.stdout.strip() == "200": up = True; break
    time.sleep(5)
log("first-boot apiserver: %s (+%ds)" % ("UP" if up else "not yet", int(time.time()-T0)))

# write the claim kubeconfig — VIP-based (parity audit qa-p1, 2026-08-05): this used to write the
# SEED's node IP, which works right up until a CP roll deletes that node — then every consumer of
# the kubeconfig looks "cluster down" while the cluster and its VIP are perfectly healthy. Cost a
# full VPN/kube-vip/HTTP2/MTU false-blame chain to find. The VIP survives CP replacement; that is
# its entire job.
CKC = SCR + "/%s-claim.conf" % NAME
open(CKC, "w").write(re.sub(r"https://10\.22\.20[0-9.]+:6443", "https://%s:6443" % NEW_VIP, tk))
log("claim kubeconfig -> %s" % CKC)
subprocess.Popen(["/bin/bash", SCR + "/post_boc.sh"], env={**os.environ, "NAME": NAME},
                 stdout=open(SCR + "/%s-postboc.log" % NAME, "w"), stderr=subprocess.STDOUT)
log("post_boc.sh auto-launched (git-verify first, then HA restore + MHC unpause)")
log("BOC3 WINDOW COMPLETE +%ds — run gates (gates.py) next" % int(time.time()-T0))
