#!/usr/bin/env python3
"""Dev-image override for instant clusters: run YOUR pushed image in a claimed cluster in seconds.

  override_image.py <kubeconfig> apply  <component>=<image-or-tag> [...]
  override_image.py <kubeconfig> revert [<component> ...]          (no names = revert everything)
  override_image.py <kubeconfig> status

<image-or-tag>: a full "repo/name:tag" or a bare "tag" (keeps the component's current repository).

Three validated strategies, resolved automatically per component (2026-07-20/21 proofs):
  flux-app     kommander HelmReleases ship an OPTIONAL valuesFrom ConfigMap `<app>-overrides` —
               a designed override hook outside git (flux cannot revert it; helm-controller
               re-reads it every ~15s). The image values-KEY is resolved from the chart's own
               template expression stored in helm release storage (no guesswork).
  caaph        HelmChartProxy valuesTemplate is CAAPH's source of truth — tag patched in place.
  controller   capi-operator does NOT guard provider Deployments (and its Provider-CR
               spec.deployment override is inert under NKP's stack-operator bundle), so a direct
               Deployment image patch persists. --hold-operators scales capi-operator to 0 for
               a bulletproof hold (revert restores it).

Originals are stored IN-CLUSTER (ConfigMap speedstart-image-overrides/kommander) so revert works
from any machine. A bad image degrades gracefully: the rollout keeps the old pod serving."""
import base64, gzip, json, re, subprocess, sys, time

STATE_CM, STATE_NS = "speedstart-image-overrides", "kommander"
# component aliases -> resolution hints (CAAPH HCP name prefixes / controller deployments)
CAAPH_ALIASES = {"cilium": "cilium-", "nutanix-csi": "nutanix-csi-", "csi": "nutanix-csi-",
                 "cluster-autoscaler": "ca-", "autoscaler": "ca-", "ccm": "nutanix-ccm-",
                 "nutanix-ccm": "nutanix-ccm-", "cosi": "cosi-controller-", "konnector": "konnector-agent-"}
CONTROLLERS = {"capx": ("capx-system", "capx-controller-manager"),
               "capi": ("capi-system", "capi-controller-manager"),
               "caren": ("caren-system", "cluster-api-runtime-extensions-nutanix"),
               "kommander-operator": ("kommander", "kommander-operator"),
               "git-operator": ("git-operator-system", "git-operator-controller-manager")}
HOLD_OPERATORS = "--hold-operators" in sys.argv
# --pull-secret <registry>=<user>:<password> — lets the cluster pull the dev's PRIVATE image.
# Creates a docker-registry Secret in the component's namespace and attaches it to the workload's
# ServiceAccount, so the kubelet presents credentials when pulling the override image.
PULL_SECRET = None
for _i, _a in enumerate(sys.argv):
    if _a == "--pull-secret" and _i + 1 < len(sys.argv):
        _reg, _, _up = sys.argv[_i + 1].partition("=")
        _u, _, _p = _up.partition(":")
        PULL_SECRET = {"registry": _reg, "user": _u, "password": _p}


def kx(*a, inp=None, t=25):
    r = subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=%ds" % t, *a],
                       capture_output=True, text=True, input=inp)
    return r.stdout.strip(), r.returncode


