#!/usr/bin/env python3
"""GIT RE-HYDRATE — deterministic git-operator repo bring-up from an integrity-checked bundle.

Replaces the fragile "restore the git VG and hope its filesystem/git-objects survived" model:
the restore only has to provide a MOUNTABLE volume — the actual repo content is rebuilt in-pod
from a `git bundle` captured at freeze, with the ingress LB rewritten to the claim's LB. Because
a corrupt git repo silently WEDGES flux (kustomizations can't reconcile -> stale template-LB CMs
stick, SSO serves the wrong issuer, apps never converge), this makes the git source deterministic
regardless of restore integrity.

Two hard-won invariants (bc20/bc21):
  1. MOUNT-GATED — wait for the volume to be genuinely mounted+writable before touching anything.
     Pod "Ready" is not enough: storage can still be wedged by a Wrinkle-2 foreign VG attachment,
     and rebuilding against an unmounted pod silently leaves stale refs.
  2. FLUX ALWAYS RESUMED — every exit path goes through finally: resume_flux(). Leaving flux
     suspended is the worst outcome (the platform stops reconciling, silently).

Prune-safe: flux `management` GitRepository + the platform kustomizations are SUSPENDED before
the repo is touched and resumed once the hydrated (correct-LB) content is in place — an empty or
half-built repo reconciled with prune=true would delete the whole platform.

  git_hydrate.py <kubeconfig> <source_lb> <claim_lb> <bundle_dir>

bundle_dir must hold kommander.bundle (+ optional admin.bundle)."""
import subprocess, sys, time, os

KC, SRC_LB, LB, BDIR = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
GITNS, POD, GC = "git-operator-system", "git-operator-git-0", "git-server-fcgi"
KUSTS = ["apps-kommander", "dex", "dex-k8s-authenticator", "traefik-forward-auth-mgmt", "kube-oidc-proxy"]

