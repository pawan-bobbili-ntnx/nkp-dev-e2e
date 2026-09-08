#!/usr/bin/env python3
"""PREFLIGHT for a clone-claim — fail in seconds, not 8 minutes in.

Every check here corresponds to a failure this project actually hit. A claim clones and boots 5
VMs before it touches most of these, so a bad input costs ~8 minutes and leaves VMs to clean up.

  preflight.py <template-prefix> <claim-name> <new-vip> <new-lb>

Exit 0 = safe to claim. Non-zero = do not run. Read-only: touches nothing.
"""
import json, os, re, subprocess, sys, uuid as uuidlib

SCR = os.environ.get("SPEEDSTART_DIR") or os.path.dirname(os.path.abspath(__file__))
PC = (os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)"))
if len(sys.argv) < 5:
    print(__doc__); sys.exit(64)
PREFIX, NAME, VIP, LB = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

fails, warns = [], []
def ok(m):   print("  [ ok ] %s" % m)
def bad(m):  fails.append(m); print("  [FAIL] %s" % m)
def warn(m): warns.append(m); print("  [warn] %s" % m)

def sh(*a, to=30):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=to).stdout
    except Exception: return ""

# ---- 1. claim name -------------------------------------------------------------------------
# bcB1/bcC1/bcC2: uppercase names cloned+booted 5 VMs, then died ~6.5min in inside
# remint_machinenames because the name is embedded in CAPI Machine names.
if re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", NAME) and len(NAME) <= 40:
    ok("claim name %r is a valid lowercase RFC 1123 subdomain" % NAME)
else:
    bad("claim name %r is NOT a lowercase RFC 1123 subdomain (a-z, 0-9, '-'; <=40)" % NAME)

# ---- 2. credentials + PC reachability -------------------------------------------------------
U, P = os.environ.get("NKP_NUTANIX_USER"), os.environ.get("NKP_NUTANIX_PASSWORD")
if not (U and P):
    bad("NKP_NUTANIX_USER/PASSWORD not in env (source smoke-creds.env)")
else:
    code = sh("curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "25",
              "-u", "%s:%s" % (U, P), PC + "/api/nutanix/v3/users/me", to=35).strip()
    ok("PC reachable and credentials valid") if code == "200" else \
        bad("PC auth/reachability failed (HTTP %s) — VPN down or creds wrong" % (code or "no response"))

# ---- 3. ssh key ------------------------------------------------------------------------------
key = os.path.expanduser("~/.ssh/nkp_cluster")
ok("ssh key present: %s" % key) if os.path.exists(key) else bad("missing ssh key %s" % key)

# ---- 4. template artifacts -------------------------------------------------------------------
# A missing old_disk aborts every claim; a missing <PREFIX>.conf aborts the window.
rp_path = os.path.join(SCR, "%s-golden-rps.json" % PREFIX)
for f, why in ((rp_path, "golden RPs"),
               (os.path.join(SCR, "%s-freeze-manifest.json" % PREFIX), "freeze manifest"),
               (os.path.join(SCR, "%s.conf" % PREFIX), "template kubeconfig (<PREFIX>.conf)")):
    ok("%s present" % why) if os.path.exists(f) else bad("MISSING %s: %s" % (why, f))

if os.path.exists(rp_path):
    try:
        rps = json.load(open(rp_path))
        missing = [vg[:8] for vg, e in rps.items() if not e.get("old_disk")]
        ok("all %d golden RPs carry old_disk" % len(rps)) if not missing else \
            bad("golden RPs missing old_disk for %s — every claim will abort" % missing)
    except Exception as e:
        bad("golden RPs unreadable: %s" % e)

bmeta = os.path.join(SCR, "%s-git-bundles.json" % PREFIX)
if os.path.exists(bmeta):
    try:
        b = json.load(open(bmeta))
        bdir = b.get("dir", "")
        kb = os.path.join(bdir, "kommander.bundle")
        ok("git bundle present (src LB %s)" % b.get("src_lb")) if os.path.exists(kb) else \
            bad("bundle metadata points at a missing file: %s" % kb)
    except Exception as e:
        bad("bundle metadata unreadable: %s" % e)
else:
    warn("no %s-git-bundles.json — git_hydrate will fall back to the prototype bundle" % PREFIX)

# ---- 4b. the etcd binaries the window ships to each node ---------------------------------------
# qa-sn-c3 (2026-07-28): the scratchpad lives under /private/tmp, which macOS prunes. The three
# binaries vanished between sessions and the claim died at +119s inside a prep thread with a raw
# scp traceback — after cloning and booting a VM that then had to be torn down. They must also be
# LINUX x86-64: a macOS build scp's fine and fails much later, inside the window, as "cannot
# execute binary file".
for _b in ("etcdutl", "etcdctl", "etcd"):
    _p = os.path.join(SCR, _b)
    if not os.path.exists(_p):
        bad("missing %s — the window cannot restore etcd. Restore it from the durable mirror "
            "(speedstart-state/) or re-extract the etcd release tarball." % _p)
    else:
        _kind = sh("file", "-b", _p, to=15)
        ok("%s present (Linux x86-64)" % _b) if "ELF" in _kind and "x86-64" in _kind else \
            bad("%s is not a Linux x86-64 binary (%s) — it will fail inside the window"
                % (_b, _kind.strip()[:60]))