def log(m): print("[OVERRIDE %s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def state_load():
    o, rc = kx("get", "cm", STATE_CM, "-n", STATE_NS, "-o", "jsonpath={.data.state}")
    return json.loads(o) if rc == 0 and o else {}


def state_save(st):
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": STATE_CM, "namespace": STATE_NS}, "data": {"state": json.dumps(st)}}
    kx("apply", "-f", "-", inp=json.dumps(cm))


# ---------- resolution ----------
def find_fluxapp(name):
    o, rc = kx("get", "helmrelease", name, "-n", "kommander", "-o", "jsonpath={.metadata.name}")
    return rc == 0 and o == name


def find_caaph(name):
    pref = CAAPH_ALIASES.get(name, name + "-")
    o, _ = kx("get", "helmchartproxy", "-n", "kommander", "-o",
              "jsonpath={range .items[*]}{.metadata.name} {end}")
    return next((h for h in o.split() if h.startswith(pref)), None)


def resolve(name):
    if name in CONTROLLERS:
        return ("controller",) + CONTROLLERS[name]
    if find_fluxapp(name):
        return ("flux-app", "kommander", name)
    h = find_caaph(name)
    if h:
        return ("caaph", "kommander", h)
    return (None, None, None)


# ---------- flux-app: values-key discovery from the chart's own template ----------
def flux_image_key(release_ns, release):
    """Return (values_path_for_tag, current_repo, current_tag) from helm release storage."""
    o, _ = kx("get", "secret", "-n", release_ns, "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}")
    secs = sorted((s for s in o.splitlines() if s.startswith("sh.helm.release.v1.%s.v" % release)),
                  key=lambda n: int(n.rsplit(".v", 1)[-1]))   # NUMERIC: lexicographic puts v9 after v10
    if not secs:
        return None, None, None
    raw, _ = kx("get", "secret", secs[-1], "-n", release_ns, "-o", "jsonpath={.data.release}", t=40)
    rel = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(raw))))
    # deployed image gives us truth to match template expressions against
    dep_img = None
    for l in rel["manifest"].splitlines():
        m = re.search(r'^\s+image:\s*"?([^"\s]+)"?\s*$', l)
        if m and ":" in m.group(1) and "{{" not in m.group(1):
            dep_img = m.group(1); break
    vals = rel["chart"]["values"]
    def get(path):
        cur = vals
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur: return None
            cur = cur[p]
        return cur
    # scan templates for image: "{{ .Values.a.b }}:{{ .Values.c.d }}" style exprs; pick the one
    # whose current values render the deployed image
    for t in rel["chart"]["templates"]:
        src = base64.b64decode(t["data"]).decode(errors="replace")
        for l in src.splitlines():
            if "image:" not in l or "{{" not in l:
                continue
            refs = re.findall(r"\.Values\.([A-Za-z0-9_.]+)", l)
            if len(refs) < 2:
                continue
            repo_p, tag_p = refs[0], refs[-1]
            repo_v, tag_v = get(repo_p), get(tag_p)
            # match on the REPOSITORY only — the deployed TAG may already be an override or a
            # rollback remnant; requiring defaults-tag equality broke after upgrade/rollback cycles
            if repo_v and isinstance(tag_v, str) and dep_img and \
               dep_img.rsplit(":", 1)[0].endswith(repo_v.split("/")[-1]):
                return tag_p, repo_v, tag_v
    return None, None, dep_img


def yaml_nest(path, value):
    keys = path.split(".")
    out, pad = "", ""
    for k in keys[:-1]:
        out += "%s%s:\n" % (pad, k); pad += "  "
    out += "%s%s: %s\n" % (pad, keys[-1], value)
    return out

def yaml_nest_merge(*snippets):
    # Concatenating yaml_nest() strings produces DUPLICATE top-level keys; on parse the last
    # doc wins and earlier keys are silently dropped (bug found live: image.repository override
    # erased image.tag -> pod pulled <newrepo>:<oldtag> = ImagePullBackOff). Deep-merge instead.
    import yaml as _y
    merged = {}
    def deep(dst, src):
        for k, v in (src or {}).items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict): deep(dst[k], v)
            else: dst[k] = v
    for s in snippets:
        deep(merged, _y.safe_load(s) or {})
    return _y.safe_dump(merged, default_flow_style=False)


# ---------- multi-image support ----------
def walk_yaml_paths(text):
    """Indentation-aware walk of a values(Template) — returns {path: (line_idx, indent)} for every
    mapping key. Tolerates comments; template-free YAML only (verified: HCP templates here are plain)."""
    out, stack = {}, []   # stack of (indent, key)
    for i, l in enumerate(text.splitlines()):
        st = l.strip()
        if not st or st.startswith("#"):
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_.-]+):", l)
        if not m:
            continue
        ind = len(m.group(1))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, m.group(2)))
        out[".".join(k for _, k in stack)] = (i, ind)
    return out