def log(m): print("[HYDRATE %s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)
def kx(*a, to=30, inp=None):
    return subprocess.run(["kubectl", "--kubeconfig", KC, "--request-timeout=%ds" % to, *a],
                          capture_output=True, text=True, input=inp)
def pexec(script, to=150):
    return kx("exec", "-n", GITNS, POD, "-c", GC, "--", "sh", "-c", script, to=to)

def resume_flux():
    kx("patch", "gitrepository", "management", "-n", "kommander-flux", "--type=merge",
       "-p", '{"spec":{"suspend":false}}')
    for k in KUSTS:
        kx("patch", "kustomization", k, "-n", "kommander", "--type=merge", "-p", '{"spec":{"suspend":false}}')
    kx("annotate", "gitrepository", "management", "-n", "kommander-flux",
       "reconcile.fluxcd.io/requestedAt=%d" % int(time.time()), "--overwrite")

def hydrate_repo(repo_path, bundle, rewrite):
    """Rebuild a bare repo from its bundle: clone -> LB rewrite -> commit -> re-init bare -> fetch
    -> fsck gate. In-pod git needs HOME=/tmp and -c safe.directory=* (uid 65532 has no writable
    HOME; `git config --global` fails with exit 255)."""
    rw = ('grep -rl "%s" . 2>/dev/null | grep -v "^./.git/" | '
          'while read f; do sed -i "s/%s/%s/g" "$f"; done; '
          % (SRC_LB, SRC_LB.replace(".", "\\."), LB)) if rewrite else "true\n"
    script = r"""export HOME=/tmp
G="git -c safe.directory=* -c user.email=claim@nkp -c user.name=claim"
R=%s ; B=/tmp/%s
[ -f "$B" ] || { echo "NOBUNDLE"; exit 0; }
cd /tmp && rm -rf wt_%s
$G clone -q -b main "$B" wt_%s 2>/dev/null || { echo "CLONEFAIL"; exit 3; }
cd wt_%s
%s
$G add -A >/dev/null 2>&1; $G commit -q -m "claim: ingress LB -> %s" >/dev/null 2>&1 || true
rm -rf $R/objects $R/refs $R/packed-refs
$G --git-dir=$R init -q --bare
$G --git-dir=$R fetch -q /tmp/wt_%s/.git "+refs/heads/main:refs/heads/main"
printf "ref: refs/heads/main\n" > $R/HEAD
$G --git-dir=$R fsck --no-dangling >/tmp/fsck_%s 2>&1 && echo "FSCK_CLEAN" || { echo "FSCK_FAIL"; cat /tmp/fsck_%s; }
$G --git-dir=$R log --oneline -1
""" % (repo_path, bundle, bundle, bundle, bundle, rw, LB, bundle, bundle, bundle)
    return pexec(script)

# ---- 0. MOUNT GATE — before touching flux, prove the volume is really mounted + writable -----
mounted = False
for i in range(120):                      # up to ~20 min: sweep + CSI attach can be slow
    probe = pexec('test -d /volumes/git/kommander/kommander.git && '
                  'touch /volumes/git/.hydrate-probe 2>/dev/null && '
                  'rm -f /volumes/git/.hydrate-probe && echo MOUNTED', to=30)
    if "MOUNTED" in probe.stdout:
        mounted = True; break
    if i % 6 == 5: log("  waiting for git volume mount (%ds elapsed)" % (i * 10))
    time.sleep(10)
if not mounted:
    log("FATAL: git volume never mounted — flux left UNTOUCHED (never suspended). "
        "Check for a Wrinkle-2 foreign VG attachment blocking CSI ControllerPublish.")
    sys.exit(5)
log("git volume mounted + writable")

# The source bundle must exist LOCALLY. Without this check a stale copy left in the pod's /tmp by
# an earlier run would be reused silently — the run would look clean while hydrating from an
# unknown/mismatched artifact.
if not os.path.exists(os.path.join(BDIR, "kommander.bundle")):
    log("FATAL: kommander.bundle not found in %s — flux left UNTOUCHED (never suspended)" % BDIR)
    sys.exit(6)

# ---- 0b. SKIP-IF-ALREADY-CORRECT (tail postmortem, bcd1 vs bcd3, 2026-07-26) ----------------
# boc3cp's in-window `git-at-rest` step normally ALREADY rewrote the ingress LB in this repo before
# first boot. Rebuilding from the bundle then throws that history away and writes a brand-new
# commit, so flux sees a NEW revision and re-reconciles EVERYTHING — dex helm-upgrades a second
# time and the whole SSO chain (dex -> dex-k8s-authenticator / kube-oidc-proxy /
# traefik-forward-auth) rolls again. Measured: those 4 HelmReleases are the ONLY ones that finish
# after window-close, and they are the entire post-window tail (bcd1 273-306s vs bcd3 607-889s).
# bcd3 spent 285s with staleLB-refs=3 purely re-rendering after hydrate's redundant rebuild.
# So: if the repo is INTACT and ALREADY carries the claim LB with no template LB left, changing
# nothing is strictly better than rebuilding. Corruption repair (bc17) still triggers a rebuild.
probe = pexec(
    'export HOME=/tmp; G="git -c safe.directory=*"; R=/volumes/git/kommander/kommander.git; '
    '$G --git-dir=$R fsck --no-dangling >/dev/null 2>&1 && echo FSCK_OK || echo FSCK_BAD; '
    'echo "SRC:$($G --git-dir=$R grep -c "%s" HEAD 2>/dev/null | wc -l)"; '
    'echo "DST:$($G --git-dir=$R grep -c "%s" HEAD 2>/dev/null | wc -l)"' % (SRC_LB, LB), to=90)
_ok = "FSCK_OK" in probe.stdout
_src = _dst = -1
for line in probe.stdout.splitlines():
    if line.startswith("SRC:"):
        try: _src = int(line.split(":", 1)[1].strip())
        except Exception: pass
    if line.startswith("DST:"):
        try: _dst = int(line.split(":", 1)[1].strip())
        except Exception: pass
log("pre-check: fsck=%s files-with-src-LB(%s)=%s files-with-claim-LB(%s)=%s"
    % ("OK" if _ok else "BAD", SRC_LB, _src, LB, _dst))
if _ok and _src == 0 and _dst > 0:
    log("repo already intact AND already on the claim LB — SKIPPING rebuild "
        "(a rebuild would force a second flux reconcile and re-roll the whole SSO chain)")
    # flux was never suspended on this path; just make sure it is running and nudge it once.
    resume_flux()
    log("flux verified running + reconcile requested; git_hydrate complete (no-op)")
    sys.exit(0)
log("rebuild REQUIRED (corrupt repo or stale/absent claim LB) — proceeding")

rc = 0
try:
    # ---- 1. SUSPEND flux so an in-flight repo can't prune the platform ----------------------
    kx("patch", "gitrepository", "management", "-n", "kommander-flux", "--type=merge",
       "-p", '{"spec":{"suspend":true}}')
    for k in KUSTS:
        kx("patch", "kustomization", k, "-n", "kommander", "--type=merge", "-p", '{"spec":{"suspend":true}}')
    log("flux GitRepository + %d kustomizations suspended" % len(KUSTS))

    # ---- 2. push bundles into the pod ------------------------------------------------------
    for b in ("kommander.bundle", "admin.bundle"):
        p = os.path.join(BDIR, b)
        if os.path.exists(p):
            kx("cp", p, "%s/%s:/tmp/%s" % (GITNS, POD, b), "-c", GC, to=90)
    log("bundles copied")

    # ---- 3. rebuild each bare repo, LB-rewrite, fsck-gate ----------------------------------
    r = hydrate_repo("/volumes/git/kommander/kommander.git", "kommander.bundle", True)
    log("kommander.git: %s" % (r.stdout.strip().replace("\n", " | ") or r.stderr[-160:]))
    if "FSCK_CLEAN" not in r.stdout:
        log("ERROR: kommander.git hydration not clean — flux still resumed below"); rc = 4
    else:
        ra = hydrate_repo("/volumes/admin/admin.git", "admin.bundle", False)
        log("admin.git: %s" % (ra.stdout.strip().replace("\n", " | ")[:120] or "skipped"))
finally:
    # ---- 4. RESUME flux + reconcile — UNCONDITIONAL -----------------------------------------
    resume_flux()
    log("flux resumed + reconcile requested")
if rc: sys.exit(rc)

# ---- 5. verify: stale LB clears from rendered CMs, dex serves the claim LB -------------------
for i in range(24):
    time.sleep(15)
    cms = kx("get", "cm", "-n", "kommander", "-o", "json", to=25).stdout
    tl = cms.count(SRC_LB) + cms.count("<lb-address>")
    iss = subprocess.run(["curl", "-sk", "--max-time", "8",
                          "https://%s/dex/.well-known/openid-configuration" % LB],
                         capture_output=True, text=True).stdout
    ok = ("%s/dex" % LB) in iss
    if tl == 0 and ok:
        log("HYDRATION CONVERGED at +%ds: dex issuer=%s, no stale LB in CMs" % (i * 15, LB)); break
    if i % 4 == 3: log("  +%ds staleLB-refs=%d issuer-ok=%s" % (i * 15, tl, ok))
log("git_hydrate complete")
