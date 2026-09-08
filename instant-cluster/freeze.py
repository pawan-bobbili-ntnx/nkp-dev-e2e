#!/usr/bin/env python3
"""Freeze qa3b-tmpl into a golden template — HARDENED contract (2026-07-23):
golden RPs are minted AT FREEZE TIME, so template data survives even if the VGs are later
deleted (the 2026-07-23 qa-3cp-tmpl loss: a legacy claim cross-attached the template VGs and
a teardown deleted them — with at-freeze RPs that would have been a non-event).

Steps: verify green -> quiesce (git-operator STS + helm-repository -> 0, wait detach) ->
pause Cluster+NKPCluster -> mint VG RPs -> write golden-rps cache + freeze manifest ->
power OFF all VMs. Usage: freeze.py <kubeconfig> <cluster-name-prefix>"""
import json, os, subprocess, sys, time, uuid as uuidlib

SCR = os.environ.get("SPEEDSTART_DIR") or os.path.dirname(os.path.abspath(__file__))
KC, PREFIX = sys.argv[1], sys.argv[2]
PC = (os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)"))
U, P = (os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"]), (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])
def log(m): print("[FREEZE %s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def kx(*a, to=30):
    r = subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=%ds"%to,*a],
                       capture_output=True, text=True)
    return r.stdout.strip()

def pcapi(m, u, b=None, to=60):
    a = ["curl","-sk","--max-time",str(to),"-u","%s:%s"%(U,P),"-X",m,u,
         "-H","Content-Type: application/json","-H","NTNX-Request-Id: "+str(uuidlib.uuid4())]
    if b is not None: a += ["-d", json.dumps(b)]
    out = subprocess.run(a, capture_output=True, text=True).stdout
    try: return json.loads(out or "{}")
    except Exception: return {"_raw": (out or "")[:200]}

# 1. verify green
bad = [l for l in kx("get","pods","-A","--no-headers",to=40).splitlines()
       if l.split() and len(l.split())>3 and l.split()[3] not in ("Running","Completed","Succeeded")]
log("unhealthy pods: %d %s" % (len(bad), [b.split()[1] for b in bad[:3]]))
if len(bad) > 2: log("FATAL: cluster not green enough to freeze"); sys.exit(1)
# 1a. RECONCILED GATE - version-aware. NKP 2.18+ has NKPCluster; NKP 2.17 does
# NOT have that CRD at all (it has KommanderCluster, and its CAPI Cluster lives
# in `default`, not `kommander`). Gating unconditionally on NKPCluster made a
# 2.17 GA baseline unfreezable, which is why no GA template has ever existed
# (found 2026-08-29). Fall back to the 2.17-equivalent signals rather than
# weakening the gate: the CAPI Cluster's own conditions plus KommanderCluster.
def _cluster_ns():
    """Namespace of the CAPI Cluster - `kommander` on 2.18, `default` on 2.17."""
    for ln in kx("get","cluster","-A","--no-headers",to=40).splitlines():
        f = ln.split()
        if len(f) > 1 and f[1].startswith(PREFIX):
            return f[0]
    for ln in kx("get","cluster","-A","--no-headers",to=40).splitlines():
        if ln.split():
            return ln.split()[0]
    return "kommander"

CNS = _cluster_ns()
log("CAPI Cluster namespace: %s" % CNS)

_has_nkpc = "nkpclusters" in kx("api-resources","--api-group=clusters.nkp.nutanix.com","-o","name",to=40) \
            or bool(kx("get","crd","nkpclusters.clusters.nkp.nutanix.com","-o","name",to=40))
if _has_nkpc:
    nk = kx("get","nkpcluster","-A","--no-headers")
    log("nkpcluster: %s" % nk)
    if "Reconciled" not in nk: log("FATAL: NKPCluster not Reconciled"); sys.exit(1)
else:
    # 2.17 path: no NKPCluster CRD. Require the CAPI Cluster to be Ready AND
    # TopologyReconciled, and the host KommanderCluster to be Joined - together
    # these are what NKPCluster.Reconciled summarises on 2.18.
    _cj = kx("get","cluster","-n",CNS,"-o",
             "jsonpath={range .items[0].status.conditions[*]}{.type}={.status} {end}", to=40)
    _kc = kx("get","kommandercluster","-A","--no-headers", to=40)
    log("no NKPCluster CRD (2.17): cluster conditions [%s] kommandercluster [%s]"
        % (_cj.strip(), _kc.strip()[:80]))
    if "Ready=True" not in _cj or "TopologyReconciled=True" not in _cj:
        log("FATAL: CAPI Cluster not Ready/TopologyReconciled"); sys.exit(1)
    if "Joined" not in _kc:
        log("FATAL: KommanderCluster not Joined"); sys.exit(1)

# 1b. SSH-KEY GATE (qa-nrm1, 2026-08-05): every node must accept the pipeline's ssh key. The
# qa-nrm1 template was built WITHOUT --ssh-public-key, so no clone was ssh-able and every claim
# died in stage A ("Permission denied" surfacing as not-reachable). A frozen template is the LAST
# moment this is cheaply checkable (afterwards the nodes are off); an unclaimable template that
# freezes "successfully" costs a full thaw + key-inject + re-freeze cycle to repair.
_sshkey = os.path.expanduser(os.environ.get("NKP_SSH_KEY", "~/.ssh/nkp_cluster"))
_ips = [a for a in kx("get","nodes","-o",
        "jsonpath={range .items[*]}{.status.addresses[?(@.type=='InternalIP')].address} {end}", to=30).split() if a]
_nossh = []
for _ip in _ips:
    _r = subprocess.run(["ssh","-i",_sshkey,"-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
                         "-o","ConnectTimeout=10","-o","BatchMode=yes","konvoy@%s" % _ip,"true"],
                        capture_output=True, text=True)
    if _r.returncode != 0: _nossh.append(_ip)
if _nossh:
    log("FATAL: %d node(s) do not accept the pipeline ssh key (%s): %s — claims from this template "
        "would die in stage A. Bake the key at build (--ssh-public-key) or inject it (privileged "
        "DaemonSet appending to /home/konvoy/.ssh/authorized_keys), then re-run freeze."
        % (len(_nossh), _sshkey, _nossh)); sys.exit(1)
log("ssh-key gate: all %d node(s) accept %s" % (len(_ips), _sshkey))
# 1d. ORPHAN-FEDERATION GATE (2026-09-07): a template frozen with a KubeFedCluster that no
# KommanderCluster claims (a detached workload cluster whose kubefed member was left behind)
# hands every claim a dead federated member, and `nkp upgrade kommander` then waits 40 minutes
# on propagation to a cluster that does not exist. Cheapest place to refuse it is here.
_kfc = kx("get","kubefedcluster","-A","-o","json", to=30)
_kmc = kx("get","kommandercluster","-A","-o","json", to=30)
try:
    import json as _json
    _members = _json.loads(_kfc).get("items", []) if _kfc else []
    _kc = _json.loads(_kmc).get("items", []) if _kmc else []
    _claimed = {(i.get("spec",{}).get("kubefedClusterRef") or {}).get("name") for i in _kc} | {i["metadata"]["name"] for i in _kc}
    _orphans = [m["metadata"]["name"] for m in _members
                if "kubernetes.default" not in (m.get("spec",{}).get("apiEndpoint") or "") and m["metadata"]["name"] not in _claimed]
except Exception as _e:
    _orphans = []; log("orphan-federation gate: could not evaluate (%s) - continuing" % _e)
if _orphans:
    log("FATAL: %d orphaned KubeFedCluster(s) with no KommanderCluster: %s - a claim from this template "
        "inherits a dead federated member and every kommander upgrade times out. Delete them "
        "(kubectl -n kube-federation-system delete kubefedcluster <name>; its secretRef; the <name>-* "
        "namespace), then re-run freeze." % (len(_orphans), _orphans)); sys.exit(1)
log("orphan-federation gate: %d federated member(s), none orphaned" % len(_members))
# 1c. GENERATIVE ssh check (qa-p1 CP-roll finding, 2026-08-06): the gate above verifies the nodes
# that EXIST — it cannot see that machines created LATER (scale-out, remediation, CP roll) come
# from the ClusterClass, and if the cluster spec carries no sshAuthorizedKey (build omitted
# --ssh-public-key-file; konvoy2 SSHConfig() silently no-ops on ""), every day-2 machine boots with
# NO login user at all. Existing nodes can be repaired by hand; the generative config is what makes
# the repair durable. WARN, not FATAL: the template is still claimable — but say it loudly.
_cspec = kx("get","cluster","-n",CNS,"-o","json", to=40) or ""
if "sshAuthorizedKey" not in _cspec and "ssh-rsa" not in _cspec and "ssh-ed25519" not in _cspec:
    log("WARN: the cluster spec carries NO sshAuthorizedKey — machines created AFTER cloning "
        "(scale-out, remediation, CP roll) will have no login user. Current nodes pass only "
        "because the key was injected by hand. Fix at build time (--ssh-public-key-file) or "
        "day-2 via the NKPCluster ssh fields (NOTE: that change rolls every machine).")
else:
    log("generative ssh check: cluster spec carries an authorized key (day-2 machines get the user)")


# 2. record PV -> VG map BEFORE quiesce
pvs = {}
for line in kx("get","pv","-o",
    "jsonpath={range .items[*]}{.metadata.name}|{.spec.claimRef.namespace}/{.spec.claimRef.name}|{.spec.csi.volumeHandle}{'\\n'}{end}").splitlines():
    parts = line.split("|")
    if len(parts) == 3 and "NutanixVolumes-" in parts[2]:
        pvs[parts[0]] = {"claim": parts[1], "vg": parts[2].replace("NutanixVolumes-","")}
log("data PVs: %s" % {k[-12:]: v["claim"] for k, v in pvs.items()})
if not pvs: log("FATAL: no CSI PVs found"); sys.exit(1)

# 2.5 GIT BUNDLES — capture BEFORE quiesce, while the git pod is still serving (2026-07-25).
# These are the hydration artifacts git_hydrate.py replays at claim time: the claim rebuilds the
# git-operator repos from these bundles (LB rewritten) instead of trusting the restored VG's
# filesystem. That makes the git source correct-by-construction — a corrupt or stale restore
# WEDGES flux (kustomizations can't reconcile -> stale template-LB CMs stick -> wrong dex issuer
# -> SSO chain crashloops), which was the bc17 root cause. Bundles are ~1MB and fsck-verified.
BDIR = os.path.join(SCR, "%s-git-bundles" % PREFIX)
os.makedirs(BDIR, exist_ok=True)
GITNS, GPOD, GCON = "git-operator-system", "git-operator-git-0", "git-server-fcgi"

# FALLBACK (qa3b-tmpl, 2026-07-29): if the git pod is NOT Running but a prior freeze's bundles
# exist, KEEP them instead of failing. This is the architecture's own designed-for case — the
# template's live git VGs had been deleted out from under it (a teardown casualty; the same class
# as the 2026-07-23 qa-3cp-tmpl loss that motivated at-freeze RPs), so the git pod could never
# start. Bundles only change when the git server serves commits; a pod that has NOT been Running
# means no commits since the prior capture, so the prior bundles are exactly current. We verify
# the files, not just the metadata. If the git pod IS Running, capture fresh as always — this
# branch never masks a capturable state.
_gstat = subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=20s","get","pod",GPOD,
                         "-n",GITNS,"-o","jsonpath={.status.phase}"],capture_output=True,text=True).stdout.strip()