def caaph_image_blocks(vt):
    """Sub-image anchors in a valuesTemplate: every `image:` mapping block, named by parent path
    ('' -> 'default'). cilium: default/operator/hubble.relay/certgen/envoy."""
    paths = walk_yaml_paths(vt)
    blocks = {}
    for p, (idx, ind) in paths.items():
        if p == "image" or p.endswith(".image"):
            name = p[:-len(".image")] if p.endswith(".image") else "default"
            blocks[name] = (p, idx, ind)
    return blocks


def caaph_patch_block(vt, block_path, tag, repo=None):
    """Insert-or-replace tag: (and repository:) as children of the given image: block."""
    lines = vt.splitlines()
    paths = walk_yaml_paths(vt)
    idx, ind = paths[block_path]
    child_ind = None
    for j in range(idx + 1, len(lines)):
        m = re.match(r"^(\s*)\S", lines[j])
        if not m:
            continue
        ci = len(m.group(1))
        if ci <= ind:
            break
        child_ind = ci if child_ind is None else child_ind
    child_ind = child_ind if child_ind is not None else ind + 2
    pad = " " * child_ind
    inserts = [pad + "tag: " + tag] + ([pad + "repository: " + repo] if repo else [])
    # replace existing tag:/repository: children in the block, else insert right after the anchor
    end = idx + 1
    while end < len(lines):
        m = re.match(r"^(\s*)\S", lines[end])
        if m and len(m.group(1)) <= ind:
            break
        end += 1
    block = lines[idx + 1:end]
    block = [l for l in block if not re.match(r"^\s*(tag|repository):", l)]
    return "\n".join(lines[:idx + 1] + inserts + block + lines[end:])


def flux_image_paths(release_ns, release):
    """All image-bearing value paths in a flux app's chart defaults: dicts with (repository|name)+tag."""
    o, _ = kx("get", "secret", "-n", release_ns, "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}")
    secs = sorted((x for x in o.splitlines() if x.startswith("sh.helm.release.v1.%s.v" % release)),
                  key=lambda n: int(n.rsplit(".v", 1)[-1]))
    if not secs:
        return {}
    raw, _ = kx("get", "secret", secs[-1], "-n", release_ns, "-o", "jsonpath={.data.release}", t=40)
    rel = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(raw))))
    found = {}
    def walk(d, p=""):
        if not isinstance(d, dict):
            return
        if "tag" in d and ("repository" in d or "name" in d):
            found[p or "default"] = (p, d.get("repository") or d.get("name"), d.get("tag"))
        for k, v in d.items():
            walk(v, (p + "." + k).lstrip("."))
    walk(rel["chart"]["values"])
    return found


def ensure_pull_secret(ns, sa_names, st_entry):
    """Create the registry secret in ns and attach it to the given ServiceAccounts."""
    if not PULL_SECRET:
        return
    import base64 as _b
    auth = _b.b64encode(("%s:%s" % (PULL_SECRET["user"], PULL_SECRET["password"])).encode()).decode()
    dockercfg = json.dumps({"auths": {PULL_SECRET["registry"]: {
        "username": PULL_SECRET["user"], "password": PULL_SECRET["password"], "auth": auth}}})
    sec = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
           "metadata": {"name": "speedstart-dev-pull-secret", "namespace": ns},
           "data": {".dockerconfigjson": _b.b64encode(dockercfg.encode()).decode()}}
    kx("apply", "-f", "-", inp=json.dumps(sec))
    patched = []
    for sa in sa_names:
        if not sa:
            sa = "default"
        cur, rc = kx("get", "sa", sa, "-n", ns, "-o", "jsonpath={.imagePullSecrets[*].name}")
        if rc != 0:
            continue
        if "speedstart-dev-pull-secret" not in cur.split():
            kx("patch", "sa", sa, "-n", ns, "--type=json", "-p",
               json.dumps([{"op": "add", "path": "/imagePullSecrets" if not cur else "/imagePullSecrets/-",
                            "value": [{"name": "speedstart-dev-pull-secret"}] if not cur else {"name": "speedstart-dev-pull-secret"}}]))
        patched.append(sa)
    if patched:
        st_entry["pull_secret"] = {"ns": ns, "sas": patched}
        log("  pull-secret attached in %s to SA(s): %s" % (ns, ",".join(patched)))


