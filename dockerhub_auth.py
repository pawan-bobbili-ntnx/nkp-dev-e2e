#!/usr/bin/env python3
# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Give flux authenticated Docker Hub pulls on a cluster.

WHY THIS EXISTS
    The `kommander` and `kommander-appmanagement` charts come from
    oci://docker.io/mesosphere/... on both the 2.17 and 2.18 lines, while
    every other chart comes from ghcr.io. A shared lab egress IP therefore
    burns Docker Hub's anonymous allowance (100 pulls / 6h / IP) and those
    two charts - and only those two - fail with TOOMANYREQUESTS. The install
    then dies as an opaque "failed to wait for HelmRelease".

WHY IT IS NOT A ONE-LINE PATCH
    A hand-added spec.secretRef is STRIPPED again within ~10 seconds. Two
    different controllers re-apply these objects, and BOTH must be quiet:

      * the flux Kustomization that renders them (suspend/resume - done
        below);
      * **KommanderCoreInstaller**, i.e. the kommander-operator itself.
        Confirmed 2026-08-30 from .metadata.managedFields:
            manager=KommanderCoreInstaller  op=Apply
            manager=kustomize-controller    op=Apply
        Suspending only the Kustomization is NOT enough - measured live:
            t=10s secretRef='dockerhub-auth' ready=True  stored artifact ...
            t=20s secretRef=''               ready=False TOOMANYREQUESTS
        Pass --stop-operator to scale kommander-operator to 0 across the
        patch. Do that ONLY when no install/upgrade is in flight: the
        operator is required to reconcile KommanderCore, so stopping it
        mid-install trades one failure for another.

WHAT "WORKING" LOOKS LIKE, AND THE TRAP
    Authentication itself works instantly - the artifact is fetched and
    stored on the first authenticated reconcile. But .status.artifact
    persisting is NOT sufficient for the install: the CLI waits on the
    HelmRelease, whose Ready gate follows the OCIRepository's Ready, and
    that re-checks the digest on every reconcile. So once secretRef is
    stripped, Ready flaps back to False even though the chart content is
    present and the release already installed (Released=True). Ready - not
    ArtifactInStorage - is therefore the honest success criterion.

WHAT IT DOES NOT FIX
    `--registry-mirror-url/-username/-password` configures containerd on the
    NODES. Flux's source-controller pulls charts itself over HTTPS from
    inside a pod and never touches containerd, so the mirror flags cannot
    authenticate chart pulls. This is the only thing that does.

USAGE
    ./dockerhub_auth.py <kubeconfig> [--user U --password P]

    Credentials come from --user/--password, else DOCKERHUB_USER /
    DOCKERHUB_PASSWORD, else the macOS keychain entry for
    https://index.docker.io/v1/. The secret is applied via stdin so it never
    appears in argv or in a log.

    Idempotent: safe to re-run, and a no-op when nothing is rate-limited.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time

SECRET = "dockerhub-auth"
RATE_TELLS = ("toomanyrequests", "rate limit")


def kubectl(kubeconfig: str, *args: str, stdin: str | None = None) -> tuple[int, str]:
    p = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args],
                       input=stdin, capture_output=True, text=True)
    return p.returncode, (p.stdout or p.stderr)


def kget(kubeconfig: str, *args: str) -> dict:
    rc, out = kubectl(kubeconfig, *args, "-o", "json")
    try:
        return json.loads(out) if rc == 0 and out.strip() else {}
    except ValueError:
        return {}


def credentials(args) -> tuple[str, str]:
    import os
    if args.user and args.password:
        return args.user, args.password
    u, p = os.environ.get("DOCKERHUB_USER"), os.environ.get("DOCKERHUB_PASSWORD")
    if u and p:
        return u, p
    r = subprocess.run(["docker-credential-osxkeychain", "get"],
                       input="https://index.docker.io/v1/", capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("no credentials: pass --user/--password, set DOCKERHUB_USER/"
                 "DOCKERHUB_PASSWORD, or log in with `docker login`")
    c = json.loads(r.stdout)
    return c["Username"], c["Secret"]


def rate_limited(kubeconfig: str) -> list[tuple[str, str, str]]:
    """(name, namespace, owning-kustomization) for each throttled OCIRepository."""
    out = []
    for item in kget(kubeconfig, "get", "ocirepository", "-A").get("items", []):
        for cond in item.get("status", {}).get("conditions", []):
            msg = cond.get("message", "").lower()
            failed = ((cond.get("type") == "Ready" and cond.get("status") == "False")
                      or (cond.get("type") == "FetchFailed" and cond.get("status") == "True"))
            if failed and any(t in msg for t in RATE_TELLS):
                meta = item["metadata"]
                out.append((meta["name"], meta["namespace"],
                            meta.get("labels", {}).get(
                                "kustomize.toolkit.fluxcd.io/name", "")))
                break
    return out


def ensure_secret(kubeconfig: str, namespace: str, user: str, password: str) -> None:
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    cfg = {"auths": {h: {"username": user, "password": password, "auth": auth}
                     for h in ("https://index.docker.io/v1/", "index.docker.io", "docker.io")}}
    manifest = {
        "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": SECRET, "namespace": namespace},
        "data": {".dockerconfigjson": base64.b64encode(json.dumps(cfg).encode()).decode()},
    }
    rc, out = kubectl(kubeconfig, "apply", "-f", "-", stdin=json.dumps(manifest))
    print(f"  secret {namespace}/{SECRET}: {out.strip()[:80] if rc == 0 else 'FAILED ' + out[:120]}")


