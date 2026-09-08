#!/usr/bin/env python3
"""Give every cloned CAPI Machine a UNIQUE, claim-prefixed name (rename-by-recreate).

WHY: Machine metadata.name is immutable, and a clone copies the template's CAPI tree verbatim, so
every clone carries the template's machine names (the KCP hash was rolled ONCE, at template build).
CAPX's name-guard then forces every clone's VMs to take those same names on the PC -> with N clones,
each machine name appears N+1 times in Prism. Unique per-claim machine names fix both `kubectl get
machines` and the PC duplicate-name problem. (Cluster name + node hostnames remain template's —
those are a rebuild/roll respectively, accepted deltas.)

HOW (the cheap way — paused-tree surgery, NOT the CP roll):
  per Machine, while the Cluster is PAUSED (controllers idle, so no reconcile races):
   1. CREATE a replacement Machine: new name "<claim>-cp-<hash>"/"<claim>-md-<hash>", same spec
      (providerID, bootstrap.configRef, infrastructureRef, dataSecretName), same labels,
      same ownerReferences (KCP / MachineSet — owner UIDs unchanged, so ownership just works).
   2. RE-POINT the children's ownerReferences at the new Machine (name + NEW uid).
      *** THIS IS THE STEP THAT PREVENTS DISASTER: KubeadmConfig + NutanixMachine carry an
      ownerReference to the OLD Machine's UID; delete the old Machine without re-pointing and the
      garbage collector CASCADES — it deletes the bootstrap config and NutanixMachine of a LIVE
      control plane. ***
   3. Strip the old Machine's finalizers, then delete it (finalizer would otherwise invoke
      infra-deletion teardown; with it stripped + paused, the delete is pure bookkeeping).
  On unpause the machine controller re-binds status.nodeRef via providerID (same mechanism the
  claim already relies on) and updates the node's cluster.x-k8s.io/machine annotation.
  PHASE 12 asserts the outcome: every machine Running-with-NodeRef + no-roll + day-2 rename check.

Usage: remint_machinenames.py <kubeconfig> <claim-name>
"""
import json, secrets, subprocess, sys

KC, CLAIM = sys.argv[1], sys.argv[2]


def _detect_ns():
    """Namespace holding the CAPI Cluster: `kommander` on 2.18, `default` on 2.17.

    Hardcoding "kommander" here made this tool fail on a 2.17 GA template with
    the MISLEADING message "Cluster is not paused" - it was looking in a
    namespace that holds no Cluster at all, so the pause check could never see
    the paused cluster it was asked about. The claim then correctly aborted
    before rename (2026-08-31), but pointed at pausing rather than at namespace.
    """
    r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=30s",
                        "get", "cluster", "-A", "--no-headers"],
                       capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        f = line.split()
        if len(f) > 1:
            return f[0]
    return "kommander"


NS = _detect_ns()


def kx(*a, inp=None):
    # RETRY transient API errors (measured: a single "request canceled" mid-surgery FATAL'd a run
    # and left the tree half-pointed — surgery steps must be retry-safe, and all of ours are
    # idempotent PATCH/GET/DELETE calls).
    import time as _t
    for _i in range(5):
        r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=30s", *a],
                           capture_output=True, text=True, input=inp)
        if r.returncode == 0 or "AlreadyExists" in (r.stderr or ""):
            return r
        _t.sleep(3)
    return r


machines = json.loads(kx("get", "machines", "-n", NS, "-o", "json").stdout)["items"]
# IDEMPOTENCY FIRST: on a re-run after unpause everything is already renamed — exit clean before
# the paused-guard (which exists to protect actual SURGERY, not a no-op).
if machines and all(m["metadata"]["name"].startswith(CLAIM + "-") for m in machines):
    print("all %d machines already claim-named — nothing to do" % len(machines)); sys.exit(0)

r = kx("get", "cluster", "-n", NS, "-o", "jsonpath={.items[0].spec.paused}")
if r.stdout.strip() != "true":
    print("FATAL: Cluster is not paused — machine surgery is only safe while paused"); sys.exit(1)