def remove_pull_secret(e):
    ps = e.get("pull_secret")
    if not ps:
        return
    for sa in ps["sas"]:
        cur, rc = kx("get", "sa", sa, "-n", ps["ns"], "-o", "json")
        if rc != 0:
            continue
        obj = json.loads(cur)
        lst = [x for x in obj.get("imagePullSecrets") or [] if x.get("name") != "speedstart-dev-pull-secret"]
        kx("patch", "sa", sa, "-n", ps["ns"], "--type=merge", "-p", json.dumps({"imagePullSecrets": lst or None}))
    kx("delete", "secret", "speedstart-dev-pull-secret", "-n", ps["ns"], "--ignore-not-found=true")
    log("  pull-secret detached + removed (%s)" % ps["ns"])


# ---------- apply/revert per strategy ----------
def apply_one(name, image, st):
    name, _, sub = name.partition("@")
    strat, ns, target = resolve(name)
    if not strat:
        log("SKIP %s: no HelmRelease/HCP/controller match" % name); return False
    repo, _, tag = image.rpartition(":") if ":" in image else ("", "", image)
    if strat == "flux-app":
        if sub:
            paths = flux_image_paths(ns, "kommander-%s" % name) or flux_image_paths(ns, name)
            hit = paths.get(sub) or next((v for k, v in paths.items() if k.endswith(sub)), None)
            if not hit:
                log("SKIP %s@%s: sub-images are %s (see: images %s)" % (name, sub, sorted(paths), name)); return False
            key, cur_repo, cur_tag = (hit[0] + ".tag").lstrip("."), hit[1], hit[2]
        else:
            key, cur_repo, cur_tag = flux_image_key(ns, "kommander-%s" % name) or (None, None, None)
            if not key:
                key, cur_repo, cur_tag = flux_image_key(ns, name)
        if not key:
            log("SKIP %s: could not resolve image values-key from chart template" % name); return False
        parts = [yaml_nest(key, tag)]
        if repo:  # full image given -> also override the repository (sibling key)
            parts.append(yaml_nest(key.rsplit(".", 1)[0] + "." + ("repository" if "tag" in key else "name"), repo))
        own_vals = yaml_nest_merge(*parts)   # THIS override's complete snippet (tag AND repo)
        skey = name + ("@" + sub if sub else "")
        # one <app>-overrides CM per app: merge with any other active sub-overrides of the same app
        for k2, e2 in st.items():
            if e2.get("strategy") == "flux-app" and e2.get("app") == name and k2 != skey:
                parts.append(e2["vals"])
        vals = yaml_nest_merge(*parts)
        cm = {"apiVersion": "v1", "kind": "ConfigMap",
              "metadata": {"name": "%s-overrides" % name, "namespace": ns}, "data": {"values.yaml": vals}}
        kx("apply", "-f", "-", inp=json.dumps(cm))
        # helm-controller does not watch valuesFrom CMs — force the re-render now
        kx("annotate", "hr", name, "-n", ns,
           "reconcile.fluxcd.io/requestedAt=%d" % time.time(), "--overwrite")
        # store the FULL snippet: a later sibling apply rebuilds the CM from these — storing
        # only the tag nest silently dropped earlier repository overrides (found live: the CM
        # ended with mesosphere/<img>:dev-tag = nonexistent image, HR wedged mid-upgrade)
        st[skey] = {"strategy": "flux-app", "ns": ns, "cm": "%s-overrides" % name, "app": name,
                    "orig_tag": cur_tag, "vals": own_vals}
        # SA lookup: patch the release's workload SAs after the helm re-render (namespace default
        # covers charts that don't set a dedicated SA)
        ensure_pull_secret(ns, ["default"], st[skey])
        log("%s [flux-app] -> %s (key %s; ConfigMap %s-overrides)" % (skey, image, key, name))
    elif strat == "caaph":
        vt, _ = kx("get", "helmchartproxy", target, "-n", ns, "-o", "jsonpath={.spec.valuesTemplate}")
        blocks = caaph_image_blocks(vt)
        if not blocks:
            log("SKIP %s: HCP %s has no image: blocks" % (name, target)); return False
        if sub:
            if sub not in blocks:
                log("SKIP %s@%s: sub-images are %s (see: images %s)" % (name, sub, sorted(blocks), name)); return False
            pick = sub
        elif len(blocks) == 1:
            pick = next(iter(blocks))
        else:
            log("SKIP %s: MULTI-IMAGE chart — pick one of %s (e.g. %s@%s=%s)"
                % (name, sorted(blocks), name, sorted(blocks)[0], tag)); return False
        new_vt = caaph_patch_block(vt, blocks[pick][0], tag, repo or None)
        kx("patch", "helmchartproxy", target, "-n", ns, "--type=merge",
           "-p", json.dumps({"spec": {"valuesTemplate": new_vt}}))
        key = name + ("@" + pick if sub else "")
        st[key] = {"strategy": "caaph", "ns": ns, "hcp": target, "orig_vt": vt}
        ensure_pull_secret("kube-system", ["default"], st[key])
        log("%s [caaph] -> %s at block %r (HCP %s; exact-revert state saved)" % (key, image, pick, target))
    else:  # controller
        cur, _ = kx("get", "deploy", target, "-n", ns, "-o",
                    "jsonpath={.spec.template.spec.containers[0].image}")
        cname, _ = kx("get", "deploy", target, "-n", ns, "-o",
                      "jsonpath={.spec.template.spec.containers[0].name}")
        new_img = image if repo else cur.rsplit(":", 1)[0] + ":" + tag
        if HOLD_OPERATORS:
            kx("scale", "deploy", "cluster-api-operator", "-n", "capi-operator-system", "--replicas=0")
        kx("set", "image", "deploy/%s" % target, "-n", ns, "%s=%s" % (cname, new_img))
        sa, _ = kx("get", "deploy", target, "-n", ns, "-o",
                   "jsonpath={.spec.template.spec.serviceAccountName}")
        st[name] = {"strategy": "controller", "ns": ns, "deploy": target, "container": cname,
                    "orig_image": cur, "held_operators": HOLD_OPERATORS}
        ensure_pull_secret(ns, [sa or "default"], st[name])
        log("%s [controller] -> %s (deploy %s/%s%s)" %
            (name, new_img, ns, target, ", capi-operator held at 0" if HOLD_OPERATORS else ""))
    return True


