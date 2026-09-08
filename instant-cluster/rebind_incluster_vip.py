#!/usr/bin/env python3
"""Rebind every IN-CLUSTER reference to the control-plane VIP: OLD -> NEW.

WHY THIS EXISTS (measured on the qa3cp4 clean-room run, 2026-07-12):
The at-rest disk surgery rewrites the VIP only in ON-DISK files on the CONTROL-PLANE nodes. It does NOT
touch anything stored INSIDE the cluster. That was invisible for as long as the clone flow reused
the template's VIP; the moment each clone gets its OWN VIP (which is what makes N concurrent clones
possible at all), every in-cluster copy of the old VIP becomes a live landmine:

  deploy/nutanix-cloud-controller-manager  env KUBERNETES_SERVICE_HOST = OLD  (hostNetwork=true)
      -> CCM cannot reach the apiserver -> exits 1 instantly, NO log output at all
      -> nodes NEVER get a providerID -> stage B PHASE 6 re-mint fails -> claim dead.
  ds/cilium + ds/cilium-envoy + deploy/cilium-operator   KUBERNETES_SERVICE_HOST = OLD
      -> cilium agents Init:CrashLoopBackOff -> cilium-cni cannot reach the agent
      -> NO pod gets a network sandbox -> ~100 pods stranded -> admission webhooks never come up.
  cm/kube-proxy, cm/kubeadm-config, cm/nutanix-config
  Secrets (CAPI *-kubeconfig): the VIP is base64-encoded INSIDE the kubeconfig, so a plain string
      scan misses it. CAPI itself uses that kubeconfig to reach the workload cluster.

ORDERING TRAP (learned the hard way): the CAAPH HelmChartProxy is cilium's source of truth, but its
webhook (caaph-webhook) needs a working CNI, and the CNI needs this patch. So a HCP-first strategy
DEADLOCKS. Patch the DaemonSet/Deployment FIRST (needs no webhook) so cilium recovers, and only then
patch the HCP -- otherwise CAAPH later re-renders the DS back to the OLD VIP.

Usage: rebind_incluster_vip.py <kubeconfig> <old-vip> <new-vip>
"""
import base64, json, os, subprocess, sys

KC, OLD, NEW = sys.argv[1], sys.argv[2], sys.argv[3]


def kx(*a, inp=None):
    return subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=60s", *a],
                          capture_output=True, text=True, input=inp)


def apply(obj):
    # strip server-managed fields that make a re-apply fail
    obj["metadata"].pop("managedFields", None)
    obj["metadata"].pop("resourceVersion", None)
    return kx("apply", "-f", "-", inp=json.dumps(obj)).returncode == 0


changed, failed = [], []

# --- 1. plain-text holders: ConfigMaps / Deployments / DaemonSets / StatefulSets -------------
# Patch these FIRST and WITHOUT touching CAAPH: they need no webhook, and fixing cilium here is what
# brings the admission chain back to life so the HelmChartProxy patch below can even be accepted.
# ORDER (2026-07-14, qa-sn7 finding): DAEMONSETS FIRST. The cilium DS rebind triggers a ~2.5min
# agent roll that is the claim's longest convergence — every second it starts earlier is a second
# off the total. The early-49 overlap usually gets killed at stage-A exit (~85s in), so whatever
# is first here is what actually lands early. configmaps carry no rollout cost and can wait.
# TARGETED mode (2026-07-14): the VIP-carrying set has been IDENTICAL across 10 measured claims;
# the four cluster-wide `get -A -o json` scans cost ~35s on a busy node while the patches cost ~5s.
# Pass 2 (6.7, full scan) remains the completeness catch-all — a missed object lands there.
TARGETED = [("configmap","kube-public","cluster-info"),("configmap","kube-system","kubeadm-config"),
            ("configmap","kube-system","nutanix-config"),("daemonset","kube-system","cilium"),
            ("daemonset","kube-system","cilium-envoy"),("deployment","kube-system","cilium-operator"),
            ("deployment","kube-system","nutanix-cloud-controller-manager"),
            ("deployment","ntnx-system","konnector-agent")]
if os.environ.get("REBIND_TARGETED"):
    for kind, ns, name in TARGETED:
        g = kx("get", kind, "-n", ns, name, "-o", "json")
        if g.returncode != 0:
            continue
        item = json.loads(g.stdout); blob = json.dumps(item)
        if OLD not in blob:
            continue
        (changed if apply(json.loads(blob.replace(OLD, NEW))) else failed).append("%s %s/%s" % (kind, ns, name))
    print("rebound %d object(s), %d failure(s) [targeted pass-1; pass 2 is the catch-all]"
          % (len(changed), len(failed)))
    raise SystemExit(0)

for kind in ("daemonset", "deployment", "configmap", "statefulset"):
    r = kx("get", kind, "-A", "-o", "json")
    if r.returncode != 0:
        continue
    for item in json.loads(r.stdout).get("items", []):
        blob = json.dumps(item)
        if OLD not in blob:
            continue
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        new_item = json.loads(blob.replace(OLD, NEW))
        (changed if apply(new_item) else failed).append("%s %s/%s" % (kind, ns, name))

# FAST MODE (pass-1, 2026-07-14): secrets + HCPs are webhook-gated — applying them while the
# admission chain is still down burns a 60s timeout PER OBJECT (measured: pass-1 = 2m16s of
# doomed retries). Pass 2 (6.7) handles them once webhooks answer. REBIND_SKIP_GATED=1 skips
# both sections; the CM/deploy/DS rebinds above are the only pre-webhook-critical objects.
import os as _os
if _os.environ.get("REBIND_SKIP_GATED"):
    print("rebound %d object(s), %d failure(s) [fast mode: secrets+HCPs deferred to pass 2]"
          % (len(changed), len(failed)))
    raise SystemExit(0)

# --- 2. Secrets: the VIP is base64-encoded inside (CAPI kubeconfigs) -> a raw scan MISSES it ----
r = kx("get", "secret", "-A", "-o", "json")
if r.returncode == 0:
    for item in json.loads(r.stdout).get("items", []):
        if item.get("type", "").startswith("kubernetes.io/service-account"):
            continue
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        data, hit = item.get("data") or {}, False
        for k, v in list(data.items()):
            try:
                raw = base64.b64decode(v).decode("utf-8")
            except Exception:
                continue          # genuinely binary -> not a kubeconfig, skip
            if OLD in raw:
                data[k] = base64.b64encode(raw.replace(OLD, NEW).encode()).decode()
                hit = True
        if hit:
            item["data"] = data
            (changed if apply(item) else failed).append("secret %s/%s" % (ns, name))

# --- 3. CAAPH HelmChartProxies LAST: they are the source of truth, but their webhook needs the CNI
#        that step 1 just repaired. Patching them first deadlocks.
r = kx("get", "helmchartproxy", "-A", "-o", "json")
if r.returncode == 0:
    for item in json.loads(r.stdout).get("items", []):
        blob = json.dumps(item)
        if OLD not in blob:
            continue
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        new_item = json.loads(blob.replace(OLD, NEW))
        (changed if apply(new_item) else failed).append("helmchartproxy %s/%s" % (ns, name))

for c in changed:
    print("  rebound %s" % c)
for f in failed:
    print("  FAILED  %s" % f)
print("rebound %d object(s), %d failure(s)" % (len(changed), len(failed)))

# A leftover HelmChartProxy is FATAL, not cosmetic: CAAPH will re-render the DaemonSet back to the
# OLD VIP and silently break cilium again, minutes or hours later.
sys.exit(1 if failed else 0)
