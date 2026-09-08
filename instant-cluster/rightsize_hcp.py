#!/usr/bin/env python3
"""Single-node right-sizing (standalone port of stage B PHASE 6.68).

  rightsize_hcp.py <kubeconfig>

On one node the 2nd replica of cilium-operator/csi-controller is unschedulable forever (hard
anti-affinity on hostname), and csi's maxSurge:0 rollout deadlocks at replicas=2. Injects
replicas: 1 into the HCP valuesTemplates (the CAAPH source of truth) so one helm upgrade fixes
both. Values keys verified against cilium 1.19.2 (operator.replicas) and nutanix-csi-storage
3.7.1 (controller.replicas)."""
import json, subprocess, sys, time

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

KC = sys.argv[1]
NS = _detect_ns(KC)

def log(m): print("[RIGHTSIZE %s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def kx(*a, tries=3):
    for _ in range(tries):
        r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=22s", *a],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
        time.sleep(3)
    return None

def inject(hcp_prefix, block_key, indent_insert):
    hn = next((x for x in (kx("get", "helmchartproxy", "-n", NS, "-o",
              "jsonpath={range .items[*]}{.metadata.name} {end}") or "").split()
              if x.startswith(hcp_prefix + "-")), "")
    if not hn:
        log("no HCP with prefix %r — skipped" % hcp_prefix); return
    vt = kx("get", "helmchartproxy", hn, "-n", NS, "-o", "jsonpath={.spec.valuesTemplate}") or ""
    if not vt:
        log("%s empty valuesTemplate — skipped" % hn); return
    lines = vt.splitlines()
    if any(l.startswith(block_key + ":") for l in lines):
        out, in_block = [], False
        for l in lines:
            if l.startswith(block_key + ":"):
                out.append(l); in_block = True
                out.append(indent_insert + "replicas: 1")
                continue
            if in_block and l.strip().startswith("replicas:"):
                continue
            if in_block and l and not l.startswith(" "):
                in_block = False
            out.append(l)
        new = "\n".join(out)
    else:
        new = vt.rstrip("\n") + "\n%s:\n%sreplicas: 1\n" % (block_key, indent_insert)
    if new == vt:
        log("%s already right-sized" % hn); return
    pp = kx("patch", "helmchartproxy", hn, "-n", NS, "--type=merge",
            "-p", json.dumps({"spec": {"valuesTemplate": new}}))
    log("%s -> %s.replicas=1 (%s)" % (hn, block_key, "patched" if pp is not None else "PATCH FAILED"))

inject("cilium", "operator", "  ")
inject("nutanix-csi", "controller", "  ")