def revert_one(name, e, _st_view=None):
    _st_view = _st_view or {}
    remove_pull_secret(e)
    if e["strategy"] == "flux-app":
        app = e.get("app", name)
        remaining = [e2 for k2, e2 in _st_view.items()
                     if e2.get("strategy") == "flux-app" and e2.get("app") == app and e2 is not e]
        if remaining:   # other sub-overrides of this app stay active: rewrite CM without ours
            vals = yaml_nest_merge(*[e2["vals"] for e2 in remaining])
            cm = {"apiVersion": "v1", "kind": "ConfigMap",
                  "metadata": {"name": e["cm"], "namespace": e["ns"]}, "data": {"values.yaml": vals}}
            kx("apply", "-f", "-", inp=json.dumps(cm))
        else:
            kx("delete", "cm", e["cm"], "-n", e["ns"], "--ignore-not-found=true")
        name = app
        # a broken override image leaves helm-controller mid-remediation; a reconcile nudge makes
        # the rollback-to-defaults land in seconds instead of minutes (measured on reloader)
        kx("annotate", "helmrelease", name, "-n", e["ns"],
           "reconcile.fluxcd.io/requestedAt=%d" % int(time.time()), "--overwrite")
    elif e["strategy"] == "caaph":
        if "orig_vt" in e:   # exact restore (multi-image-safe)
            kx("patch", "helmchartproxy", e["hcp"], "-n", e["ns"], "--type=merge",
               "-p", json.dumps({"spec": {"valuesTemplate": e["orig_vt"]}}))
        else:                # legacy tag-only state
            vt, _ = kx("get", "helmchartproxy", e["hcp"], "-n", e["ns"], "-o", "jsonpath={.spec.valuesTemplate}")
            new_vt = re.sub(r"^(\s*)tag:\s*\S+\s*$", r"\g<1>tag: %s" % e["orig_tag"], vt, count=1, flags=re.M)
            kx("patch", "helmchartproxy", e["hcp"], "-n", e["ns"], "--type=merge",
               "-p", json.dumps({"spec": {"valuesTemplate": new_vt}}))
    else:
        kx("set", "image", "deploy/%s" % e["deploy"], "-n", e["ns"], "%s=%s" % (e["container"], e["orig_image"]))
        if e.get("held_operators"):
            kx("scale", "deploy", "cluster-api-operator", "-n", "capi-operator-system", "--replicas=1")
    log("%s reverted (%s)" % (name, e["strategy"]))


