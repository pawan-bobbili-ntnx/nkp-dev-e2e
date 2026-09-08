#!/usr/bin/env python3
"""Uniform claim measurement — every run measured the same way, so lever effects can be compared
against a known variance band instead of eyeballed. Poll-based, so it must be started BEFORE (or
alongside) the claim.

  measure_claim.py <claim-name> [max_minutes]

Emits one JSON line to <claim>-metrics.json with milestones in seconds measured from the claim's
own T0 (process launch, taken from the boc3cp log's first line) AND from window-complete, since
the post-window tail is the part that varies most.
"""
import json, os, re, subprocess, sys, time

SCR = os.path.dirname(os.path.abspath(__file__))
NAME = sys.argv[1]
MAXS = float(sys.argv[2]) * 60 if len(sys.argv) > 2 else 45 * 60
LOG = os.path.join(SCR, NAME.replace("qa3-", "") + ".log")
KC = os.path.join(SCR, "%s-claim.conf" % NAME)
PB = os.path.join(SCR, "%s-postboc.log" % NAME)
M = {"claim": NAME}


def kx(*a, to=25):
    if not os.path.exists(KC): return ""
    return subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=%ds" % to, *a],
                          capture_output=True, text=True).stdout


def logval(pat):
    """seconds-from-T0 for a '+Ns] <pat>' line in the boc3cp log"""
    if not os.path.exists(LOG): return None
    m = re.search(r"\+(\d+)s\] " + pat, open(LOG, errors="ignore").read())
    return int(m.group(1)) if m else None


t_start = time.time()
while time.time() - t_start < MAXS:
    time.sleep(10)
    # --- window-side milestones come straight from the claim log (authoritative, no polling race)
    for key, pat in (("stageA_s", r"map: \d+ CPs"), ("prep_done_s", r"WINDOW: starting staged etcd"),
                     ("restore_s", r"COMPRESS-A: restored pairs"), ("gitatrest_s", r"git-at-rest:"),
                     ("close_s", r"closing window"), ("kubelets_s", r"starting ALL kubelets"),
                     ("window_s", r"BOC3 WINDOW COMPLETE")):
        if M.get(key) is None:
            v = logval(pat)
            if v is not None: M[key] = v
    if M.get("window_s") is None:
        continue                                   # window not done yet; nothing post-window to measure

    # --- post-window milestones, measured in seconds AFTER window-complete
    now_rel = None
    if os.path.exists(LOG):
        # wall-clock anchor: window_s is relative to claim T0; use file mtime deltas for post-window
        now_rel = int(time.time() - os.path.getmtime(LOG))
    if M.get("git_running_s") is None:
        g = kx("get", "pod", "git-operator-git-0", "-n", "git-operator-system", "--no-headers")
        if g and len(g.split()) > 2 and g.split()[2] == "Running":
            M["git_running_s"] = now_rel
    if M.get("hydrated_s") is None and os.path.exists(PB):
        _pb = open(PB, errors="ignore").read()
        # "git_hydrate complete" covers BOTH endings: the full rebuild ("HYDRATION CONVERGED")
        # and the skip-if-already-correct no-op path added 2026-07-26.
        if "HYDRATION CONVERGED" in _pb or "git_hydrate complete" in _pb:
            M["hydrated_s"] = now_rel
            M["hydrate_skipped"] = "SKIPPING rebuild" in _pb
    if M.get("green_s") is None:
        pods = kx("get", "pods", "-A", "--no-headers")
        if pods:
            bad = [l for l in pods.splitlines()
                   if len(l.split()) > 3 and l.split()[3] not in ("Running", "Completed", "Succeeded")]
            nk = kx("get", "nkpcluster", "-A", "--no-headers")
            if not bad and "Reconciled" in nk:
                M["green_s"] = now_rel
    # sweep activity (how much wrinkle-2 churn this run saw)
    if os.path.exists(PB):
        txt = open(PB, errors="ignore").read()
        M["foreign_detaches"] = txt.count("wrinkle2-sweep: detached")
    if M.get("green_s") is not None and M.get("hydrated_s") is not None:
        break

M["total_s"] = (M.get("window_s") or 0) + (M.get("green_s") or 0)
json.dump(M, open(os.path.join(SCR, "%s-metrics.json" % NAME), "w"), indent=1)
print(json.dumps(M))