def docker_io_repos(kubeconfig: str) -> list[tuple[str, str, bool]]:
    """(name, namespace, has_secretRef) for every OCIRepository on docker.io."""
    out = []
    for item in kget(kubeconfig, "get", "ocirepository", "-A").get("items", []):
        if "docker.io" in item.get("spec", {}).get("url", ""):
            out.append((item["metadata"]["name"], item["metadata"]["namespace"],
                        bool(item.get("spec", {}).get("secretRef", {}).get("name"))))
    return out


def watch(args) -> int:
    """Keep secretRef applied to every docker.io OCIRepository, forever.

    Not elegant, but it is the only thing that holds: the operator owns these
    objects and re-applies them without secretRef, so the reference has to be
    re-asserted rather than set once. Each re-apply triggers an authenticated
    reconcile, which stores the artifact and flips Ready True; the strip that
    follows only flips it back until the next pass.
    """
    user, password = credentials(args)
    print(f"watch: re-applying secretRef every {args.watch}s "
          f"(Docker Hub account {user}); Ctrl-C to stop", flush=True)
    seen_ns: set[str] = set()
    while True:
        try:
            repos = docker_io_repos(args.kubeconfig)
            for name, ns, has in repos:
                if ns not in seen_ns:
                    ensure_secret(args.kubeconfig, ns, user, password)
                    seen_ns.add(ns)
                if not has:
                    kubectl(args.kubeconfig, "patch", "ocirepository", name, "-n", ns,
                            "--type=merge",
                            "-p", json.dumps({"spec": {"secretRef": {"name": SECRET}}}))
                    print(f"  re-applied secretRef to {ns}/{name}", flush=True)
        except Exception as exc:            # a transient API error must not end the watch
            print(f"  watch error (continuing): {exc}", flush=True)
        time.sleep(args.watch)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kubeconfig")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--timeout", type=int, default=180, help="seconds to wait for Ready")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                    help="run forever, re-applying secretRef every SECONDS. Defeats "
                         "KommanderCoreInstaller, which strips it ~10s after each apply "
                         "and cannot be held down while an install needs it.")
    ap.add_argument("--stop-operator", action="store_true",
                    help="also scale kommander-operator to 0 across the patch - it is the "
                         "OTHER writer that strips secretRef. Only safe when no install is running.")
    args = ap.parse_args()

    if args.watch:
        return watch(args)

    throttled = rate_limited(args.kubeconfig)
    if not throttled:
        print("no rate-limited OCIRepositories - nothing to do")
        return 0
    print(f"rate-limited: {', '.join(n for n, _, _ in throttled)}")

    user, password = credentials(args)
    print(f"using Docker Hub account: {user} (secret not shown)")

    namespaces = {ns for _, ns, _ in throttled}
    for ns in namespaces:
        ensure_secret(args.kubeconfig, ns, user, password)

    # Suspend owners FIRST: a Kustomization re-applies desired state and
    # strips spec.secretRef, which is why the naive patch silently fails.
    owners = {(k, ns) for _, ns, k in throttled if k}
    for k, ns in owners:
        kubectl(args.kubeconfig, "patch", "kustomization", k, "-n", ns,
                "--type=merge", "-p", '{"spec":{"suspend":true}}')
        print(f"  suspended kustomization {ns}/{k}")

    scaled = []
    if args.stop_operator:
        for ns in namespaces:
            rc, _ = kubectl(args.kubeconfig, "scale", "deploy/kommander-operator",
                            "-n", ns, "--replicas=0")
            if rc == 0:
                scaled.append(ns)
                print(f"  scaled kommander-operator to 0 in {ns} (it strips secretRef)")

    try:
        for name, ns, _ in throttled:
            kubectl(args.kubeconfig, "patch", "ocirepository", name, "-n", ns, "--type=merge",
                    "-p", json.dumps({"spec": {"secretRef": {"name": SECRET}}}))
            kubectl(args.kubeconfig, "annotate", "ocirepository", name, "-n", ns,
                    f"reconcile.fluxcd.io/requestedAt={int(time.time())}", "--overwrite")
            print(f"  patched + nudged {ns}/{name}")

        deadline = time.time() + args.timeout
        pending = {(n, ns) for n, ns, _ in throttled}
        while pending and time.time() < deadline:
            time.sleep(10)
            for name, ns in sorted(pending):
                obj = kget(args.kubeconfig, "get", "ocirepository", name, "-n", ns)
                ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                            for c in obj.get("status", {}).get("conditions", []))
                if ready:
                    print(f"  READY {ns}/{name}")
                    pending.discard((name, ns))
    finally:
        # Always resume, even if the wait failed: leaving flux suspended is a
        # far worse state to walk away from than an unauthenticated pull.
        for k, ns in owners:
            kubectl(args.kubeconfig, "patch", "kustomization", k, "-n", ns,
                    "--type=merge", "-p", '{"spec":{"suspend":false}}')
            print(f"  resumed kustomization {ns}/{k}")
        for ns in scaled:
            kubectl(args.kubeconfig, "scale", "deploy/kommander-operator",
                    "-n", ns, "--replicas=1")
            print(f"  restored kommander-operator in {ns}")

    if pending:
        print(f"STILL FAILING: {sorted(pending)}")
        return 1
    print("all previously rate-limited OCIRepositories are Ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
