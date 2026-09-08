#!/usr/bin/env python3
"""Refresh the clone's nutanix PC-credential secrets after a PC cred rotation.
New creds read from ENV (NKP_NUTANIX_USER/NKP_NUTANIX_PASSWORD) — never on the command line.
Format-aware: sets username/password fields; never needs the old password.

  refresh-creds.py <kubeconfig>
"""
import base64, json, os, subprocess, sys

KC = sys.argv[1]
NEWU = (os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"])
NEWP = (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])

def k(*args, inp=None, t=20):
    return subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=%ds" % t, *args],
                          capture_output=True, text=True, input=inp)

def get_secret(ns, name):
    r = k("get", "secret", name, "-n", ns, "-o", "json")
    return json.loads(r.stdout) if r.returncode == 0 else None

def patch_data(ns, name, newdata):
    r = k("patch", "secret", name, "-n", ns, "--type=merge", "-p", json.dumps({"data": newdata}))
    print("  %s/%s: %s" % (ns, name, "OK" if r.returncode == 0 else r.stderr[:90]))

def set_in_json(obj):
    """Recursively set any username/password (and prismCentral.username/password) to new creds."""
    if isinstance(obj, dict):
        for kk in list(obj.keys()):
            if kk in ("username", "user"): obj[kk] = NEWU
            elif kk in ("password", "pass"): obj[kk] = NEWP
            else: set_in_json(obj[kk])
    elif isinstance(obj, list):
        for it in obj: set_in_json(it)
    return obj

# discrete-key secrets: separate base64 username/password keys (not a blob)
# konnector-agent (ntnx-system) missed by the pattern-discovery below -> onboarding
# init container crashloops with "invalid Nutanix credentials" (found 2026-07-04)
for ns, name in [("capx-system", "global-nutanix-credentials"),
                 ("ntnx-system", "konnector-agent")]:
    sec = get_secret(ns, name)
    if sec and "username" in (sec.get("data") or {}):
        patch_data(ns, name, {"username": base64.b64encode(NEWU.encode()).decode(),
                              "password": base64.b64encode(NEWP.encode()).decode()})

# (namespace, name, key, format) — format: 'json' | 'colon' (endpoint:port:user:pass)
targets = [
    ("kube-system", "nutanix-ccm-credentials", "credentials", "json"),
    ("ntnx-system", "nutanix-csi-credentials", "key", "colon"),
]
# auto-discover any *pc-credentials* / *prism* secret (these are TOPOLOGY-referenced and used by
# CCM/CAREN — e.g. kommander/<cluster>-pc-credentials{,-for-csi,-for-konnector-agent}).
#
# BUG FIXED 2026-07-12: this used a 20s-timeout `get secrets -A -o json`, which TIMES OUT on a
# converged kommander cluster -> empty stdout -> json.loads raised -> the whole discovery was lost
# and those secrets were SILENTLY never refreshed. Real-world blast radius: after a PC cred
# rotation, CAPX/CCM get an OIDC auth redirect ("unsupported protocol scheme") and cannot reach
# Prism Central at all -> VM create/delete hangs forever (seen live: a control-plane machine stuck
# in Deleting/WaitingForInfrastructureDeletion, stalling an entire CP roll).
# Fix: cheap name-only listing with a long timeout, per-secret fetch, and FAIL LOUD.
disc = k("get", "secrets", "-A", "-o",
         "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{'\\n'}{end}", t=90)
if disc.returncode != 0:
    print("  FATAL: secret discovery failed (%s) — pc-credentials would be missed"
          % disc.stderr[:100].strip())
    sys.exit(2)
found = 0
for line in disc.stdout.splitlines():
    if "/" not in line:
        continue
    ns, nm = line.split("/", 1)
    if ("pc-credentials" not in nm) and ("prism" not in nm):
        continue
    if any(t[1] == nm for t in targets):
        continue
    sec = get_secret(ns, nm)
    if not sec:
        continue
    for key in (sec.get("data") or {}):
        targets.append((ns, nm, key, "auto")); found += 1
print("  discovered %d pc-credential/prism secret keys" % found)

for ns, name, key, fmt in targets:
    sec = get_secret(ns, name)
    if not sec or key not in (sec.get("data") or {}):
        print("  %s/%s[%s]: not found, skip" % (ns, name, key)); continue
    dec = base64.b64decode(sec["data"][key]).decode()
    try:
        # discrete plain-string keys FIRST — a discovered secret can hold bare
        # username/password values (kommander/<cluster>-pc-credentials-for-konnector-agent),
        # which are NOT JSON; feeding them to json.loads was the "parse error" noise.
        if key in ("username", "user"):
            newval = NEWU
        elif key in ("password", "pass"):
            newval = NEWP
        elif fmt in ("json", "auto") and dec.lstrip().startswith(("[", "{")):
            newval = json.dumps(set_in_json(json.loads(dec)))
        elif fmt == "colon" or (dec.count(":") >= 3 and "{" not in dec):
            parts = dec.strip().split(":")
            # endpoint:port:user:pass  -> keep endpoint+port, replace user+pass
            if len(parts) >= 4:
                parts[-2], parts[-1] = NEWU, NEWP
                newval = ":".join(parts)
            else:
                print("  %s/%s: unexpected colon format, skip" % (ns, name)); continue
        else:
            # not a credential blob we understand (e.g. prism-central-metadata holds a UUID) —
            # only touch it if it actually parses as JSON with cred fields
            if not dec.lstrip().startswith(("[", "{")):
                print("  %s/%s[%s]: not a cred blob, skip" % (ns, name, key)); continue
            newval = json.dumps(set_in_json(json.loads(dec)))
        patch_data(ns, name, {key: base64.b64encode(newval.encode()).decode()})
    except Exception as ex:
        print("  %s/%s: parse error %s" % (ns, name, ex))

print("done. restart CCM/CSI to pick up new creds.")
