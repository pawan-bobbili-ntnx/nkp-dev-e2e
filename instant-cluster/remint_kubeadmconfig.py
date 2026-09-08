#!/usr/bin/env python3
"""Re-mint cloned CP machines' KubeadmConfig to match KCP's desired state -> SUPPRESS the CP roll.

THE PROBLEM (finding 2, root-caused 2026-07-12 against CAPI v1.12.5 source):
On unpause, KCP compares each CP Machine's KubeadmConfig against the desired one it computes from
KCP.spec.kubeadmConfigSpec. Any diff => MachinesUpToDate=False => KCP AUTOMATICALLY ROLLS the whole
control plane. For a clone that means all 3 cloned CPs get destroyed and rebuilt AT CLAIM TIME —
which throws away the converged state we cloned for, and blows the time budget completely.

WHAT ACTUALLY DIFFS (measured, not assumed):
  1. files[kube-vip.yaml].content — ONE line: the kube-vip `address` env = the OLD template VIP.
  2. clusterConfiguration.etcd.local.imageTag      3.5.24-0 (template) vs 3.6.6-0 (post-PHASE-7.1)
  3. clusterConfiguration.apiServer.extraArgs[encryption-provider-config] + the
     files[encryptionconfig.yaml] entry                (template) vs absent (post-PHASE-7.1)

WHAT DOES *NOT* DIFF — verified in cluster-api@v1.12.5 controlplane/kubeadm/internal/filters.go,
PrepareKubeadmConfigsForDiff():
  - ClusterConfiguration.ControlPlaneEndpoint  -> line 322: copied desired->current before the diff
    ("ControlPlaneEndpoint should also never change for a Cluster, so no reason to trigger a
      rollout because of that")
  - JoinConfiguration.Discovery (apiServerEndpoint + bootstrapToken) -> lines 338-339: ZEROED on
    both sides ("Changes to Discovery will apply for the next join, but will not lead to a rollout")
  - DNS, ControlPlaneComponentHealthCheckSeconds, and omittable empty/nil fields.
So the VIP is rollout-neutral EVERYWHERE except the kube-vip static-pod file, which is compared
verbatim as opaque file content.

WHY PATCHING IS CORRECT, NOT A HACK:
the node's ACTUAL on-disk /etc/kubernetes/manifests/kube-vip.yaml ALREADY carries the new VIP
(vip-rebind rewrote it), its etcd ALREADY runs 3.6.6, and its apiserver ALREADY has no encryption
provider. The cloned KubeadmConfig is simply LYING about the machine it describes — it still carries
the TEMPLATE's facts. We re-mint it to reality, exactly as PHASE 7 re-mints the stale providerID.
Result: the CAPI record becomes TRUE *and* UpToDate=True, so no roll. Both, from one fix.

Usage: remint_kubeadmconfig.py <kubeconfig> [--dry-run]
"""
import json, subprocess, sys

KC = sys.argv[1]
DRY = "--dry-run" in sys.argv
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


def kx(*a, inp=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=30s", *a],
                       capture_output=True, text=True, input=inp)
    return r


# 1. desired: KCP's kubeadmConfigSpec
r = kx("get", "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io", "-n", NS, "-o", "json")
if r.returncode != 0:
    print("FATAL: cannot read KCP:", r.stderr[:200]); sys.exit(1)
kcps = json.loads(r.stdout)["items"]
if len(kcps) != 1:
    print("FATAL: expected exactly 1 KCP, found %d" % len(kcps)); sys.exit(1)
kcp = kcps[0]
desired = kcp["spec"]["kubeadmConfigSpec"]
print("KCP %s: desired kubeadmConfigSpec has %d files" % (kcp["metadata"]["name"],
                                                          len(desired.get("files", []))))

# 2. every CP machine (owned by the KCP) -> its KubeadmConfig
r = kx("get", "machines", "-n", NS, "-o", "json")
machines = json.loads(r.stdout)["items"]
cp = [m for m in machines
      if any(o.get("kind") == "KubeadmControlPlane" for o in m["metadata"].get("ownerReferences", []))]
print("control-plane machines: %d" % len(cp))

patched = 0
for m in cp:
    mn = m["metadata"]["name"]
    ref = (m["spec"].get("bootstrap") or {}).get("configRef") or {}
    bc = ref.get("name")
    if not bc:
        print("  %s: no bootstrap configRef — skip" % mn); continue

    r = kx("get", "kubeadmconfig", bc, "-n", NS, "-o", "json")
    if r.returncode != 0:
        print("  %s: cannot read KubeadmConfig %s — skip" % (mn, bc)); continue
    cur = json.loads(r.stdout)["spec"]

    # Only the rollout-relevant, machine-INDEPENDENT parts. initConfiguration/joinConfiguration are
    # per-machine (node name, local endpoint, discovery token) and are excluded from CAPI's diff, so
    # we deliberately leave them untouched.
    patch = {}
    if json.dumps(cur.get("files"), sort_keys=True) != json.dumps(desired.get("files"), sort_keys=True):
        patch["files"] = desired.get("files")
    if json.dumps(cur.get("clusterConfiguration"), sort_keys=True) != \
       json.dumps(desired.get("clusterConfiguration"), sort_keys=True):
        patch["clusterConfiguration"] = desired.get("clusterConfiguration")

    if not patch:
        print("  %s (%s): already matches desired — no-op" % (mn, bc)); continue
    print("  %s (%s): re-minting %s" % (mn, bc, "+".join(sorted(patch))))
    if DRY:
        continue
    # merge patch: lists (files, extraArgs) are REPLACED wholesale, which is exactly what we need to
    # DROP the stale encryptionconfig file / encryption-provider-config arg.
    r = kx("patch", "kubeadmconfig", bc, "-n", NS, "--type=merge",
           "-p", json.dumps({"spec": patch}))
    if r.returncode != 0:
        print("    FAILED: %s" % (r.stderr or "")[:300]); sys.exit(2)
    patched += 1

print("re-minted %d KubeadmConfig(s)%s" % (patched, " (dry-run)" if DRY else ""))
