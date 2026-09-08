#!/usr/bin/env python3
"""STAGE A (node-count agnostic): clone the frozen template's VMs -> boot -> de-identify ->
detect each node's inherited IP. Stops there; claim.py's window does all remaining surgery.

De-identify means everything that would otherwise make the clone impersonate the template:
the inherited `providerid` custom attribute (the providerID wall — CAPX would resolve the
TEMPLATE's VM), /etc/machine-id (Nutanix CSI resolves its VM by machine-id), and the iSCSI IQN.

Which VMs to clone comes from the freeze manifest's recorded UUIDs, never from a name match --
see "Template identity is UUID-based" in README.md.

Writes claim-map-<name>.json, consumed by claim.py, post_boc.sh, gates.py and teardown_claim.py.

  clone.py <template-prefix> <name> <old-vip>
"""
import json, os, subprocess, sys, threading, time, uuid as uuidlib

PC = (os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)")); V3 = PC + "/api/nutanix/v3"; V4VM = PC + "/api/vmm/v4.2/ahv/config/vms"
SSH_KEY = os.path.expanduser("~/.ssh/nkp_cluster")
# SCR resolves to THIS script's directory, with an env override. It used to be a hardcoded
# absolute path under /private/tmp — which is ephemeral (it already ate smoke-creds.env once)
# and changes with every session, so a copy of these scripts anywhere else silently pointed at
# a directory that no longer existed. Same value as before when run from the scratchpad.
SCR = os.environ.get("SPEEDSTART_DIR") or os.path.dirname(os.path.abspath(__file__))
U, P = (os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"]), (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])
PREFIX, NAME, OLD_VIP = sys.argv[1], sys.argv[2], sys.argv[3]
# 4th arg = the clone's OWN VIP. Omit it to keep the template's VIP (single-clone-at-a-time only).
NEW_VIP = sys.argv[4] if len(sys.argv) > 4 else OLD_VIP
# 5th/6th args = the clone's OWN LB range (MetalLB). Without it, every clone ANNOUNCES THE
# TEMPLATE'S LB IPs -> N concurrent clones ARP-fight over the same ingress VIP on the shared VLAN.
NEW_LB_START = sys.argv[5] if len(sys.argv) > 5 else ""
NEW_LB_END   = sys.argv[6] if len(sys.argv) > 6 else NEW_LB_START
def log(m): print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def curl(a, to=60): return subprocess.run(["curl","-sk","--max-time",str(to),"-u","%s:%s"%(U,P)]+a,capture_output=True,text=True).stdout
def v3(m,path,b=None):
    a=["-X",m,V3+path]
    if b is not None: a+=["-H","Content-Type: application/json","-d",json.dumps(b)]
    # A big listing (270+ VMs on a busy PC) can come back TRUNCATED mid-body
    # over a lossy link - valid JSON prefix, no closing brace. That is a
    # transient, not an API error: retry before failing the whole claim
    # (hit live 2026-08-28 23:00 and 2026-08-29 13:57).
    d = None
    for attempt in (1, 2, 3):
        raw = curl(a)
        try:
            d = json.loads(raw); break
        except Exception:
            if attempt < 3 and raw and raw.lstrip().startswith("{"):
                log("PC response truncated (%d bytes) for %s %s - retry %d/3"
                    % (len(raw or ""), m, path, attempt))
                time.sleep(5 * attempt); continue
            log("FATAL: PC returned non-JSON for %s %s: %s"%(m,path,(raw or "")[:120])); sys.exit(20)
    # FAIL LOUD ON AUTH/API ERRORS (2026-07-14): a 401 used to fall through the bare except and
    # return {} — so vms/list looked like "template: 0 VMs", stageA cloned nothing, de-identified
    # nothing, then died on IndexError. An auth failure must NEVER masquerade as an empty result.
    if isinstance(d,dict) and d.get("state")=="ERROR":
        msgs=[x.get("message","") for x in (d.get("message_list") or [])]
        log("FATAL: PC API error %s on %s %s: %s"%(d.get("code"),m,path,"; ".join(msgs)[:160]))
        sys.exit(21)
    return d
def ssh(ip,cmd,to=120):
    # retry CONNECTION-level failures (banner timeout/refused/no-route) — a single transient
    # hiccup must not kill a 20-min claim (measured on qa3cp7n's 6Q). Command failures (rc!=0
    # over a real connection) are returned as-is.
    last=(255,"","no attempt")
    for _ in range(4):
        try:
            r=subprocess.run(["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
                "-o","ConnectTimeout=12","-o","BatchMode=yes","konvoy@"+ip,cmd],capture_output=True,text=True,timeout=to+30)
            last=(r.returncode,r.stdout.strip(),r.stderr.strip())
            if not (r.returncode==255 and any(s in (r.stderr or "") for s in
                    ("timed out","banner exchange","Connection refused","No route to host","Connection reset"))):
                return last
        except subprocess.TimeoutExpired:
            last=(124,"","ssh timeout after %ds"%(to+30))
        time.sleep(6)
    return last

# 1. discover + clone + boot
# Source of truth is the freeze manifest's VM UUIDs. freeze.py records exactly which VMs it
# quiesced, after verifying each is the VM actually running its node.
# WHY NOT BY NAME (qa-sn-tmpl1, 2026-07-27): the old rule was "name starts with PREFIX and
# power_state==OFF". A CAPX duplicate-name VM pair broke it — two VMs shared a name, and once both
# were OFF the match was ambiguous, so this cloned a half-bootstrapped disk while the real cluster
# ran on the twin. The claim then came up on a blank keyspace. A UUID cannot be ambiguous.
tmpl=[]
_mf=SCR+"/%s-freeze-manifest.json"%PREFIX
_fvms=(json.load(open(_mf)).get("vms") if os.path.exists(_mf) else None)
if _fvms:
    _byuuid={e["metadata"]["uuid"]:e for e in v3("POST","/vms/list",{"kind":"vm","length":500}).get("entities",[])}
    for _v in _fvms:
        e=_byuuid.get(_v["uuid"])
        if not e:
            log("FATAL: freeze manifest lists VM %s (%s) but it no longer exists on the PC. The "
                "template has been partially deleted; re-freeze before claiming."
                %(_v["uuid"][:8],_v.get("machine","?"))); sys.exit(22)
        if e["status"]["resources"].get("power_state")!="OFF":
            log("FATAL: template VM %s (%s) is powered ON. Cloning it would copy disks mid-write. "
                "The template is not frozen."%(_v["uuid"][:8],e["status"]["name"])); sys.exit(22)
        tmpl.append({"name":e["status"]["name"],"uuid":_v["uuid"],"cp":_v["cp"]})
    tmpl.sort(key=lambda t:(not t["cp"],t["name"]))   # CPs first, then stable by name
    log("template: %d VMs (%d CP) from freeze manifest UUIDs"%(len(tmpl),sum(t["cp"] for t in tmpl)))
else:
    log("FATAL: %s has no 'vms' list — it was written by a freeze.py predating UUID-based template "
        "selection. Re-freeze the template so the exact VM UUIDs are recorded; selecting by name is "
        "unsafe when a duplicate-name VM pair exists."%os.path.basename(_mf))
    sys.exit(22)
existing={e["status"]["name"]:e["metadata"]["uuid"] for e in v3("POST","/vms/list",{"kind":"vm","length":500}).get("entities",[])}
m=[]
for i,t in enumerate(tmpl):
    role="cp%d"%i if t["cp"] else "md%d"%i
    cname="%s-%s"%(NAME,role)
    if cname in existing:
        log("reuse existing clone %s (%s) — resumable re-run"%(cname,existing[cname][:8]))
    else:
        v3("POST","/vms/%s/clone"%t["uuid"],{"override_spec":{"name":cname}})
        log("clone %s -> %s"%(t["name"],cname))
    m.append({"tmpl":t,"clone_name":cname,"cp":t["cp"]})
# poll until every clone VM materializes (a fixed sleep raced slow clone tasks -> clone_uuid=None -> KeyError)
for attempt in range(30):
    time.sleep(10)
    byname={e["status"]["name"]:e["metadata"]["uuid"] for e in v3("POST","/vms/list",{"kind":"vm","length":500}).get("entities",[])}
    for c in m: c["clone_uuid"]=byname.get(c["clone_name"])
    missing=[c["clone_name"] for c in m if not c["clone_uuid"]]
    if not missing: break
    log("  waiting for clone VMs to materialize (missing: %s)"%missing)
else:
    log("FATAL: clones never materialized: %s — clean up manually before retrying"%missing); sys.exit(2)
# power on with TASK verification — a silently-failed PUT (e.g. RESOURCE_SHORTAGE on the
# near-full PE) previously left a clone OFF and crashed the IP wait with KeyError('ip')
# 2026-07-26: was 16 — a STALE workaround from when this PE was 95-98% full and a 32G worker would
# not place. The PE now runs ~66% used (15 hosts, ~34% headroom). The 16G cap is now actively
# harmful: a 2-worker cluster packs ~46 pods onto one worker and the node ends up **109% over-
# committed on memory limits** (17100Mi of limits vs ~15.9Gi allocatable). Under that pressure the
# kernel cannot allocate a thread stack and Go dies with `newosproc ... errno=11 (EAGAIN)` — the
# kube-rbac-proxy sidecar crashloop that corrupted 3 runs (bc25, bcj2, +1). It is NOT a pid/nproc
# limit: RLIMIT_NPROC is unlimited, containerd LimitNPROC=infinity, podPidsLimit=4096, cgroup
# pids.max=4096. It is memory. Re-cap only if the PE fills up again.
WORKER_MEM_CAP_G=int(os.environ.get("SPEEDSTART_WORKER_MEM_G","32"))
# PARALLEL power-on (2026-07-26). This was serial: each clone did PUT + poll-task-to-SUCCEEDED,
# ~11-14s apiece, so 5 clones cost ~75s wall (measured bcf2: first ON 09:04:40, last 09:05:26).
# The VMs are independent, so run them concurrently — cost becomes the slowest single power-on.
# sys.exit() inside a thread would NOT stop the run (the run_tool/boc8 lesson), so failures are
# recorded and the FATAL is raised after the join.
_pw_err={}
def _power_on(c):
    try:
        g=v3("GET","/vms/%s"%c["clone_uuid"])
        if g.get("status",{}).get("resources",{}).get("power_state")=="ON":
            log("  %s already ON"%c["clone_name"]); return
        # cap worker memory BEFORE power-on (resize needs OFF; it is). Template worker is 32G and
        # RESOURCE_SHORTAGE-refuses on this capacity-constrained PE.
        if (not c["cp"]) and g["spec"]["resources"].get("memory_size_mib",0) > WORKER_MEM_CAP_G*1024:
            g["spec"]["resources"]["memory_size_mib"]=WORKER_MEM_CAP_G*1024
            log("  %s: capping worker mem to %dG (PE capacity)"%(c["clone_name"],WORKER_MEM_CAP_G))
        g["spec"]["resources"]["power_state"]="ON"
        r=v3("PUT","/vms/%s"%c["clone_uuid"],{"spec":g["spec"],"metadata":g["metadata"],"api_version":"3.1"})
        task=(r.get("status",{}).get("execution_context") or {}).get("task_uuid")
        ok=False; err="PUT rejected: %s"%str(r)[:120]
        if task:
            err="task timeout"
            for _ in range(60):
                time.sleep(2)
                t=v3("GET","/tasks/%s"%task)
                if t.get("status")=="SUCCEEDED": ok=True; break
                if t.get("status")=="FAILED": err=str(t.get("error_detail"))[:150]; break
        if not ok: _pw_err[c["clone_name"]]=err; return
        log("  %s powered ON"%c["clone_name"])
    except Exception as e:
        _pw_err[c["clone_name"]]=str(e)[:150]
_pt=[threading.Thread(target=_power_on,args=(c,)) for c in m]
for t in _pt: t.start()
for t in _pt: t.join(timeout=180)
if _pw_err:
    log("FATAL: power-on failed: %s"%_pw_err); sys.exit(4)
# POSITIVE VERIFICATION, not just "no errors recorded": v3() calls sys.exit() on an API/auth error,
# which inside a worker thread raises SystemExit — NOT caught by `except Exception`, so that thread
# would die silently leaving _pw_err empty and a clone still OFF. Confirm the actual power state.
_still_off=[]
for c in m:
    g=v3("GET","/vms/%s"%c["clone_uuid"])
    if g.get("status",{}).get("resources",{}).get("power_state")!="ON":
        _still_off.append(c["clone_name"])
if _still_off:
    log("FATAL: these clones are still not ON after the parallel power-on: %s"%_still_off); sys.exit(4)
log("all %d clones confirmed ON (parallel power-on)"%len(m))
# LEVER-B (2026-07-26): this polled a 500-VM `vms/list` every 3s and cost ~49s to learn the IPs
# (measured bck1: powered ON 18:35:41, IPs known 18:36:39) — even though the guest boots in **11.7s**
# (systemd-analyze) and NetworkManager is up at 4.4s. So we were not waiting for boot, we were
# waiting to be TOLD about it, through the heaviest possible endpoint. Poll each clone's own
# /vms/<uuid> CONCURRENTLY at a tighter interval instead: far less PC work per round, and one slow
# VM no longer delays the others.
def _poll_ip(c):
    for _ in range(160):
        if c.get("ip"): return
        e=v3("GET","/vms/%s"%c["clone_uuid"])
        for nic in (e.get("status",{}).get("resources",{}) or {}).get("nic_list",[]) or []:
            for ep in nic.get("ip_endpoint_list",[]) or []:
                ip=ep.get("ip","")
                if ip.startswith("10.22.2") and not ip.startswith("10.22.203"):
                    c["ip"]=ip; return
        time.sleep(2)
_ipt=[threading.Thread(target=_poll_ip,args=(c,)) for c in m]
for t in _ipt: t.start()
for t in _ipt: t.join(timeout=340)
noip=[c["clone_name"] for c in m if not c.get("ip")]
if noip:
    log("FATAL: clones never got an IP: %s — check DHCP/boot before retrying"%noip); sys.exit(5)
for c in m: log("  %s uuid=%s ip=%s cp=%s"%(c["clone_name"],c["clone_uuid"],c.get("ip"),c["cp"]))

# 2. remove custom-attr on all
log("remove inherited providerid customAttr on all clones")
# PARALLEL too — same reasoning as power-on: 5 independent VMs, each a GET(etag)+POST round-trip
# (~3s serial apiece). Failure here is NOT fatal (the log records it and downstream providerID
# handling re-checks), so no error aggregation is needed — just don't serialize the round-trips.
def _stale_attrs(c):
    d=(json.loads(curl(["%s/%s"%(V4VM,c["clone_uuid"])],30) or "{}") or {}).get("data") or {}
    return [a for a in (d.get("customAttributes") or []) if "providerid" in str(a).lower()]
def _rm_attr(c):
    try:
        hdr=curl(["-D","-","%s/%s"%(V4VM,c["clone_uuid"])],30)
        etag=next((l.split(":",1)[1].strip() for l in hdr.splitlines() if l.lower().startswith("etag:")),None)
        # Remove the providerid attributes the clone ACTUALLY carries, not the one we assume it
        # inherited. The old code posted "providerid:<template-uuid>"; remove-custom-attributes
        # matches on the exact string, so any other value (a VM CAPX minted before a re-mint, a
        # differently-formed attr) is a silent no-op that then trips the verify below as a hard
        # FATAL. Reading first makes the request describe reality.
        have=_stale_attrs(c)
        if not have: return True
        o=curl(["-X","POST","%s/%s/$actions/remove-custom-attributes"%(V4VM,c["clone_uuid"]),
            "-H","Content-Type: application/json","-H","If-Match: %s"%etag,"-H","NTNX-Request-Id: %s"%uuidlib.uuid4(),
            "-d",json.dumps({"customAttributes":have})],60)
        if "TaskReference" in o:
            log("  %s removing %s -> accepted"%(c["clone_name"],have)); return True
        # Log WHY. This used to print a bare "no-task", which told us nothing and sent me chasing a
        # non-existent API-version bug. The v4.2 endpoint is correct; the rejection is transient --
        # the etag goes stale while Prism is still mutating the freshly cloned VM (power-on, NIC/IP
        # assignment), so If-Match fails. Hence: re-issue with a FRESH etag, do not just re-poll.
        log("  %s removing %s -> REJECTED: %s"%(c["clone_name"],have,o.strip()[:180] or "empty response"))
        return False
    except Exception as e:
        log("  %s removed=EXC %s"%(c["clone_name"],str(e)[:80])); return False
_at=[threading.Thread(target=_rm_attr,args=(c,)) for c in m]
for t in _at: t.start()
for t in _at: t.join(timeout=120)
# VERIFY THE OUTCOME, not the response string. `"TaskReference" in o` is a fragile success test —
# on bcg1 it reported removed=False for cp2 while the attribute had in fact been removed. What
# matters is that no inherited providerid customAttribute survives (it is the providerID wall:
# CAPX would otherwise resolve the TEMPLATE's VM). Re-issue once for any VM that still has one.
for c in m:
    try:
        # POLL, don't spot-check. remove-custom-attributes is asynchronous (it returns a
        # TaskReference), so a single read ~3s after the POST can still observe the old value and
        # declare a false FATAL — which is exactly what happened on qa-sn-c2 (2026-07-27): the
        # attribute was gone moments later, but the claim had already aborted stage A. Give the
        # task a bounded window to land, and only then treat a surviving attribute as real.
        # RE-ISSUE every round with a fresh etag — do not poll a request that was already rejected.
        # qa-sn-c4 (2026-07-28) died here after 30s: the first POST was rejected, and the old loop
        # re-issued only once before spending the rest of its budget re-reading an unchanged VM.
        for _try in range(12):
            if not _stale_attrs(c): break
            if _try: log("  %s still has a stale providerid attr — re-issuing (try %d)"
                         % (c["clone_name"], _try + 1))
            _rm_attr(c)
            time.sleep(4)
        else:
            log("FATAL: %s kept its inherited providerid customAttribute after ~50s of retries — "
                "CAPX would resolve the TEMPLATE VM (day-2 hazard)"%c["clone_name"]); sys.exit(6)
    except SystemExit: raise
    except Exception as e:
        log("  %s attr-verify skipped (%s)"%(c["clone_name"],str(e)[:80]))
log("providerid customAttributes verified absent on all %d clones"%len(m))
time.sleep(2)   # v4 remove-custom-attributes task is ~1s; downstream ssh has its own retries

# 3. detect each CP's baked OLD node IP + hostname (self-describing from its own etcd.yaml)
cps=[c for c in m if c["cp"]]
# CONCURRENT reachability wait (qa-n2 postmortem, 2026-08-05): this loop was SERIAL per VM with a
# 72s budget each — 4 slow-booting clones = up to 288s of pure queueing, and it showed: stage A
# went 100s -> 201s the day the PE booted guests slowly. The VMs boot in parallel; wait on them in
# parallel. Budget is 300s per VM (PE-load boot variance is real; the budget only bites when
# a VM is genuinely dead, and the machine-id step FATALs right after with a named node anyway).
def _wait_reach(c):
    # 150s was not enough on a loaded PC: 2026-09-01, morning, ~200 VMs on the
    # cluster, BOTH clones timed out here and stage A FATALed at +439s — while
    # the same claim four hours earlier went IP -> ssh in 14s. The budget only
    # costs wall-clock when a VM is genuinely dead, and the machine-id step
    # right after still FATALs with a named node, so prefer tolerating a slow
    # boot over failing a run (or a live demo) that would have worked.
    for i in range(150):   # ~300s
        rc,o,e=ssh(c["ip"],"echo ok")
        if rc==0:
            if i > 20:
                log("  %s answered ssh after %ds (slow boot, PE load)"%(c["clone_name"], i*2))
            return
        if i and i % 30 == 0:
            log("  %s still waiting for ssh (%ds)"%(c["clone_name"], i*2))
        time.sleep(2)
    log("  WARNING: %s (%s) not ssh-reachable after 300s — downstream machine-id step may FATAL"
        %(c["clone_name"],c["ip"]))
_rt=[threading.Thread(target=_wait_reach,args=(c,)) for c in m]
for t in _rt: t.start()
# must exceed _wait_reach's own budget, or the main thread walks past a clone
# that was still going to answer - which made raising that budget a no-op
for t in _rt: t.join(timeout=310)

# 3.4. WRINKLE-2 fix: regen /etc/machine-id = clone VM UUID on EVERY clone (CPs too).
# The cloned disk carries the TEMPLATE's machine-id (= template VM UUID) and the Nutanix CSI
# node plugin resolves its VM by machine-id -> restored VGs would attach to the TEMPLATE VM.
# Self-derived on each node from SMBIOS (the clone re-mints product_uuid = clone VM UUID).
# No kubelet restart here: reident (CPs) and stage-B phase 6 (all nodes) restart kubelet later,
# and stage-B phase 7.5 gates on CSINode correctness before any volume work.
# DHCP note (verified 2026-07-04): NM ipv4.dhcp-client-id is unset on these images and
# systemd-networkd is inactive -> client identity is MAC-based, so this rewrite cannot shift
# the node's DHCP lease. Re-verify if the base image's network stack ever changes.
log("regen /etc/machine-id on all clones (Nutanix CSI resolves its VM by machine-id)")
# validate-then-write: refuse anything that isn't 32 hex chars BEFORE overwriting (a plain
# > redirect would truncate machine-id to empty if the SMBIOS read ever failed)
MIDCMD=("sudo bash -c 'set -euo pipefail; new=$(tr -d \"-\" </sys/class/dmi/id/product_uuid | tr \"[:upper:]\" \"[:lower:]\"); "
        "echo \"$new\" | grep -qE \"^[0-9a-f]{32}$\"; printf \"%s\\n\" \"$new\" >/etc/machine-id; "
        "if [ -d /var/lib/dbus ]; then rm -f /var/lib/dbus/machine-id; ln -sf /etc/machine-id /var/lib/dbus/machine-id; fi; "
        "cat /etc/machine-id'")  # /var/lib/dbus absent on CP nodes — conditional
# COMPRESS-D (2026-07-24): the per-node machine-id + host/old-IP detection loops were
# serialized (~4-8s x 5 nodes x 2 rounds) — run them all concurrently.
_mid_fail = {}
def _mid_one(c):
    rc,o,e=ssh(c["ip"],MIDCMD)
    got=(o.splitlines()[-1].strip() if o else "")
    want=(c["clone_uuid"] or "").replace("-","").lower()
    if rc!=0 or got!=want:
        time.sleep(8)
        rc,o,e=ssh(c["ip"],MIDCMD)
        got=(o.splitlines()[-1].strip() if o else "")
    log("  %s machine-id=%s matches-clone-uuid=%s"%(c["clone_name"],got[:13],got==want))
    if got!=want: _mid_fail[c["clone_name"]]=got
    # 3.4b WRINKLE-2 *ROOT* FIX (measured 2026-07-25): machine-id was only HALF the identity.
    # The clone also inherits /etc/iscsi/initiatorname.iscsi from the template, so every clone of
    # a template presents the SAME iSCSI IQN. Nutanix Volumes keys client access on that IQN, so
    # CSI's ControllerPublish attaches the restored VG to the VM already registered under it —
    # the TEMPLATE's node. That is the real source of the "foreign VM attachment" that wedged the
    # git pod in Init:0/3 for 150-250s every claim (the sweeps only cleaned up after the fact).
    # PROVEN: bc23 and bc25 — two independent live clusters — both had
    #   InitiatorName=iqn.1994-05.com.redhat:8a586ccc7b7d
    # A freshly CAPX-provisioned day-2 node had a UNIQUE IQN, confirming it is clone-inherited.
    # This is also a CORRECTNESS/data-safety fix, not just a speed one: independent clusters must
    # not share a storage-initiator identity. Regenerate before kubelet/CSI ever starts.
    _rc2,_o2,_e2 = ssh(c["ip"],
        "sudo bash -c 'set -e; f=/etc/iscsi/initiatorname.iscsi; "
        "if [ -f $f ]; then new=$(/usr/sbin/iscsi-iname); "
        "echo \"$new\" | grep -qE \"^iqn\\.\"; printf \"InitiatorName=%s\\n\" \"$new\" >$f; "
        "systemctl restart iscsid 2>/dev/null || true; fi; cat $f 2>/dev/null'")
    log("  %s IQN=%s" % (c["clone_name"], (_o2.strip().split("=")[-1] if _o2 else "n/a")[:34]))
def _detect_one(c):
    if c["cp"]:
        rc,o,e=ssh(c["ip"],"sudo bash -c 'hostname; grep -oE \"advertise-client-urls=https://[0-9.]+\" /etc/kubernetes/manifests/etcd.yaml | grep -oE \"[0-9.]+\" | head -1'")
        lines=o.splitlines(); c["host"]=lines[0] if lines else "?"; c["old"]=lines[1] if len(lines)>1 else "?"
        log("  CP %s: host=%s old=%s new=%s"%(c["clone_name"],c["host"],c["old"],c["ip"]))
    else:
        rc,o,e=ssh(c["ip"],"hostname"); c["host"]=o.strip()
_th=[threading.Thread(target=_mid_one,args=(c,)) for c in m]
for t in _th: t.start()
for t in _th: t.join(timeout=120)
if _mid_fail:
    log("FATAL: machine-id != clone VM UUID after retry on %s — fix before stage B"%list(_mid_fail)); sys.exit(3)
_th=[threading.Thread(target=_detect_one,args=(c,)) for c in m]
for t in _th: t.start()
for t in _th: t.join(timeout=120)
_ws=[c for c in m if not c["cp"]]
worker=_ws[0] if _ws else None   # SINGLE-NODE template: no worker VM at all

# re-pair tmpl<->clone by HOSTNAME (a clone's hostname == its source template VM's name).
# Enumeration-order pairing breaks on resumable re-runs (vms/list order differs between the
# run that created the clones and the run that reuses them) -> TMAP would cross-wire
# template->clone uuids between CPs -> Machine/Node providerID mismatch after re-mint.
byhost={t["name"]:t for t in tmpl}
for c in m:
    t=byhost.get(c.get("host"))
    if t: c["tmpl"]=t
    else: log("WARNING: no template VM named '%s' — keeping order-pairing for %s"%(c.get("host"),c["clone_name"]))

# save map for stage B
json.dump({"cps":[{k:c[k] for k in ("clone_name","clone_uuid","ip","host","old","tmpl") } for c in cps],
           "worker":({"clone_name":worker["clone_name"],"clone_uuid":worker["clone_uuid"],"ip":worker["ip"],"host":worker["host"],"tmpl":worker["tmpl"]} if worker else None),
           "workers":[{"clone_name":w["clone_name"],"clone_uuid":w["clone_uuid"],"ip":w["ip"],"host":w["host"],"tmpl":w["tmpl"]} for w in _ws],
           "tmap":{c["tmpl"]["uuid"]:c["clone_uuid"] for c in m},
           "old_vip":OLD_VIP,"new_vip":NEW_VIP,"lb_start":NEW_LB_START,"lb_end":NEW_LB_END,"prefix":PREFIX,
           "name":NAME}, open(SCR+"/claim-map-%s.json"%NAME,"w"), indent=1)
log("saved map -> claim-map-%s.json" % NAME)

# STAGE A ENDS HERE.
#
# The clones are cloned, booted and given their own machine identity (machine-id + iSCSI IQN), but
# are otherwise still the template. All remaining identity work happens AT REST inside claim.py's
# one kubelet window, so the cluster converges exactly once already believing its new identity.
#
# 2026-07-27: this file used to continue into `reident-3cp.py` — a file that no longer exists
# anywhere in the tree. The branch was unreachable (claim.py always set SKIP_REIDENT=1, which
# exited above), so it was ~45 lines of dead code pointing at a missing script. Removed along with
# the online single-node path it mirrored; stopping here is now the only behaviour.
log("stage A complete — %d clones booted and re-identified; the window does the rest" % len(m))
sys.exit(0)