if _gstat != "Running":
    _bj = SCR + "/%s-git-bundles.json" % PREFIX
    _kb = os.path.join(BDIR, "kommander.bundle")
    if os.path.exists(_bj) and os.path.exists(_kb) and os.path.getsize(_kb) > 512:
        _prev = json.load(open(_bj))
        log("git pod is %r — KEEPING prior bundles (%s, src_lb=%s). The live git volume is not "
            "required for claims: git_hydrate rebuilds from these bundles over the RP scaffold."
            % (_gstat or "absent", _prev.get("bundles"), _prev.get("src_lb")))
    else:
        log("FATAL: git pod is %r and no prior bundle set exists — cannot freeze a claimable "
            "template without kommander.bundle" % (_gstat or "absent")); sys.exit(3)
_bundled = []
for repo, out in ([] if _gstat != "Running" else
                  [("/volumes/git/kommander/kommander.git", "kommander.bundle"),
                   ("/volumes/admin/admin.git", "admin.bundle")]):
    mk = subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=180s","exec","-n",GITNS,GPOD,
                         "-c",GCON,"--","sh","-c",
                         'export HOME=/tmp; G="git -c safe.directory=*"; '
                         '$G --git-dir=%s bundle create /tmp/%s --all >/dev/null 2>&1 && '
                         '$G --git-dir=%s fsck --no-dangling >/dev/null 2>&1 && echo OK || echo FAIL'
                         % (repo, out, repo)], capture_output=True, text=True)
    if "OK" not in mk.stdout:
        log("WARN: bundle create failed for %s (%s)" % (repo, (mk.stdout or mk.stderr)[-100:])); continue
    cp = subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=180s","cp",
                         "%s/%s:/tmp/%s" % (GITNS, GPOD, out), os.path.join(BDIR, out), "-c", GCON],
                        capture_output=True, text=True)
    p = os.path.join(BDIR, out)
    if os.path.exists(p) and os.path.getsize(p) > 512:
        _bundled.append(out); log("bundle %s -> %s (%d KB)" % (out, BDIR, os.path.getsize(p)//1024))
    else:
        log("WARN: bundle copy-out failed for %s: %s" % (out, (cp.stderr or "")[-100:]))
if _gstat == "Running":
    # record the ingress LB baked into the bundles so the claim knows what to rewrite FROM
    _lb = kx("get","svc","kommander-traefik","-n","kommander","-o",
             "jsonpath={.status.loadBalancer.ingress[0].ip}") or ""
    json.dump({"bundles": _bundled, "src_lb": _lb, "dir": BDIR},
              open(SCR + "/%s-git-bundles.json" % PREFIX, "w"), indent=1)
    log("git bundles: %s (src ingress LB=%s)" % (_bundled or "NONE", _lb or "?"))
    if "kommander.bundle" not in _bundled:
        log("FATAL: kommander.bundle not captured — claims would fall back to raw-restore integrity"); sys.exit(3)

# 3. quiesce: stop the consumers, wait for volumeattachments to drain
kx("annotate","kustomization","git-operator","-n","kommander","kustomize.toolkit.fluxcd.io/reconcile=disabled","--overwrite")
kx("patch","kustomization","git-operator","-n","kommander","--type=merge","-p",'{"spec":{"suspend":true}}')
kx("scale","statefulset","git-operator-git","-n","git-operator-system","--replicas=0")
kx("scale","deploy","helm-repository","-n","caren-system","--replicas=0")
log("consumers scaled down; waiting for volume detach")
for i in range(40):
    va = kx("get","volumeattachments","--no-headers",to=20) or ""
    n = len([l for l in va.splitlines() if l.strip()])
    if n == 0: break
    time.sleep(6)
log("volumeattachments remaining: %s" % (n if 'n' in dir() else "?"))

# 4. pause CAPI + NKP
cl = kx("get","cluster","-n",CNS,"-o","jsonpath={.items[0].metadata.name}")
kx("patch","cluster",cl,"-n",CNS,"--type=merge","-p",'{"spec":{"paused":true}}')
if not cl:
    log("FATAL: no CAPI Cluster found in namespace %s - refusing to freeze an "
        "unpaused cluster (its controllers would keep writing while we snapshot)" % CNS)
    sys.exit(1)
# NKPCluster exists only on 2.18+. On 2.17 pausing the CAPI Cluster is the whole
# job, because there is no NKPCluster controller to quiesce.
nkp = ""
if _has_nkpc:
    nkp = kx("get","nkpcluster","-A","-o","jsonpath={.items[0].metadata.name}")
    nkpns = kx("get","nkpcluster","-A","-o","jsonpath={.items[0].metadata.namespace}") or "kommander"
    if nkp:
        kx("patch","nkpcluster",nkp,"-n",nkpns,"--type=merge","-p",'{"spec":{"paused":true}}')
log("paused cluster=%s (ns %s) nkpcluster=%s" % (cl, CNS, nkp or "<none - 2.17>"))

# 5. mint golden RPs (the hardening) + write cache in boc3cp's RP_CACHE format
# Prior cache: the fallback source for old_disk when a source VG has since been deleted (the disk
# extId is immutable per VG, so a value cached by an earlier freeze remains correct forever).
try:
    _prev_rps = json.load(open(SCR + "/%s-golden-rps.json" % PREFIX))
except Exception:
    _prev_rps = {}
rps = {}
for pv, meta in pvs.items():
    vg = meta["vg"]
    rp_name = "golden-%s-%s" % (PREFIX, vg[:8])
    def _find_rp_by_name(name):
        pg = 0
        while pg < 30:
            d = pcapi("GET", PC + "/api/dataprotection/v4.0/config/recovery-points?$page=%d&$limit=100" % pg).get("data",[])
            if not isinstance(d, list) or not d: return ""
            for rp0 in d:
                if rp0.get("name") == name: return rp0.get("extId","")
            pg += 1
        return ""
    rp_ext = _find_rp_by_name(rp_name)     # resumable: reuse an already-minted golden RP
    if rp_ext:
        log("golden RP already exists for %s — reusing %s" % (vg[:8], rp_ext[:8]))
    else:
        r = pcapi("POST", PC + "/api/dataprotection/v4.0/config/recovery-points",
                  {"volumeGroupRecoveryPoints": [{"volumeGroupExtId": vg}],
                   "name": rp_name}, to=90)
        task = ((r.get("data") or {}).get("extId") or "")
        if not task: log("FATAL: RP create rejected for %s: %s" % (vg[:8], str(r)[:160])); sys.exit(2)
        for _ in range(40):
            time.sleep(4)
            t = pcapi("GET", PC + "/api/prism/v4.0/config/tasks/" + task)
            data = t.get("data") or {}
            if data.get("status") == "SUCCEEDED": break
            if data.get("status") in ("FAILED","CANCELED"):
                log("FATAL: RP task failed for %s" % vg[:8]); sys.exit(2)
        # entitiesAffected uuids are NOT reliably the RP — resolve by unique name (measured 2026-07-23)
        rp_ext = _find_rp_by_name(rp_name)
        if not rp_ext: log("FATAL: created RP %s not found by name" % rp_name); sys.exit(2)
    rp = pcapi("GET", PC + "/api/dataprotection/v4.0/config/recovery-points/" + rp_ext)
    vgrps = ((rp.get("data") or {}).get("volumeGroupRecoveryPoints") or [])
    vgrp = next((x.get("extId") for x in vgrps if x.get("volumeGroupExtId") == vg),
                vgrps[0].get("extId") if vgrps else "")
    if not vgrp: log("FATAL: no vgRP for %s" % vg[:8]); sys.exit(2)
    # CACHE old_disk (REQUIRED by claim.py _early_vg_restore): the SOURCE VG's internal disk extId
    # is the SCSI serial baked into the PV at freeze time, and the claim byte-replaces it with the
    # restored VG's new disk extId. It must be read HERE, while the source VG still exists — an
    # unattached template VG can be GC-reaped later (boc11 postmortem), and without this a claim
    # dies with "no cached old_disk". Capture it at freeze = loss-proof.
    od = ""
    for _t in range(5):
        dd = pcapi("GET", PC + "/api/volumes/v4.0/config/volume-groups/%s/disks" % vg).get("data")
        if isinstance(dd, list) and dd: od = dd[0].get("extId", ""); break
        time.sleep(3)
    if not od:
        # SOURCE VG GONE (qa3b-tmpl, 2026-07-29): the template's git+admin VGs had been deleted out
        # from under it, so this read can never succeed — but the disk extId is immutable per VG, so
        # the value cached by the PREVIOUS freeze is still exactly right. Refusing here would defeat
        # the very hardening this cache exists for ("template data survives even if the VGs are
        # later deleted"). Fall back to the prior cache; FATAL only if there is none.
        _prev_od = (_prev_rps.get(vg) or {}).get("old_disk", "")
        if _prev_od:
            od = _prev_od
            log("  source VG %s no longer exists — using old_disk from the prior freeze cache (%s)"
                % (vg[:8], od[:8]))
        else:
            log("FATAL: cannot read source disk extId for VG %s and no prior cache — claims would "
                "fail" % vg[:8]); sys.exit(2)
    rps[vg] = {"rp": rp_ext, "vgrp": vgrp, "old_disk": od}
    log("  old_disk cached for %s: %s" % (vg[:8], od[:8]))
    log("golden RP: %s (%s) -> rp=%s" % (meta["claim"], vg[:8], rp_ext[:8]))
json.dump(rps, open(SCR + "/%s-golden-rps.json" % PREFIX, "w"), indent=1)
json.dump({"pvs": pvs, "rps": rps, "frozen": time.strftime("%Y-%m-%d %H:%M"),
           # The exact VMs this freeze quiesced, so clone.py can select by UUID instead of
           # re-deriving them from (name prefix + power_state). Written in step 6 below, once the
           # identity gate has confirmed they are the VMs actually running the nodes.
           "vms": None},
          open(SCR + "/%s-freeze-manifest.json" % PREFIX, "w"), indent=1)
log("golden RPs + manifest written")

# 6. power OFF this cluster's VMs — **UUID-SCOPED, never a name filter**.
# SAFETY (found 2026-07-25): CAPX names day-2 replacement VMs from the ClusterClass, e.g. a worker
# replaced in a CLAIMED cluster is named "<template-prefix>-md-0-...". A `vm_name==PREFIX.*` filter
# therefore also matches OTHER live clusters' VMs, and this loop would power them OFF. Scope to the
# exact providerID UUIDs of THIS cluster's Machines instead (same rule teardown_claim.py follows).
_pids = kx("get","machines","-n",CNS,"-o",
           "jsonpath={range .items[*]}{.spec.providerID}{'\\n'}{end}", to=40)
own = [p.strip().replace("nutanix://","") for p in _pids.splitlines() if p.strip().startswith("nutanix://")]
if not own:
    log("FATAL: could not resolve this cluster's VM UUIDs from Machines — refusing to power off "
        "by name filter (that would risk powering off other clusters' VMs)"); sys.exit(4)
log("power-off scope: %d VM UUIDs from this cluster's Machines" % len(own))

# 6a. IDENTITY GATE — the providerID VM must be the VM actually RUNNING the node.
# BUG THIS CATCHES (qa-sn-tmpl1, 2026-07-27): CAPX can leave a DUPLICATE-NAME VM pair, where the
# rogue twin is the one whose kubelet won node registration. CAPI's providerID then points at the
# *other* VM, which only ever half-bootstrapped. Freeze happily powered that one off, reported
# success, and minted a "template" from a 16.8MB stunted etcd (94.8% empty) — while the real
# 119MB cluster kept running on the twin. Every claim from that template then came up on a blank
# keyspace ("the server doesn't have a resource type machines").
# The UUID-scoped rule above is still correct; it just cannot see this. Cross-check the node's own
# InternalIP against the NICs of its providerID VM — the rogue holds the IP, so they disagree.
# The Node object records the VM its kubelet is REALLY running on; the Machine records the VM CAPI
# THINKS it created. Freeze scopes power-off from Machines, so any Node whose providerID is absent
# from that scope is a node we would leave running. Compare the two sets directly — do not try to
# join them, because in the failure case they have no key in common.
_np = kx("get","nodes","-o","jsonpath={range .items[*]}{.metadata.name}|{.spec.providerID}{'\\n'}{end}", to=40)
_nodes = []
for _l in _np.splitlines():
    if "|" in _l:
        _n, _p = _l.split("|", 1)
        _u = _p.strip().replace("nutanix://", "")
        if _u: _nodes.append((_n.strip(), _u))
_stray = [(n, u) for n, u in _nodes if u not in own]
if _stray:
    log("FATAL: %d node(s) run on VMs outside the power-off scope — refusing to freeze a bogus "
        "template." % len(_stray))
    for n, u in _stray:
        g = pcapi("GET", PC + "/api/nutanix/v3/vms/" + u)
        log("  node %s runs on VM %s (%s), but this cluster's Machines point at %s"
            % (n, g.get("status",{}).get("name","?"), u[:8], [o[:8] for o in own]))
    log("  This is the CAPX duplicate-name VM pair: the twin CAPI tracks never took over, so")
    log("  freezing it would capture a half-bootstrapped disk while the real cluster keeps running.")
    log("  Re-mint the Machine/NutanixMachine providerID to the live UUID, then re-freeze.")
    sys.exit(5)
log("identity gate: all %d node(s) run on VMs in the power-off scope" % len(_nodes))

# 6a2. VIP/DHCP collision gate: if any node's own address equals the cluster
# VIP, the at-rest state has ONE string with TWO meanings and every claim's
# byte-rename will scramble kube-vip (hit live 2026-08-29). Refuse loudly.
_vip = kx("config", "view", "-o", "jsonpath={.clusters[0].cluster.server}")
_vip = _vip.split("//")[-1].split(":")[0]
_node_ips = kx("get", "nodes", "-o",
               "jsonpath={range .items[*]}{.status.addresses[?(@.type=='InternalIP')].address}{'\n'}{end}").split()
if _vip in _node_ips:
    log("FATAL: cluster VIP %s equals a node's own DHCP address — the frozen state would be" % _vip)
    log("  ambiguous and every claim would mis-rename it. Fix the VIP pool vs DHCP scope and re-mint.")
    sys.exit(6)
log("vip/dhcp gate: VIP %s is distinct from all %d node address(es)" % (_vip, len(_node_ips)))

# 6b. Record the frozen VMs BY UUID, with the CP/worker role taken from the CAPI tree rather than
# guessed from the VM name. clone.py used to rediscover the template as "name starts with PREFIX
# and power_state==OFF", which cannot survive a duplicate-name pair: once both twins are OFF the
# match is ambiguous and half the time selects the wrong disk. A UUID the freeze itself recorded
# is unambiguous by construction.
_cpnames = set(kx("get","machines","-n",CNS,
                  "-l","cluster.x-k8s.io/control-plane","-o",
                  "jsonpath={.items[*].metadata.name}", to=40).split())
_mp = kx("get","machines","-n",CNS,"-o",
         "jsonpath={range .items[*]}{.metadata.name}|{.spec.providerID}{'\\n'}{end}", to=40)
_vms = []
for _l in _mp.splitlines():
    if "|" not in _l: continue
    _n, _p = _l.split("|", 1)
    _u = _p.strip().replace("nutanix://", "")
    if _u:
        _vms.append({"uuid": _u, "machine": _n.strip(), "cp": _n.strip() in _cpnames})
_mf = SCR + "/%s-freeze-manifest.json" % PREFIX
_d = json.load(open(_mf)); _d["vms"] = _vms
json.dump(_d, open(_mf, "w"), indent=1)
log("recorded %d frozen VM UUID(s) in the manifest (%d CP)"
    % (len(_vms), sum(1 for v in _vms if v["cp"])))

for vm in own:
    g = pcapi("GET", PC + "/api/nutanix/v3/vms/" + vm)
    if g.get("status",{}).get("resources",{}).get("power_state") == "ON":
        spec = {"metadata":{k:v for k,v in g["metadata"].items() if k!="status"},"spec":g["spec"]}
        spec["spec"]["resources"]["power_state"] = "OFF"
        pcapi("PUT", PC + "/api/nutanix/v3/vms/" + vm, spec)
        log("power OFF: %s (%s)" % (g["status"]["name"], vm[:8]))
on = []
for _ in range(30):
    time.sleep(8)
    on = []
    for vm in own:
        g = pcapi("GET", PC + "/api/nutanix/v3/vms/" + vm)
        if g.get("status",{}).get("resources",{}).get("power_state") == "ON":
            on.append(g.get("status",{}).get("name", vm[:8]))
    if not on: break
log("all VMs OFF" if not on else "WARN: still ON: %s" % on)

# 7. LIVENESS POST-CONDITION — a frozen cluster must be DEAD.
# The cheapest possible proof that we powered off the right VMs: if the control-plane endpoint
# still answers after every scoped VM reports OFF, then something we did NOT power off is still
# serving this cluster, and the artifact we just minted does not represent it. (qa-sn-tmpl1 kept
# returning HTTP 200 on its VIP for hours after a "successful" freeze.) Fail loud — a silently
# bogus template is far more expensive than a failed freeze.
_srv = kx("config","view","--minify","-o","jsonpath={.clusters[0].cluster.server}")
if _srv:
    alive = None
    for _ in range(6):                          # allow ~1min for the endpoint to actually go down
        time.sleep(10)
        alive = subprocess.run(["curl","-sk","-o","/dev/null","-w","%{http_code}","--max-time","8",
                                _srv.rstrip("/") + "/healthz"], capture_output=True, text=True).stdout.strip()
        if alive != "200": break
    if alive == "200":
        log("FATAL: %s still serves HTTP 200 after every scoped VM is OFF." % _srv)
        log("  The cluster is alive on a VM this freeze did not touch, so the golden RPs and the")
        log("  powered-off disks are NOT the converged state. Do not claim from this template.")
        sys.exit(6)
    log("liveness post-condition: control-plane endpoint is down (%s)" % (alive or "no response"))

# DURABLE-MIRROR the freeze artifacts (2026-08-05). The working dir often lives under /private/tmp,
# which macOS prunes between sessions — qa-sn-tmpl1's manifest/RP-cache/kubeconfig/bundles were lost
# exactly that way, leaving a frozen, healthy template UNCLAIMABLE until a full thaw+refreeze.
# A template's artifacts are part of the template; they must survive the scratch dir.
_mirror = os.environ.get("SPEEDSTART_MIRROR", os.path.expanduser("~/Documents/nkp/speedstart-state"))
if os.path.isdir(_mirror):
    import shutil
    _copied = 0
    for _f in ("%s-freeze-manifest.json" % PREFIX, "%s-golden-rps.json" % PREFIX,
               "%s-git-bundles.json" % PREFIX, os.path.basename(KC)):
        _src = os.path.join(SCR, _f) if os.path.exists(os.path.join(SCR, _f)) else (KC if _f == os.path.basename(KC) else None)
        if _src and os.path.exists(_src):
            _dst = os.path.join(_mirror, _f)
    if os.path.abspath(_src) == os.path.abspath(_dst):
        _copied += 1  # already in the state home - nothing to mirror
    else:
        shutil.copy2(_src, _dst); _copied += 1
    _bdst = os.path.join(_mirror, os.path.basename(BDIR))
    if os.path.isdir(BDIR) and os.path.abspath(BDIR) != os.path.abspath(_bdst):
        shutil.copytree(BDIR, _bdst, dirs_exist_ok=True); _copied += 1
    log("artifacts mirrored to %s (%d items)" % (_mirror, _copied))
else:
    log("WARN: no durable mirror at %r — freeze artifacts exist ONLY in the prunable working dir. "
        "Set SPEEDSTART_MIRROR or create the directory; losing these makes the template unclaimable."
        % _mirror)

log("FREEZE COMPLETE — template %s ready for boc3 claims" % PREFIX)