def main():
    global KC
    args, _skip = [], False
    for _i, _a in enumerate(sys.argv[1:], 1):
        if _skip:
            _skip = False; continue
        if _a == "--hold-operators":
            continue
        if _a == "--pull-secret":
            _skip = True; continue
        args.append(_a)
    if len(args) < 2:
        print(__doc__); sys.exit(2)
    KC, cmd = args[0], args[1]
    st = state_load()
    if cmd == "images":
        for name in args[2:]:
            strat, ns, target = resolve(name)
            print("%s [%s]" % (name, strat or "UNRESOLVED"))
            if strat == "caaph":
                vt, _ = kx("get", "helmchartproxy", target, "-n", ns, "-o", "jsonpath={.spec.valuesTemplate}")
                for nm, (p, _i, _d) in sorted(caaph_image_blocks(vt).items()):
                    print("  @%s  (values path: %s)" % (nm, p))
            elif strat == "flux-app":
                paths = flux_image_paths(ns, "kommander-%s" % name) or flux_image_paths(ns, name)
                for nm, (p, r, t) in sorted(paths.items()):
                    print("  @%s  %s:%s" % (nm, r, t))
            elif strat == "controller":
                img, _ = kx("get", "deploy", target, "-n", ns, "-o",
                            "jsonpath={.spec.template.spec.containers[0].image}")
                print("  (single image) %s" % img)
        return
    if cmd == "status":
        print(json.dumps(st, indent=1) if st else "no active overrides")
        return
    if cmd == "revert":
        names = args[2:] or list(st)
        for n in names:
            if n in st:
                e = st.pop(n)
                revert_one(n, e, st)
            else:
                log("no recorded override for %s" % n)
        state_save(st)
        return
    if cmd != "apply":
        print(__doc__); sys.exit(2)
    failed = 0
    for spec in args[2:]:
        name, _, image = spec.partition("=")
        if not image:
            log("SKIP malformed %r (want component=image-or-tag)" % spec); failed += 1; continue
        if not apply_one(name, image, st):
            failed += 1
    state_save(st)
    log("state saved in-cluster (%s/%s); revert with: override_image.py <kc> revert" % (STATE_NS, STATE_CM))
    if failed:
        # a SKIP is a NO-OP, not a success — callers (orchestrator) must see it fail
        log("%d override(s) did NOT apply" % failed); sys.exit(1)


if __name__ == "__main__":
    main()
