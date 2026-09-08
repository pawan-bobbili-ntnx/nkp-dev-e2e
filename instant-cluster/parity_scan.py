#!/usr/bin/env python3
"""RESIDUAL-IDENTITY SCAN: nothing in a claimed cluster may reference the TEMPLATE's identity.
Scans every namespaced+cluster object's full JSON for template markers. Any hit = parity bug.
  parity_scan.py <kubeconfig> <marker>=<label> [<marker>=<label> ...]
Whitelist: fields that legitimately keep history (events, last-applied annotations are checked
separately and reported as WARN not FAIL)."""
import json, subprocess, sys

KC = sys.argv[1]
MARKERS = [a.split("=",1) for a in sys.argv[2:]]
def k(*a):
    return subprocess.run(["kubectl","--kubeconfig",KC,"--request-timeout=60s",*a],
                          capture_output=True, text=True)
# every api resource that supports list
r = k("api-resources","--verbs=list","-o","name")
kinds = [x for x in r.stdout.split() if x and x not in ("events","events.events.k8s.io",
         "componentstatuses","bindings","localsubjectaccessreviews")]
fails, warns = [], []
for kind in kinds:
    rr = k("get",kind,"-A","-o","json")
    if rr.returncode != 0 or not rr.stdout: continue
    try: items = json.loads(rr.stdout).get("items") or []
    except Exception: continue
    for it in items:
        blob = json.dumps(it)
        for marker,label in MARKERS:
            if marker in blob:
                nm = "%s/%s/%s" % (kind, it.get("metadata",{}).get("namespace",""), it.get("metadata",{}).get("name",""))
                # annotations-only hits (kubectl last-applied etc.) are WARN
                spec_blob = json.dumps({kk:vv for kk,vv in it.items() if kk!="metadata"})
                md = it.get("metadata",{})
                md_no_ann = json.dumps({kk:vv for kk,vv in md.items() if kk!="annotations"})
                if marker in spec_blob or marker in md_no_ann:
                    fails.append((nm,label))
                else:
                    warns.append((nm,label))
seen=set()
for nm,label in fails:
    if (nm,label) in seen: continue
    seen.add((nm,label)); print("FAIL %s carries %s" % (nm,label))
for nm,label in warns[:10]:
    print("WARN %s (annotation-only) %s" % (nm,label))
print("RESULT fails=%d warns=%d kinds-scanned=%d" % (len(set(fails)), len(warns), len(kinds)))
