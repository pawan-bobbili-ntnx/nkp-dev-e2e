#!/usr/bin/env python3
"""Rename clone VMs on Prism Central to match their CAPI Machine names.

FINDING (2026-07-12): CAPX has a NAME-GUARD on delete. In nutanixmachine-controller it resolves
the VM by providerID/vmUUID and then REFUSES to act if the VM's PC name differs from the
Machine/NutanixMachine name:
    found VM with UUID <uuid> but name <qa3cp3-cp0> did not match
    Machine name <qa-tmpl-3cp-c98rf-6r5w7> or NutanixMachineName <...>
Our clone flow names the VMs "<claim>-cp0/cp1/cp2/md3" while the cloned CAPI Machines keep the
TEMPLATE's generated names. Unpause tolerates this (Ready NutanixMachines short-circuit the VM
lookup), but ANY delete does not -> the old CP VM can't be deleted -> a CP roll / scale-in /
remediation wedges forever at `WaitingForInfrastructureDeletion`.

=> For a clone to be day-2 capable, each VM's PC name MUST equal its CAPI Machine name.
   This script fixes an existing clone; the clone flow should name VMs this way at claim time
   (or rename right after the CAPI-tree re-mint).

Usage: rename_vms_to_machines.py <kubeconfig>   (PC creds from NKP_NUTANIX_USER/PASSWORD)
"""
import json, os, subprocess, sys, time

KC = sys.argv[1]
U, P = (os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"]), (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])
V3 = (os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)")) + "/api/nutanix/v3"
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

NS = _detect_ns(KC)


def kx(*a):
    return subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=25s", *a],
                          capture_output=True, text=True)


def curl(args, to=60):
    return subprocess.run(["curl", "-sk", "--max-time", str(to), "-u", "%s:%s" % (U, P)] + args,
                          capture_output=True, text=True).stdout


# 1. map providerID(vmUUID) -> desired Machine name, from the CAPI tree
r = kx("get", "machine", "-n", NS, "-o",
       "jsonpath={range .items[*]}{.metadata.name}|{.spec.providerID}{'\\n'}{end}")
want = {}
for line in r.stdout.splitlines():
    if "|" not in line:
        continue
    name, pid = line.split("|", 1)
    uuid = pid.strip().replace("nutanix://", "")
    if uuid:
        want[uuid] = name.strip()
print("CAPI machines (vmUUID -> desired VM name):")
for u, n in want.items():
    print("  %s -> %s" % (u[:8], n))

# 2. list VMs, rename any whose name != desired
ents = json.loads(curl(["-X", "POST", V3 + "/vms/list", "-H", "Content-Type: application/json",
                        "-d", '{"kind":"vm","length":500}'])).get("entities", [])
# COMPRESS (2026-07-24): rename all VMs CONCURRENTLY (was serial: 5 x ~10s task-waits)
from concurrent.futures import ThreadPoolExecutor
# DUPLICATE-NAME GUARD (bcB1 postmortem, 2026-07-25): if the CAPI Machines have NOT been re-minted
# to the claim's names (e.g. remint_machinenames failed), `want` still holds the TEMPLATE's names
# and renaming would give this clone's VMs names identical to the live template's VMs — the CAPX
# duplicate-VM hazard, separable only by UUID. Never rename onto a name another VM already owns.
_byname = {}
for _e in ents:
    _byname.setdefault(_e["status"]["name"], _e["metadata"]["uuid"])
_dupes = []

def _rename_one(e):
    uuid = e["metadata"]["uuid"]
    if uuid not in want:
        return 0
    cur = e["status"]["name"]
    desired = want[uuid]
    if cur == desired:
        print("  %s already named correctly (%s)" % (uuid[:8], cur)); return 0
    _owner = _byname.get(desired)
    if _owner and _owner != uuid:
        _dupes.append((uuid, desired, _owner))
        print("  REFUSING %s: %r is already the name of VM %s — renaming would create a "
              "duplicate-name pair (did remint_machinenames run?)" % (uuid[:8], desired, _owner[:8]))
        return 0
    g = json.loads(curl([V3 + "/vms/" + uuid]))
    g["spec"]["name"] = desired
    resp = json.loads(curl(["-X", "PUT", V3 + "/vms/" + uuid, "-H", "Content-Type: application/json",
                            "-d", json.dumps({"spec": g["spec"], "metadata": g["metadata"],
                                              "api_version": "3.1"})]))
    task = (resp.get("status", {}).get("execution_context") or {}).get("task_uuid")
    ok = False
    for _ in range(24):
        time.sleep(3)
        t = json.loads(curl([V3 + "/tasks/" + task])) if task else {}
        if t.get("status") == "SUCCEEDED": ok = True; break
        if t.get("status") == "FAILED": break
    print("  RENAME %s: %s -> %s : %s" % (uuid[:8], cur, desired, "OK" if ok else "FAILED"))
    return 1 if ok else 0
with ThreadPoolExecutor(max_workers=6) as _ex:
    renamed = sum(_ex.map(_rename_one, ents))
print("renamed %d VM(s). CAPX name-guard should now permit delete." % renamed)
if _dupes:
    print("FATAL: refused %d rename(s) that would have duplicated an existing VM name: %s"
          % (len(_dupes), [(u[:8], n) for u, n, _o in _dupes]))
    sys.exit(3)   # non-zero -> boc3cp records a must-tool failure and aborts before boot