# ---- 4c. every script the pipeline shells out to --------------------------------------------
# qa-sn-c7 (2026-07-28): `rightsize_hcp.py` had vanished from the working dir along with the etcd
# binaries (see 4b — /private/tmp gets pruned). post_boc.sh pipes its stderr through `sed` and keeps
# going, so single-node right-sizing SILENTLY no-opped; the parity gate still passed only because
# that template happened to be right-sized already. Missing helpers must fail here, loudly, not turn
# into a no-op several minutes into a claim.
# REQUIRED = what the claim itself shells out to. A missing one of these turns into a silent no-op
# or a mid-claim traceback.
for _s in ("clone.py", "post_boc.sh", "git_hydrate.py", "rightsize_hcp.py",
           "rebind_incluster_vip.py", "refresh-creds.py", "remint_kubeadmconfig.py",
           "remint_machinenames.py", "rename_vms_to_machines.py"):
    ok("%s present" % _s) if os.path.exists(os.path.join(SCR, _s)) else \
        bad("MISSING %s — the claim shells out to it; restore from the repo or durable mirror"
            % os.path.join(SCR, _s))
# NOT required to claim — these are operator-invoked afterwards (verify / clean up). Warn only:
# failing a claim because a *cleanup* tool is absent would be its own kind of wrong.
for _s in ("gates.py", "teardown_claim.py"):
    if not os.path.exists(os.path.join(SCR, _s)):
        warn("%s not in the working dir — the claim will run, but you cannot %s from here"
             % (_s, "verify parity" if _s == "gates.py" else "tear this claim down"))

# ---- 5. template VMs must all be powered OFF -------------------------------------------------
# A powered-ON template VM means a half-frozen artifact; stage A also uses power_state as the
# template/live discriminator, so a running one would be cloned mid-write.
if U and P:
    raw = sh("curl", "-sk", "--max-time", "45", "-u", "%s:%s" % (U, P), "-X", "POST",
             PC + "/api/nutanix/v3/vms/list", "-H", "Content-Type: application/json",
             "-H", "NTNX-Request-Id: " + str(uuidlib.uuid4()),
             "-d", json.dumps({"filter": "vm_name==%s.*" % PREFIX, "length": 30}), to=55)
    try:
        ents = json.loads(raw or "{}").get("entities") or []
        on = [e["status"]["name"] for e in ents
              if e["status"]["resources"].get("power_state") == "ON"]
        if not ents:
            bad("template %r matched NO VMs" % PREFIX)
        elif on:
            # NOTE: name-prefix matching also catches day-2 VMs of OTHER clusters (CAPX names them
            # after the ClusterClass) — so report, don't assume these are template members.
            warn("VMs matching %r are powered ON: %s — confirm by UUID that none are template members"
                 % (PREFIX, on))
        else:
            ok("all %d VMs matching %r are OFF (template frozen)" % (len(ents), PREFIX))
    except Exception as e:
        bad("could not list template VMs: %s" % e)

# ---- 5b. the template's own control plane must be DEAD ---------------------------------------
# qa-sn-tmpl1 (2026-07-27): a CAPX duplicate-name VM pair meant freeze powered off the VM that CAPI
# called the node, while the rogue twin kept running the real cluster. The template LOOKED frozen
# (every providerID VM was OFF) yet served HTTP 200, and the disk we cloned held a half-bootstrapped
# 16.8MB etcd. The claim only surfaced this ~3.5min in, as an opaque "the server doesn't have a
# resource type machines". This is a 2-second check for exactly that.
tconf = os.path.join(SCR, "%s.conf" % PREFIX)
if os.path.exists(tconf):
    srv = sh("kubectl", "--kubeconfig", tconf, "config", "view", "--minify",
             "-o", "jsonpath={.clusters[0].cluster.server}", to=20).strip()
    if srv:
        code = sh("curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
                  srv.rstrip("/") + "/healthz", to=15).strip()
        if code == "200":
            bad("template %r is STILL RUNNING at %s — it is not frozen. Its disks are not the "
                "converged state, so a claim will boot a blank keyspace. Look for a duplicate-name "
                "VM pair on PC and resolve by UUID." % (PREFIX, srv))
        else:
            ok("template control plane is down (%s)" % (code or "no response"))

# ---- 6. requested IPs must be free -----------------------------------------------------------
# .246 once passed a ping check yet hosted a LIVE apiserver — ping alone is not enough.
for label, ip in (("VIP", VIP), ("LB", LB)):
    code = sh("curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
              "https://%s:6443/healthz" % ip, to=15).strip()
    pinged = subprocess.run(["ping", "-c1", "-W2", ip], capture_output=True).returncode == 0
    if code == "200":
        bad("%s %s hosts a LIVE apiserver — do not use" % (label, ip))
    elif pinged:
        warn("%s %s answers ping (no apiserver) — something is using it" % (label, ip))
    else:
        ok("%s %s appears free" % (label, ip))
if VIP == LB:
    bad("VIP and LB are the same address")

# ---- 7. nothing else in flight ---------------------------------------------------------------
# Concurrency is supported, but a claim running beside a teardown/freeze corrupts MEASUREMENTS and
# once caused a tool failure under PC load.
busy = []
# match the SCRIPT, not any command line containing the name: "claim.py" is a substring of
# "measure_claim.py" and "teardown_claim.py", which produced a false "in flight" warning.
for proc in ("claim.py", "teardown_claim.py", "freeze.py"):
    n = [x for x in sh("pgrep", "-af", proc).splitlines() if ("/"+proc) in x or x.endswith(proc)]
    if n: busy.append("%s(%d)" % (proc, len(n)))
ok("no other claim/teardown/freeze in flight") if not busy else \
    warn("in flight: %s — fine for a real claim, NOT for a timing measurement" % ", ".join(busy))

# ---- verdict ----------------------------------------------------------------------------------
print()
if fails:
    print("PREFLIGHT FAILED (%d): %s" % (len(fails), fails[0]))
    sys.exit(1)
print("PREFLIGHT OK%s" % ("  (%d warning(s) — read them)" % len(warns) if warns else ""))