# PARALLEL rename-by-recreate (2026-07-14): each machine's surgery is independent (its own
# replacement Machine, its own children re-point, its own delete) — serial cost was ~50s per
# machine, pure kubectl round-trips. Failures raise and are re-raised at the join.
def _rename_one(m):
    old = m["metadata"]["name"]
    if old.startswith(CLAIM + "-"):
        print("  %s already claim-named — skip" % old); return 0
    is_cp = any(o.get("kind") == "KubeadmControlPlane" for o in m["metadata"].get("ownerReferences", []))
    new = "%s-%s-%s" % (CLAIM, "cp" if is_cp else "md", secrets.token_hex(3))

    # 1. create replacement (same spec/labels/annotations/ownerRefs; fresh identity fields)
    nm = json.loads(json.dumps(m))
    nm["metadata"]["name"] = new
    for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields"):
        nm["metadata"].pop(k, None)
    nm.pop("status", None)
    cr = kx("apply", "-f", "-", inp=json.dumps(nm))
    if cr.returncode != 0:
        print("FATAL: create %s failed: %s" % (new, cr.stderr[:200])); raise RuntimeError('rename failed')
    new_uid = kx("get", "machine", new, "-n", NS, "-o", "jsonpath={.metadata.uid}").stdout.strip()
    if not new_uid:
        print("FATAL: created machine %s has no uid?!" % new); raise RuntimeError('rename failed')

    # 2. re-point children BEFORE deleting the old machine (GC cascade guard)
    old_uid = m["metadata"]["uid"]
    for kind, name in (("kubeadmconfig", (m["spec"].get("bootstrap", {}).get("configRef") or {}).get("name")),
                       ("nutanixmachine", (m["spec"].get("infrastructureRef") or {}).get("name"))):
        if not name:
            continue
        child = json.loads(kx("get", kind, name, "-n", NS, "-o", "json").stdout)
        ors = child["metadata"].get("ownerReferences", [])
        hit = False
        for o in ors:
            if o.get("uid") == old_uid:
                o["name"], o["uid"] = new, new_uid
                hit = True
        if hit:
            pr = kx("patch", kind, name, "-n", NS, "--type=merge",
                    "-p", json.dumps({"metadata": {"ownerReferences": ors}}))
            if pr.returncode != 0:
                print("FATAL: re-point %s/%s failed: %s — old machine NOT deleted (safe)"
                      % (kind, name, pr.stderr[:150])); raise RuntimeError('rename failed')

    # 3. delete old, then strip finalizers.
    # ORDER MATTERS (measured): CAPI's mutating webhook RE-ADDS machine.cluster.x-k8s.io on every
    # update, so a strip-before-delete is silently undone and the paused cluster leaves the object
    # Terminating forever. But once deletionTimestamp is SET, the API forbids adding NEW finalizers
    # — so delete FIRST, then a JSON-patch remove sticks and the object is GC'd immediately.
    kx("delete", "machine", old, "-n", NS, "--wait=false")
    kx("patch", "machine", old, "-n", NS, "--type=json",
       "-p", '[{"op":"remove","path":"/metadata/finalizers"}]')
    gone = False
    for _ in range(30):
        if kx("get", "machine", old, "-n", NS, "--no-headers").returncode != 0:
            gone = True; break
        import time; time.sleep(1)
    print("  %s -> %s (children re-pointed, old %s)" % (old, new, "GONE" if gone else "STILL PRESENT"))
    if not gone:
        print("FATAL: old machine %s would not delete" % old); raise RuntimeError('rename failed')
    return 1


from concurrent.futures import ThreadPoolExecutor
renamed = 0
with ThreadPoolExecutor(max_workers=4) as _ex:
    _futs = [_ex.submit(_rename_one, m) for m in machines]
    _errs = []
    for _f in _futs:
        try:
            renamed += _f.result()
        except Exception as _e:
            _errs.append(str(_e))
if _errs:
    print("FATAL: %d machine rename(s) failed: %s" % (len(_errs), _errs[:2])); sys.exit(2)

# verify: no template-named machines remain, count preserved
after = json.loads(kx("get", "machines", "-n", NS, "-o", "json").stdout)["items"]
stale = [x["metadata"]["name"] for x in after if not x["metadata"]["name"].startswith(CLAIM + "-")]
if len(after) != len(machines) or stale:
    print("FATAL: post-rename tree wrong (count %d->%d, stale=%s)" % (len(machines), len(after), stale))
    sys.exit(4)
print("renamed %d machine(s); tree verified (%d machines, all claim-prefixed)" % (renamed, len(after)))
