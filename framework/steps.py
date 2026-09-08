# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The vocabulary a config-driven scenario is written in.

Each step is a named, parameterised action. A YAML scenario lists steps under
prepare/execute/check/cleanup and the runner executes them in order, so a new
test case needs no Python at all.

Adding a step is one decorated function here; it becomes available to every
scenario immediately, and `run_e2e.py --steps` prints the catalogue.
"""

from __future__ import annotations

import contextlib
import json
import json as _json
import os
import re
import time
from typing import Callable

from .diagnostics import collect_cluster
from .shell import run, wait_for

STEPS: dict[str, Callable] = {}
STEP_DOC: dict[str, str] = {}


def step(name: str):
    """Register a step under ``name``."""

    def wrap(fn: Callable) -> Callable:
        if name in STEPS:
            raise RuntimeError(f"duplicate step: {name}")
        STEPS[name] = fn
        STEP_DOC[name] = (fn.__doc__ or "").strip().split("\n")[0]
        return fn

    return wrap


def parse_duration(value) -> int:
    """'90s', '5m', '1h' or a bare number of seconds -> seconds."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(value))
    if not match:
        raise ValueError(f"cannot parse duration: {value!r} (use 30s, 5m, 1h)")
    amount, unit = float(match.group(1)), match.group(2) or "s"
    return int(amount * {"s": 1, "m": 60, "h": 3600}[unit])


# ---------------------------------------------------------------- lifecycle
def traditional_create(ctx, *, control_plane: int = 1, workers: int = 1,
                   timeout: str = "60m", name_suffix: str = "",
                   type: str = "management", binary_from: str = "",
                   machine_image: str = "", kubernetes_version: str = "@default",
                   worker_vcpus: int = 0, worker_memory: int = 0) -> None:
    """Create a cluster with the scenario's generated name.

    ``type: management`` (default) creates a self-managed cluster - it manages
    itself and carries kommander core. ``type: workload`` creates a cluster
    managed by the cluster the scenario is already attached to, which is how
    test clusters hang off a fast-claimed management cluster.

    ``binary_from`` names a value stored by ``resolve_baseline``: the cluster
    is then created BY that base-version CLI - old CAPI templates, old
    defaults - which is what an upgrade test needs as its starting point.
    Omitted, the cluster is created by NKP_BIN, i.e. the current changes.
    When creating with a baseline, pass ``machine_image`` (or set
    E2E_BASE_MACHINE_IMAGE) matching the base version's kubernetes;
    ``kubernetes_version: ""`` lets the base CLI use its own default.
    """
    if type not in ("management", "workload"):
        raise RuntimeError(f"create_cluster: type must be management or workload, got {type!r}")
    if not machine_image and not ctx.config.machine_image and not ctx.config.dry_run:
        # The CLI rejects a create with no image, but only after building a
        # bootstrap cluster - four minutes to reach a one-line answer.
        raise RuntimeError(
            "no node image: set machine_image: on create_cluster in the "
            "scenario (preferred) or export E2E_MACHINE_IMAGE. It must match "
            "the kubernetes version. List images in Prism Central under "
            "Compute & Storage > Images."
        )
    cli = ctx.nkp
    image_override = machine_image or None
    k8s_override = None if kubernetes_version == "@default" else kubernetes_version
    if binary_from:
        baseline = ctx.recall(binary_from) or ""
        if not baseline:
            raise RuntimeError(
                f"create_cluster: binary_from={binary_from!r} has no value - "
                "run resolve_baseline first"
            )
        if not baseline.startswith("<"):
            cli = ctx.nkp.with_binary(baseline)
        image_override = machine_image or os.environ.get("E2E_BASE_MACHINE_IMAGE") or None
        if kubernetes_version == "@default":
            # let the base CLI pick its own supported kubernetes
            k8s_override = os.environ.get("E2E_BASE_KUBERNETES_VERSION", "")
        ctx.log.info(
            f"creating with BASE CLI {baseline} "
            f"(image={image_override or '(cli default)'}, k8s={k8s_override or '(cli default)'})"
        )
    ctx.cluster_name = ctx.cluster(name_suffix)
    ctx.pc.sweep(ctx.cluster_name)
    if type == "management":
        # A self-managed create needs the fixed-name kind bootstrapper; a
        # stale one from a killed run wedges admission with an opaque
        # timeout. Nobody should have to remember this - always reset.
        reset_bootstrap(ctx)
    ctx.kubeconfig = ctx.artifacts / f"{ctx.cluster_name}.conf"
    vip, lb = ctx.addresses()
    def _create() -> None:
        cli.create_cluster(
            ctx.cluster_name,
            control_plane_replicas=control_plane,
            worker_replicas=workers,
            kubeconfig_out=ctx.kubeconfig,
            control_plane_ip=vip,
            load_balancer_range=lb,
            machine_image=image_override,
            kubernetes_version=k8s_override,
            self_managed=(type == "management"),
            worker_vcpus=worker_vcpus,
            worker_memory=worker_memory,
            timeout_minutes=max(1, parse_duration(timeout) // 60),
        )

    from .shell import CommandError
    try:
        _create()
    except CommandError as exc:
        # A GA `create cluster --self-managed` installs kommander itself, so it
        # can lose its KommanderCore watch exactly like `install kommander`
        # does - and install_platform has retried that since 2026-08-29 while
        # this path did not. Same transient, two code paths, one unprotected:
        # canonical node-upgrade failed TWICE on it (2026-08-30, at 4/14 and
        # 11/14 core apps) while the cluster went on to converge by itself.
        # Retry only that signature; anything else is a real failure and must
        # surface unchanged.
        settling = ("error watching KommanderCore", "timed out waiting for InstallSucceeded")
        if not any(t in str(exc) for t in settling):
            raise
        # WAIT - do not re-run create. The cluster exists and is converging;
        # measured three times on 2026-08-30 it went 4/14 -> 14/14 and
        # 11/14 -> 14/14 entirely on its own after the CLI gave up. Re-running
        # `nkp create cluster` against a live cluster is UNVERIFIED and this is
        # the most expensive operation in the framework, so poll the signal the
        # CLI itself was waiting for instead: KommanderCore.status.version is
        # set only on full success (SuccessAndSetVersion).
        ctx.log.warn(
            f"create lost track of the install ({str(exc)[:90]}) - the cluster "
            "keeps converging server-side; waiting for KommanderCore instead "
            "of re-creating")

        def core_installed() -> bool:
            for item in ctx.kube.json(["get", "kommandercore", "-A"]).get("items", []):
                if item.get("status", {}).get("version"):
                    return True
            return False

        wait_for(core_installed, ctx.log,
                 what="KommanderCore to report a version (install complete)",
                 timeout_s=1800, dry_run=False)
        ctx.log.info("the install finished on its own after the CLI stopped watching")
    _announce_kubeconfig(ctx, ctx.kubeconfig, type)


@step("delete_cluster")
def delete_cluster(ctx, *, sweep: bool = True) -> None:
    """Delete the cluster and (by default) sweep any VMs left behind."""
    if not ctx.cluster_name:
        return
    ctx.nkp.delete_cluster(ctx.cluster_name)
    if sweep:
        left = ctx.pc.sweep(ctx.cluster_name)
        if left:
            ctx.log.warn(f"swept {left} VM(s) the delete left behind")


def _registry_pull_failures(ctx) -> list[str]:
    """Flux sources that cannot pull, quoting the registry's own reason.

    An install failure surfaces as "failed to wait for HelmRelease X" after a
    ten-minute timeout, which reads like a broken cluster. The real cause is
    usually one layer down, in the OCIRepository that release pulls from -
    and it is worth naming, because the two charts most likely to fail
    (kommander, kommander-appmanagement) are the two that come from
    docker.io rather than ghcr.io, so they are the ones that hit Docker
    Hub's anonymous pull limit. Live-diagnosed 2026-08-29.
    """
    if ctx.config.dry_run:
        return []
    tells = ("toomanyrequests", "rate limit", "unauthorized", "denied",
             "authentication required", "forbidden")
    out = []
    try:
        items = ctx.kube.json(["get", "ocirepository", "-A"]).get("items", [])
    except Exception:
        return []
    for item in items:
        for cond in item.get("status", {}).get("conditions", []):
            msg = cond.get("message", "")
            failed = ((cond.get("type") == "Ready" and cond.get("status") == "False")
                      or (cond.get("type") == "FetchFailed" and cond.get("status") == "True"))
            if failed and any(t in msg.lower() for t in tells):
                out.append(f"{item['metadata']['name']}: {msg[:220]}")
                break
    return sorted(set(out))


def _stuck_volume_claims(ctx) -> list[str]:
    """Pending PVCs, quoting the provisioner's own refusal.

    An install that cannot get storage does not say so: the PVC sits Pending,
    the pod never schedules, Helm hits its deadline, and the CLI reports
    "Waiting for all enabled applications to be ready ... timed out" an hour
    later. Live 2026-08-30 that chain hid an EXPIRED PRISM CENTRAL
    CREDENTIAL - the CSI could no longer look up the storage container, and
    nothing in the failure text mentioned credentials or storage.
    """
    if ctx.config.dry_run:
        return []
    out = []
    try:
        pvcs = ctx.kube.json(["get", "pvc", "-A"]).get("items", [])
    except Exception:
        return []
    pending = [p for p in pvcs if p.get("status", {}).get("phase") == "Pending"]
    for p in pending:
        meta = p["metadata"]
        msg = ""
        try:
            evs = ctx.kube.json([
                "get", "events", "-n", meta["namespace"],
                "--field-selector", f"involvedObject.name={meta['name']}",
            ]).get("items", [])
            fails = [e.get("message", "") for e in evs
                     if e.get("reason") == "ProvisioningFailed"]
            msg = fails[-1] if fails else ""
        except Exception:
            pass
        out.append(f"{meta['namespace']}/{meta['name']}: {msg[:220] or 'Pending, no provisioner event'}")
    return out


@step("install_platform")
def install_platform(ctx, *, timeout: str = "45m", binary: str = "",
                     binary_from: str = "", disable_apps: list | None = None) -> None:
    """Install the NKP platform (kommander) onto the cluster.

    ``disable_apps`` switches applications off in the installation config.
    The storage and logging chain - rook-ceph and everything downstream of its
    object store - dominates install time, so a scenario that does not exercise
    it should say so rather than wait for it.

    ``binary`` installs with a different nkp CLI, which is how an upgrade
    scenario lays down an older baseline.
    """
    if binary_from and not binary:
        binary = ctx.recall(binary_from) or ""
        if binary.startswith("<") and not ctx.config.dry_run:
            raise RuntimeError(f"binary_from={binary_from!r} resolved to a dry-run placeholder")
    cli = ctx.nkp.with_binary(binary) if binary and not binary.startswith("<") else ctx.nkp
    config = None
    if disable_apps:
        config = cli.installer_config(
            ctx.artifacts / "installer-config.yaml", disable=list(disable_apps)
        )
    from .shell import CommandError
    try:
        cli.install_kommander(
            ctx.kubeconfig,
            installer_config=config,
            timeout_minutes=max(1, parse_duration(timeout) // 60),
        )
    except CommandError as exc:
        # Before assuming a transient: a registry that will not serve the
        # charts is NOT transient, and retrying costs another full timeout
        # before failing with the same unhelpful message.
        blocked = _registry_pull_failures(ctx)
        if blocked:
            # Credentials available? Then this is fixable in place: authenticate
            # flux's chart pulls and retry ONCE. dockerhub_auth.py exits
            # non-zero when it has no credentials or cannot clear the block,
            # so a failure here falls through to the explanatory error below.
            import subprocess as _sp
            import sys as _sys
            from pathlib import Path as _P
            helper = _P(__file__).resolve().parent.parent / "dockerhub_auth.py"
            if helper.exists():
                # --watch, NOT a single-shot patch. Applying secretRef once
                # does not survive: the operator (KommanderCoreInstaller)
                # re-applies the OCIRepository and strips it within ~10s, so
                # the one-shot helper reports STILL FAILING and the install
                # dies anyway (observed 2026-08-30 01:04). Holding the
                # credential in place for the whole retry is what works -
                # measured: 14 re-applies across one install.
                ctx.log.warn("registry rate-limited - holding authenticated "
                             "chart pulls in place and retrying the install")
                watch = _sp.Popen(
                    [_sys.executable, str(helper), str(ctx.kubeconfig), "--watch", "5"],
                    stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)
                try:
                    time.sleep(20)          # let the first re-apply land
                    cli.install_kommander(
                        ctx.kubeconfig,
                        installer_config=config,
                        timeout_minutes=max(1, parse_duration(timeout) // 60),
                    )
                    ctx.log.info("install succeeded with authenticated chart pulls")
                    return
                except CommandError:
                    ctx.log.warn("install still failing with authenticated pulls")
                    raise
                finally:
                    watch.terminate()
                    try:
                        watch.wait(timeout=15)
                    except Exception:
                        watch.kill()
            raise RuntimeError(
                "platform install failed because chart artifacts could not be "
                "pulled - this is a REGISTRY problem, not a cluster problem, "
                "and retrying will not fix it:\n  "
                + "\n  ".join(blocked)
                + "\n\nWHY: the kommander and kommander-appmanagement charts "
                  "come from docker.io on both the 2.17 and 2.18 lines "
                  "(everything else is ghcr.io), so a shared lab egress IP "
                  "hits Docker Hub's anonymous pull limit - 100 pulls per 6h "
                  "per IP - and every NKP install from that IP fails here.\n"
                  "REMEDIES, in order of effort:\n"
                  "  1. wait for the 6h window to roll over and re-run;\n"
                  "  2. authenticate flux's pulls: create a docker-registry "
                  "secret and add spec.secretRef to the OCIRepositories above. "
                  "Flux's source-controller pulls charts itself over HTTPS, so "
                  "this is the only thing that actually authenticates them;\n"
                  "  3. an airgapped/mirrored install (`--airgapped`) against "
                  "a seeded registry.\n"
                  "NOT a fix: E2E_REGISTRY_MIRROR_URL / --registry-mirror-url. "
                  "That configures containerd on the NODES for image pulls; it "
                  "does not touch source-controller's chart pulls (verified "
                  "2026-08-29 - `nkp install kommander` has no registry "
                  "credential option in its config or flags)."
            ) from exc
        # Storage before "transient": a stalled install is far more often
        # something that cannot be provisioned than a flaky step, and the
        # CLI's own message never says so.
        stuck = _stuck_volume_claims(ctx)
        if stuck:
            pc_dead = not ctx.pc.reachable()
            head = ("Prism Central is REJECTING the CSI's credentials, so no "
                    "volume can be provisioned. NUTANIX_USER / "
                    "NUTANIX_PASSWORD are temporary and expire - refresh them "
                    "(tam login pc-dev) and re-run.\n"
                    if pc_dead else
                    "the platform install stalled on storage that never "
                    "provisioned:\n")
            raise RuntimeError(
                head + "Pending PersistentVolumeClaim(s):\n  " + "\n  ".join(stuck)
                + ("\n\nNOTE: a cluster can outlive its credentials - earlier "
                   "PVCs in this same cluster may be Bound while later ones "
                   "fail, which makes this look like a storage-capacity "
                   "problem rather than an auth one." if pc_dead else "")
            ) from exc

        # Two live-seen transients, both idempotent on rerun:
        #  * the installer redeploys kommander-operator then patches
        #    KommanderCore - the old operator's webhook can reject it
        #    mid-rollout;
        #  * "error watching KommanderCore" - the CLI loses its watch while
        #    the install keeps converging server-side (2026-08-29).
        ctx.log.warn(f"install failed once ({str(exc)[:120]}) - "
                     "letting the cluster settle 120s, then retrying")
        time.sleep(120)
        cli.install_kommander(
            ctx.kubeconfig,
            installer_config=config,
            timeout_minutes=max(1, parse_duration(timeout) // 60),
        )


@step("wait")
def wait_step(ctx, *, duration: str = "60s", reason: str = "") -> None:
    """Sleep for a fixed period - use after an operation to let it settle."""
    seconds = parse_duration(duration)
    ctx.log.info(f"waiting {duration}{f' ({reason})' if reason else ''}")
    if not ctx.config.dry_run:
        time.sleep(seconds)


@step("wait_nodes_ready")
def wait_nodes_ready(ctx, *, count: int, timeout: str = "25m") -> None:
    """Wait until exactly ``count`` nodes report Ready."""
    ctx.kube.wait_nodes_ready(count, timeout_s=parse_duration(timeout))


@step("wait_pods_healthy")
def wait_pods_healthy(ctx, *, namespace: str | None = None, timeout: str = "15m",
                      tolerate: int = 0) -> None:
    """Wait until pods are Running and ready (optionally in one namespace)."""
    ctx.kube.wait_pods_healthy(
        namespace=namespace, timeout_s=parse_duration(timeout), tolerate=tolerate
    )


@step("wait_reconciled")
def wait_reconciled(ctx, *, timeout: str = "20m", kind: str = "cluster") -> None:
    """Wait for CAPI/NKP reconciliation to settle after an operation.

    Checks the conditions that actually matter after a day-2 change: the CAPI
    Cluster reporting Available and TopologyReconciled. A pods-healthy check
    alone hides a cluster whose topology controller has stopped reconciling.
    """
    def settled() -> bool:
        items = ctx.kube.json(["get", kind, "-A"]).get("items", [])
        if not items:
            return False
        for obj in items:
            conds = {
                c.get("type"): c.get("status")
                for c in obj.get("status", {}).get("conditions", [])
            }
            wanted = [t for t in ("Available", "TopologyReconciled", "Ready") if t in conds]
            if not wanted or any(conds[t] != "True" for t in wanted):
                return False
        return True

    wait_for(
        settled,
        ctx.log,
        what=f"{kind} reconciled",
        timeout_s=parse_duration(timeout),
        dry_run=ctx.config.dry_run,
    )


# ----------------------------------------------------------- day-2 operations
def _resolve_node(ctx, node: str | None, role: str | None) -> str:
    if node:
        return node
    names = ctx.kube.node_names(role=role) if role else ctx.kube.node_names()
    if not names:
        if ctx.config.dry_run:
            return f"<{role or 'node'}>"
        raise RuntimeError(f"no node found for role={role}")
    return names[0]


@step("cordon_node")
def cordon_node(ctx, *, node: str | None = None, role: str = "worker") -> None:
    """Mark a node unschedulable."""
    target = _resolve_node(ctx, node, role)
    ctx.remember("node", target)
    ctx.kube.cordon(target)


@step("uncordon_node")
def uncordon_node(ctx, *, node: str | None = None, role: str = "worker") -> None:
    """Mark a node schedulable again."""
    ctx.kube.uncordon(node or ctx.recall("node") or _resolve_node(ctx, None, role))


@step("drain_node")
def drain_node(ctx, *, node: str | None = None, role: str = "worker",
               timeout: str = "10m") -> None:
    """Evict workloads from a node."""
    target = node or ctx.recall("node") or _resolve_node(ctx, None, role)
    ctx.remember("node", target)
    ctx.kube.drain(target, timeout_s=parse_duration(timeout))


@step("scale_workers")
def scale_workers(ctx, *, replicas: int, machine_deployment: str = "") -> None:
    """Scale workers the way NKP actually supports it.

    Scaling the MachineDeployment directly does not work: the cluster is
    topology-managed, so the topology controller reverts the change and the
    node count silently returns to what it was. Confirmed live - a
    `kubectl scale machinedeployment ... --replicas 3` left DESIRED back at 2
    and the scenario timed out waiting for a fourth node.

    The topology also carries no `replicas` field for workers. The size is
    expressed as autoscaler bounds on the NKPCluster:

        cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size
        cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size

    Pinning both to the same value asks for exactly that many workers.
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would pin worker autoscaler bounds to {replicas}")
        return

    items = ctx.kube.json(["get", "nkpcluster", "-A"]).get("items", [])
    if not items:
        raise RuntimeError("no NKPCluster found - is this a self-managed NKP cluster?")
    obj = items[0]
    name = obj["metadata"]["name"]
    namespace = obj["metadata"].get("namespace", "kommander")

    deployments = (
        obj["spec"]["capiCluster"]["topology"]["workers"]["machineDeployments"]
    )
    index = 0
    if machine_deployment:
        index = next(
            (i for i, md in enumerate(deployments)
             if md.get("name") == machine_deployment), 0
        )
    target = deployments[index].get("name", "md-0")

    base = f"/spec/capiCluster/topology/workers/machineDeployments/{index}/metadata"
    patch = [
        {"op": "replace",
         "path": f"{base}/annotations/cluster.x-k8s.io~1cluster-api-autoscaler-node-group-min-size",
         "value": str(replicas)},
        {"op": "replace",
         "path": f"{base}/annotations/cluster.x-k8s.io~1cluster-api-autoscaler-node-group-max-size",
         "value": str(replicas)},
    ]
    ctx.log.info(f"scaling {target} to {replicas} worker(s) via autoscaler bounds")
    ctx.kube.patch_json("nkpcluster", name, patch, namespace=namespace)


@step("restart_workload")
def restart_workload(ctx, *, target: str, namespace: str = "kube-system",
                     timeout: str = "10m") -> None:
    """Roll a Deployment/DaemonSet and wait for it to come back."""
    ctx.kube.rollout_restart(target, namespace=namespace)
    ctx.kube.rollout_status(target, namespace=namespace,
                            timeout_s=parse_duration(timeout))


# ------------------------------------------------------------------ assertions
@step("assert_node_count")
def assert_node_count(ctx, *, count: int, role: str | None = None,
                      timeout: str = "2m") -> None:
    """Assert the node count, waiting up to ``timeout`` for it to settle.

    Every assert accepts a timeout: a condition that is about to become true
    should not fail the run because the check arrived two seconds early.
    """
    if ctx.config.dry_run:
        return

    def matches() -> bool:
        names = ctx.kube.node_names(role=role) if role else ctx.kube.node_names()
        return len(names) == count

    what = f"{count} {role or 'total'} node(s)"
    try:
        wait_for(matches, ctx.log, what=what,
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        names = ctx.kube.node_names(role=role) if role else ctx.kube.node_names()
        raise AssertionError(f"expected {what}, found {len(names)}") from exc
    ctx.log.info(f"asserted {what}")


@step("assert_node_schedulable")
def assert_node_schedulable(ctx, *, node: str | None = None, role: str = "worker",
                            timeout: str = "2m") -> None:
    """Assert a node accepts workloads again, waiting up to ``timeout``."""
    if ctx.config.dry_run:
        return
    target = node or ctx.recall("node") or _resolve_node(ctx, None, role)
    wait_for(lambda: not ctx.kube.is_cordoned(target), ctx.log,
             what=f"{target} schedulable",
             timeout_s=parse_duration(timeout), dry_run=False)
    ctx.log.info(f"asserted {target} is schedulable")


@step("assert_pods_healthy")
def assert_pods_healthy(ctx, *, namespace: str | None = None, tolerate: int = 0,
                        timeout: str = "5m") -> None:
    """Fail if pods are not Running and ready within ``timeout``."""
    if ctx.config.dry_run:
        return
    bad: list[str] = []

    def healthy() -> bool:
        nonlocal bad
        bad = ctx.kube.unhealthy_pods(namespace)
        return len(bad) <= tolerate

    try:
        wait_for(healthy, ctx.log, what=f"pods healthy in {namespace or 'all namespaces'}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except TimeoutError:
        for entry in bad[:10]:
            ctx.log.warn(f"  {entry}")
        raise AssertionError(
            f"{len(bad)} pod(s) unhealthy in {namespace or 'all namespaces'} "
            f"after {timeout}") from None
    ctx.log.info(f"asserted pods healthy in {namespace or 'all namespaces'}")


@step("assert_api_reachable")
def assert_api_reachable(ctx, *, timeout: str = "2m") -> None:
    """Assert the API server answers, waiting up to ``timeout``."""
    if ctx.config.dry_run:
        return
    wait_for(ctx.kube.server_reachable, ctx.log, what="API server reachable",
             timeout_s=parse_duration(timeout), dry_run=False)
    ctx.log.info("asserted API server is reachable")


@step("assert_no_leftover_vms")
def assert_no_leftover_vms(ctx, *, timeout: str = "12m") -> None:
    """Assert every VM of this cluster is gone from Prism Central.

    Prism deletes are asynchronous, so this polls rather than asserting the
    instant after delete returns - the Aug-11 run failed its own cleanup by
    checking while 4 deletes were still in flight.
    """
    if ctx.config.dry_run or not ctx.cluster_name:
        return
    frozen = ctx.recall("frozen_template")
    deadline = time.monotonic() + parse_duration(timeout)
    left = []
    while time.monotonic() < deadline:
        left = ctx.pc.vms_named(ctx.cluster_name)
        if not left:
            ctx.log.info("asserted no leftover VMs")
            return
        if frozen:
            # finish_cluster froze this cluster: its VMs ARE the template now and
            # stay, powered OFF. What must not remain is anything still running.
            on = [v for v in left
                  if (v.get("status", {}).get("resources", {}).get("power_state") or "").upper() != "OFF"]
            if not on:
                ctx.log.info(f"{len(left)} VM(s) remain powered off as template {frozen}")
                return
            ctx.log.info(f"{len(on)}/{len(left)} template VM(s) still powering off...")
        else:
            ctx.log.info(f"{len(left)} VM(s) still present...")
        time.sleep(15)
    names = [v.get("spec", {}).get("name") or v.get("status", {}).get("name", "?") for v in left][:6]
    raise AssertionError(
        f"{len(left)} VM(s) still {'powered on' if frozen else 'present'} after {timeout}: {names}"
    )


@step("collect_diagnostics")
def collect_diagnostics(ctx, *, label: str = "failure", bundle: bool = True) -> None:
    """Failure forensics: state dumps plus a real support bundle.

    The dumps (nodes/pods/events/CAPI/HRs/PC VMs) answer the quick questions;
    ``nkp diagnose`` produces the same support bundle support would ask a
    customer for, written under the scenario's artifacts so the cluster can be
    inspected long after cleanup deleted it.
    """
    collect_cluster(ctx, label=label)
    if bundle and ctx.kubeconfig is not None and not ctx.config.dry_run:
        try:
            ctx.nkp.diagnose(ctx.kubeconfig, ctx.artifacts / f"diagnostics-{label}")
            ctx.log.info(f"support bundle written under diagnostics-{label}/")
        except Exception as exc:  # noqa: BLE001 - diagnostics never mask the failure
            ctx.log.warn(f"support bundle collection failed (non-fatal): {exc}")

@step("run")
def run_command(ctx, *, kubectl: str | None = None, nkp: str | None = None,
                expect_success: bool = True) -> None:
    """Escape hatch: run a raw kubectl or nkp command."""
    import shlex

    if kubectl:
        ctx.kube._run(shlex.split(kubectl), check=expect_success)  # noqa: SLF001
    elif nkp:
        ctx.nkp._run(shlex.split(nkp), check=expect_success)  # noqa: SLF001
    else:
        raise ValueError("run: provide either kubectl: or nkp:")


# ------------------------------------------------------------ documentation
@step("log")
def log_step(ctx, *, message: str) -> None:
    """Write a message to the scenario log (useful in examples)."""
    ctx.log.info(message)


@step("fail")
def fail_step(ctx, *, message: str = "deliberate failure") -> None:
    """Fail on purpose - used by the example scenario to show the failure path."""
    raise AssertionError(message)


# ----------------------------------------------------------------- topology
@step("assert_no_control_plane_taints")
def assert_no_control_plane_taints(ctx, *, node: str | None = None,
                                   timeout: str = "2m") -> None:
    """Fail if control-plane taints would stop workloads scheduling.

    On a single-node cluster the control plane must also carry workloads, so a
    NoSchedule taint makes the profile unusable. ``timeout`` covers the node
    registering late - taints are read once it exists.
    """
    if ctx.config.dry_run:
        return

    def node_known() -> bool:
        return bool(node or ctx.kube.node_names())

    wait_for(node_known, ctx.log, what="a node to assert taints on",
             timeout_s=parse_duration(timeout), dry_run=False)
    target = node or (ctx.kube.node_names() or [""])[0]
    taints = ctx.kube.json(["get", "node", target]).get("spec", {}).get("taints") or []
    blocking = [
        t for t in taints
        if t.get("effect") in ("NoSchedule", "NoExecute")
        and "control-plane" in t.get("key", "")
    ]
    if blocking:
        raise AssertionError(
            f"{target} still carries control-plane taints {blocking}; workloads "
            "cannot be scheduled"
        )
    ctx.log.info(f"asserted {target} accepts workloads")


@step("report_unschedulable_pods")
def report_unschedulable_pods(ctx, *, filename: str = "unschedulable.txt") -> None:
    """Record pods that cannot settle, without failing the scenario.

    Used by experimental profiles where some components legitimately cannot fit.
    """
    if ctx.config.dry_run:
        return
    remaining = ctx.kube.unhealthy_pods()
    if remaining:
        ctx.log.warn(f"{len(remaining)} pod(s) could not settle:")
        for entry in remaining[:15]:
            ctx.log.warn(f"  {entry}")
        (ctx.artifacts / filename).write_text("\n".join(remaining), encoding="utf-8")


# ------------------------------------------------------------ platform / apps
@step("record_platform_version")
def record_platform_version(ctx, *, key: str = "platform_before") -> None:
    """Remember the installed platform version for a later comparison."""
    version = ctx.nkp.platform_version(ctx.kubeconfig)
    ctx.remember(key, version)
    ctx.log.info(f"platform version: {version}")


@step("upgrade_nodes")
def upgrade_nodes(ctx, *, vm_image: str = "", namespace: str = "kommander",
                  skip_preflight: list | None = None, timeout: str = "60m") -> None:
    """Kubernetes/node-level upgrade: `nkp upgrade cluster nutanix`.

    Source-verified behaviour this step depends on:
      * the command NO-OPs when the cluster's topology.version already equals
        the CLI's target (konvoy2 cluster/upgrade.go:214-218,508-513), so the
        baseline MUST be older - this step refuses to run if it is not;
      * on a management cluster it also upgrades the CAPI stack and
        re-applies the ClusterClass (upgrade.go:226,416-463), which is why
        no separate capi-components step exists here.
    """
    if ctx.config.dry_run:
        ctx.log.info("[dry-run] would upgrade cluster nodes (k8s roll)")
        return
    before = {n["metadata"]["name"]: n["status"]["nodeInfo"]["kubeletVersion"]
              for n in ctx.kube.nodes()}
    versions = sorted(set(before.values()))
    ctx.remember("nodes_before", before)
    ctx.log.info(f"node kubelet versions before: {versions}")
    # The CAPI Cluster is NOT always in `kommander`: a `--self-managed` 2.17
    # cluster keeps it in `default`, and it stays there even after upgrading.
    # Passing the wrong namespace makes the CLI fail with a flat
    # `clusters.cluster.x-k8s.io "<name>" not found`, which reads as a missing
    # cluster rather than a lookup in the wrong place (live 2026-08-30).
    # Find it instead of making every scenario author remember.
    if not namespace or namespace == "kommander":
        found = [c["metadata"]["namespace"]
                 for c in ctx.kube.json(["get", "cluster", "-A"]).get("items", [])
                 if c["metadata"]["name"] == ctx.cluster_name]
        if found and found[0] != namespace:
            ctx.log.info(f"CAPI Cluster {ctx.cluster_name} is in namespace "
                         f"{found[0]} (not {namespace or 'kommander'})")
            namespace = found[0]
    if skip_preflight:
        ctx.remember("skip_preflight", list(skip_preflight))
    # The CLI requires a target image (flag group vm-image | control-plane-vm-image)
    # and exits 1 in three seconds without one, with the reason on a line the
    # log truncates (live-caught 2026-09-07). Name it here instead.
    if not (vm_image or ctx.config.machine_image):
        raise RuntimeError(
            "upgrade_nodes needs the image the nodes should roll TO: set "
            "vm_image: on the step (preferred) or export E2E_MACHINE_IMAGE. "
            "`nkp upgrade cluster nutanix` refuses to run without --vm-image.")
    ctx.nkp.upgrade_cluster_nodes(
        ctx.kubeconfig, ctx.cluster_name, namespace=namespace,
        vm_image=vm_image or ctx.config.machine_image,
        skip_preflight=list(skip_preflight or []),
        timeout_minutes=max(1, parse_duration(timeout) // 60))


@step("assert_app_pins_resolve")
def assert_app_pins_resolve(ctx, *, timeout: str = "1m") -> None:
    """Every platform AppDeployment pin names a ClusterApp that exists.

    The 2026-09-07 stall in five seconds: `clusterConfigOverrides[].appVersion`
    is written by the (nightly) operator; the ClusterApps come from the bundle.
    When they disagree the upgrade CLI waits its whole timeout on a version
    that cannot render, with nothing unhealthy anywhere. Run it before an
    upgrade (the bundle is what you published) and after (the pins are what
    the operator wrote).
    """
    if ctx.config.dry_run:
        ctx.log.info("[dry-run] would assert every platform pin resolves to a ClusterApp")
        return
    bad: list = []

    def ok() -> bool:
        bad[:] = _unresolvable_pins(ctx)
        return not bad

    try:
        wait_for(ok, ctx.log, what="every platform pin to resolve to a ClusterApp",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            "platform pin(s) reference a ClusterApp that does not exist - the bundle "
            "and the operator disagree on versions: " + "; ".join(bad) +
            ". Merge the kommander-applications branch onto its release branch.") from exc
    ctx.log.info("every platform pin resolves to an existing ClusterApp")


@step("assert_kapps_app_versions")
def assert_kapps_app_versions(ctx, *, apps: dict, ref: str = "", repo: str = "") -> None:
    """A kommander-applications ref carries these app versions (so the bundle will).

    ``apps: {kommander-ui: 17.234.34}``. Reads the git tree, not the cluster:
    it answers "will the bundle I am about to publish carry what the operator
    pins?" before anything is built. ``ref`` defaults to the change-set's
    resolved kommander-applications commit, else the checkout's HEAD.
    """
    import subprocess
    from pathlib import Path
    comps = ctx.recall("changeset_components") or []
    if repo:
        os.environ["E2E_KAPPS_DIR"] = repo
    src = _select_kapps_source(comps)
    repo_dir = src.git_root
    if not ref:
        ref = _kapps_sha8(comps) or "HEAD"
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert {repo_dir.name}@{ref} carries {apps}")
        return
    missing = []
    for app, want in (apps or {}).items():
        out = subprocess.run(["git", "-C", str(repo_dir), "ls-tree", "--name-only", ref, f"{src.prefix}applications/{app}/"],
                             capture_output=True, text=True)
        have = sorted(Path(x).name for x in out.stdout.split() if Path(x).name[:1].isdigit())
        if str(want) not in have:
            missing.append(f"{app}: want {want}, {ref[:8]} carries {have or 'nothing'}")
    if missing:
        raise AssertionError(f"kommander-applications@{ref[:8]} does not carry: " + "; ".join(missing))
    ctx.log.info(f"kommander-applications@{ref[:8]} carries {apps}")


@step("assert_federation_healthy")
def assert_federation_healthy(ctx, *, timeout: str = "5m") -> None:
    """Every federated member is Ready, claimed by a KommanderCluster, and propagating.

    Three things, because each failed for real: a KubeFedCluster that is
    Offline (dead endpoint); a member no KommanderCluster claims (an orphan
    left by a detach - the frozen-template defect of 2026-09-07); and
    Federated* objects whose Propagation condition is not True.
    """
    if ctx.config.dry_run:
        ctx.log.info("[dry-run] would assert federation is healthy")
        return
    problems: list = []

    def healthy() -> bool:
        problems.clear()
        members = (ctx.kube.json(["get", "kubefedcluster", "-A"]) or {}).get("items", [])
        kcs = (ctx.kube.json(["get", "kommandercluster", "-A"]) or {}).get("items", [])
        claimed = {(i.get("spec", {}).get("kubefedClusterRef") or {}).get("name") for i in kcs}
        claimed |= {i["metadata"]["name"] for i in kcs}
        if not members:
            problems.append("no KubeFedCluster at all")
        for m in members:
            name = m["metadata"]["name"]
            conds = {c.get("type"): c for c in (m.get("status", {}) or {}).get("conditions", [])}
            if conds.get("Offline", {}).get("status") == "True" or conds.get("Ready", {}).get("status") != "True":
                why = (conds.get("Offline") or conds.get("Ready") or {}).get("reason", "?")
                problems.append(f"{name}: not Ready ({why})")
            if name not in claimed:
                problems.append(f"{name}: no KommanderCluster claims it (orphan)")
        for kind in ("federatedclusterrole", "federatedclusterrolebinding"):
            for f in (ctx.kube.json(["get", kind, "-A"]) or {}).get("items", []):
                pc = next((c for c in (f.get("status", {}) or {}).get("conditions", []) if c.get("type") == "Propagation"), None)
                if pc and pc.get("status") != "True":
                    problems.append(f"{kind}/{f['metadata']['name']}: Propagation={pc.get('status')} {pc.get('reason', '')}")
        return not problems

    try:
        wait_for(healthy, ctx.log, what="federation to be healthy",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("federation is not healthy: " + "; ".join(problems)) from exc
    ctx.log.info("federation healthy: every member Ready, claimed, propagating")


@step("assert_dashboard_serves")
def assert_dashboard_serves(ctx, *, title_contains: str = "Log In", timeout: str = "5m") -> None:
    """`nkp get dashboard`'s URL answers over HTTP and lands on the login page.

    The customer-visible chain - LoadBalancer address -> traefik -> forward-auth
    -> dex - in one request. Follows redirects, ignores the self-signed CA, and
    asserts the FINAL page title, not just a status code. MetalLB addresses do
    not answer ICMP, so this is the only honest reachability check for the UI.
    """
    if ctx.config.dry_run:
        ctx.log.info("[dry-run] would assert the dashboard serves its login page")
        return
    import re
    import ssl
    import urllib.request
    res = ctx.nkp._run(["get", "dashboard", "--kubeconfig", str(ctx.kubeconfig)], quiet=True)
    m = re.search(r"URL:\s*(https?://\S+)", getattr(res, "stdout", "") or "")
    if not m:
        raise AssertionError("nkp get dashboard printed no URL")
    url = m.group(1)
    ctx.remember("dashboard_url", url)
    ctx.log.info(f"dashboard: {url}")
    sslctx = ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    last = {"note": ""}

    def serves() -> bool:
        try:
            # traefik-forward-auth answers a bare client with 401 and a browser
            # with the login redirect chain; it decides by these headers
            # (live-caught 2026-09-07: urllib got 401 where curl got 302 -> 200).
            req = urllib.request.Request(url, headers={'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=15, context=sslctx) as r:
                body = r.read(200000).decode(errors="replace")
                t = re.search(r"<title>([^<]*)</title>", body, re.I)
                last["note"] = f"HTTP {r.status} at {r.geturl()} title={t.group(1).strip() if t else '?'}"
                return r.status == 200 and bool(t) and title_contains.lower() in t.group(1).lower()
        except Exception as exc:  # noqa: BLE001
            last["note"] = f"{type(exc).__name__}: {exc}"
            return False

    try:
        wait_for(serves, ctx.log, what=f"dashboard to serve a page titled *{title_contains}*",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"dashboard does not serve: {last['note']}") from exc
    ctx.log.info(f"dashboard serves: {last['note']}")


@step("assert_preflight_skipped")
def assert_preflight_skipped(ctx, *, checks: list, namespace: str = "", timeout: str = "2m") -> None:
    """The CAPI Cluster carries the skip-preflight annotation for these checks.

    `--skip-preflight-checks` is not a CLI-side switch: konvoy2 writes the list
    onto the Cluster's `preflight.cluster.caren.nutanix.com/skip` annotation (generator/cluster.go,
    upgrade.go:48, nodepool.go:20) and CAREN's preflight webhook honours it
    (pkg/webhook/preflight/skip). Six blockers over four months
    (NCN-113929..117277) were the flag being accepted and the annotation not
    written - by upgrade, by nodepool creation, by UpgradePlan. This reads the
    annotation, which is the only thing that decides whether a check is skipped.
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert the Cluster annotation skips {checks}")
        return
    want = {str(c) for c in (checks or [])}
    seen = {"val": None}

    def present() -> bool:
        items = (ctx.kube.json(["get", "cluster", "-A"]) or {}).get("items", [])
        cl = next((c for c in items if c["metadata"]["name"] == ctx.cluster_name
                   and (not namespace or c["metadata"]["namespace"] == namespace)), None)
        if not cl:
            return False
        val = (cl["metadata"].get("annotations") or {}).get("preflight.cluster.caren.nutanix.com/skip", "")
        seen["val"] = val
        have = {x.strip() for x in val.split(",") if x.strip()}
        return want <= have or "all" in have

    try:
        wait_for(present, ctx.log, what=f"Cluster annotation to skip {sorted(want)}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"preflight skip NOT recorded on Cluster {ctx.cluster_name}: wanted {sorted(want)}, "
            f"annotation preflight.cluster.caren.nutanix.com/skip={seen['val']!r} - the CLI accepted the flag and did not write it") from exc
    ctx.log.info(f"Cluster {ctx.cluster_name} skips preflights {sorted(want)} (preflight.cluster.caren.nutanix.com/skip={seen['val']})")


@step("assert_nodes_upgraded")
def assert_nodes_upgraded(ctx, *, count: int = 0, timeout: str = "45m") -> None:
    """Every node reports a NEW kubelet version and the cluster is whole.

    Asserts on node kubeletVersion, NOT MachineDeployment .version: the CLI
    only writes spec.topology.version (generator/upgrade.go:54-57), so MD
    version fields are the wrong observable.
    """
    if ctx.config.dry_run:
        return
    before = ctx.recall("nodes_before") or {}
    old_versions = set(before.values())
    want = count or len(before)

    def rolled() -> bool:
        nodes = ctx.kube.nodes()
        if len(nodes) != want:
            return False
        vers = {n["status"]["nodeInfo"]["kubeletVersion"] for n in nodes}
        ready = all(any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in n["status"].get("conditions", []))
                    for n in nodes)
        return ready and bool(vers) and not (vers & old_versions)

    wait_for(rolled, ctx.log,
             what=f"all {want} node(s) Ready on a NEW kubelet version",
             timeout_s=parse_duration(timeout), dry_run=False)
    after = {n["metadata"]["name"]: n["status"]["nodeInfo"]["kubeletVersion"]
             for n in ctx.kube.nodes()}
    ctx.log.info(f"node roll verified: {sorted(old_versions)} -> "
                 f"{sorted(set(after.values()))}")
    # every node must be backed by a VM the CAPI tree actually tracks - a
    # roll is exactly where CAPX duplicate-name twins appear (live-caught 2x)
    # An EMPTY machine list must never be read as "every node is stray" - that
    # turns an apiserver blip (guaranteed here: the control plane was just
    # replaced) into a confident, wrong "CAPX duplicate-name twin" verdict.
    # Wait for the CAPI tree to answer before judging identity.
    def machines_visible() -> bool:
        return bool(ctx.kube.json(["get", "machines", "-A"]).get("items", []))

    wait_for(machines_visible, ctx.log,
             what="the CAPI machine list to be readable after the roll",
             timeout_s=300, dry_run=False)
    machines = ctx.kube.json(["get", "machines", "-A"]).get("items", [])
    tracked = {m["spec"].get("providerID") for m in machines}
    stray = [n["metadata"]["name"] for n in ctx.kube.nodes()
             if n["spec"].get("providerID") not in tracked]
    if stray:
        raise AssertionError(
            f"post-roll identity check failed: node(s) {stray} run on VMs the "
            "CAPI tree does not track (CAPX duplicate-name twin)")
    ctx.log.info("post-roll identity check: every node maps to a tracked Machine")


@step("deliver_changes")
def deliver_changes(ctx, *, changes: list) -> None:
    """Put a developer's change onto a cluster that already exists.

    ``create_cluster: {changes: [...]}`` is the usual route, but a GA
    baseline is pristine by definition - create_cluster REFUSES changes
    alongside ``version:`` - so an upgrade scenario has to deliver its
    change after it has its baseline. Same machinery and the same
    content-addressed image tags; the branch resolves to a sha8 and the
    sha8 IS the registry tag, so nothing here is configured by hand.
    """
    comps = _resolve_changes(ctx, changes)
    if not comps:
        raise RuntimeError(
            "deliver_changes: nothing to deliver - pass changes: [repo@branch]")
    ctx.remember("changeset_components", comps)
    _deliver_changes(ctx, comps)


@step("record_nodes")
def record_nodes(ctx, *, key: str = "nodes_snapshot") -> None:
    """Snapshot node identity, for a later step to prove nodes did or did not move."""
    if ctx.config.dry_run:
        return
    snap = {n["metadata"]["name"]: {
        "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
        "providerID": n["spec"].get("providerID", ""),
        "uid": n["metadata"].get("uid", ""),
    } for n in ctx.kube.nodes()}
    ctx.remember(key, snap)
    ctx.log.info(f"recorded {len(snap)} node(s): {sorted(snap)}")


@step("assert_nodes_unchanged")
def assert_nodes_unchanged(ctx, *, was: str = "nodes_snapshot") -> None:
    """CONTAINMENT: prove this upgrade did NOT touch the node layer.

    A blast-radius claim is only worth something if its negative is checked.
    Replacing nodes is the loudest, slowest thing an NKP upgrade can do, so
    an unchanged node set is what separates "kommander-scoped" from
    "everything" - and it is the assertion that would catch the day a
    kommander-only upgrade starts rolling machines.

    Compares uid as well as name: a replaced node can reuse a name, but
    never its uid.
    """
    if ctx.config.dry_run:
        return
    before = ctx.recall(was) or {}
    if not before:
        raise RuntimeError(
            f"assert_nodes_unchanged: nothing recorded under {was!r} - "
            "run record_nodes before the upgrade")
    after = {n["metadata"]["name"]: {
        "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
        "providerID": n["spec"].get("providerID", ""),
        "uid": n["metadata"].get("uid", ""),
    } for n in ctx.kube.nodes()}
    if after == before:
        ctx.log.info(f"containment verified: all {len(after)} node(s) untouched "
                     f"(same uid, kubelet and providerID)")
        return
    gone = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    changed = {n: f"{before[n]} -> {after[n]}"
               for n in set(before) & set(after) if before[n] != after[n]}
    raise AssertionError(
        "containment FAILED - this upgrade touched the node layer: "
        f"replaced={gone} new={added} changed={changed}. Either the change "
        "has a wider blast radius than the scenario claims, or the upgrade "
        "verb under test now rolls nodes.")


# ------------------------------------------------- upgrade target: version
#: Where `upgrade kommander` gets the k-apps for its target version. Verified
#: in kommander-cli/pkg/appsrepo/uri.go: the CLI builds this exact URL from
#: `version.GetVersion().GitVersion` unless --kommander-applications-repository
#: overrides it. A version with no tarball here cannot be upgraded to, and the
#: failure lands ~40 min into a run as a go-getter 403.
_KAPPS_TARBALL = "https://downloads.d2iq.com/dkp/{v}/kommander-applications-{v}.tar.gz"


def _http_exists(url: str):
    """True / False / None (cannot tell). Only a definitive 2xx or 403/404 answers."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            # the bucket answers 200 with a real content-length for a real
            # artifact; anything tiny is an error document, not a release.
            length = int(resp.headers.get("Content-Length") or 0)
            return resp.status == 200 and length > 1024
    except urllib.error.HTTPError as exc:
        return False if exc.code in (403, 404) else None
    except Exception:  # noqa: BLE001 - a preflight must never block a good run
        return None


def _gh_release_exists(repo: str, tag: str):
    import subprocess

    try:
        r = subprocess.run(["gh", "release", "view", tag, "-R", repo,
                            "--json", "tagName", "-q", ".tagName"],
                           capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode == 0 and r.stdout.strip():
        return True
    if "release not found" in (r.stderr or "").lower():
        return False
    return None


def _cli_kommander_version(binary: str) -> str:
    """The version `upgrade kommander` will target, read from the binary itself.

    `nkp version` prints one line per component; the kommander line is the one
    that decides the platform target (kommander-cli reads
    version.GetVersion().GitVersion). A plain `go build` reports v0.0.0-dev.
    """
    import subprocess

    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True,
                             timeout=120).stdout
    except Exception:  # noqa: BLE001
        return ""
    for line in out.splitlines():
        if line.strip().startswith("kommander:"):
            return line.split(":", 1)[1].strip()
    return ""


@step("preflight_version_artifacts")
def preflight_version_artifacts(ctx, *, version: str = "", require: bool = True,
                                need_cli: bool = False) -> None:
    """Does this NKP version exist as artifacts? Answer in seconds, not 40 minutes.

    An upgrade target is only real if the things the upgrade FETCHES are real.
    Two probes, both against the sources the product itself uses:

      * the kommander-applications tarball the CLI downloads
        (downloads.d2iq.com - the URL kommander-cli/pkg/appsrepo/uri.go builds);
      * the konvoy2 / kommander-cli GitHub releases - but ONLY when the
        framework has to fetch a CLI for the version (``need_cli``); a
        developer's own build needs published k-apps, not a published release.

    This is also how the framework DISCOVERS which versions are testable -
    empirically, per run, with no allowlist to go stale. Verified live
    2026-08-31: v2.18.0 / v2.17.0 / v2.19.0-dev.23 / v2.18.0-rc.1 all resolve;
    v2.18.1 / v2.18.2 / v2.19.0 do not exist at all.
    """
    version = version or ctx.recall("upgrade_target_version") or ""
    if not version:
        raise RuntimeError("preflight_version_artifacts needs version: (or a "
                           "prior upgrade step that recorded one)")
    if not version.startswith("v"):
        version = "v" + version
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would preflight artifacts for {version}")
        return

    probes = [
        ("kommander-applications tarball", _http_exists(_KAPPS_TARBALL.format(v=version))),
    ]
    # The GitHub releases only matter when the framework has to FETCH a CLI for
    # this version. A developer running their own build already has the binary;
    # failing them because their version was never released would be wrong -
    # nightly builds have k-apps published but no release (verified live
    # 2026-08-31: v0.0.0-dev has a tarball, no GitHub release).
    if need_cli:
        probes += [
            ("konvoy2 release", _gh_release_exists("mesosphere/konvoy2", version)),
            ("kommander-cli release", _gh_release_exists("mesosphere/kommander-cli", version)),
        ]
    missing = [name for name, ok in probes if ok is False]
    unknown = [name for name, ok in probes if ok is None]
    for name, ok in probes:
        ctx.log.info(f"    {name}: " +
                     {True: "present", False: "MISSING", None: "cannot tell"}[ok])
    if unknown:
        ctx.log.warn(f"could not verify {', '.join(unknown)} for {version} "
                     "(no network/gh auth?) - not blocking the run")
    if missing and require:
        raise AssertionError(
            f"NKP {version} is not a testable upgrade target: {', '.join(missing)} "
            f"does not exist.\n"
            f"  The platform version is decided by the CLI BINARY, and everything "
            f"downstream (KommanderCore, ManagementPlane, the k-apps the operators "
            f"fetch) follows it - so a version with no artifacts wedges the upgrade "
            f"~40 minutes in rather than failing here.\n"
            f"  Pick a released version, or point the scenario at a build whose "
            f"version has published artifacts.")
    if not missing:
        ctx.log.info(f"NKP {version} is artifact-backed - upgrade target is real")


def _fetch_release_cli(ctx, version: str) -> str:
    """Fetch konvoy+kommander for a released version and shim them as one `nkp`."""
    import platform as _platform
    import subprocess

    goos = "linux" if _platform.system() == "Linux" else "darwin"
    goarch = {"x86_64": "amd64", "amd64": "amd64",
              "arm64": "arm64", "aarch64": "arm64"}[_platform.machine()]
    dest = ctx.artifacts / f"nkp-{version}"
    dest.mkdir(parents=True, exist_ok=True)
    shim = dest / "nkp"
    if shim.exists():
        return str(shim)
    ctx.log.info(f"fetching konvoy+kommander {version} ({goos}/{goarch}) from GitHub releases")
    for repo, asset, out in (
        ("mesosphere/konvoy2", "konvoy", "konvoy"),
        ("mesosphere/kommander-cli", "kommander", "kommander"),
    ):
        tgz = dest / (out + ".tgz")
        # Older releases ship darwin_amd64 only - Rosetta runs it fine.
        arches = [goarch] + (["amd64"] if goarch != "amd64" else [])
        rc = None
        for arch in arches:
            rc = subprocess.run(
                ["gh", "release", "download", version, "-R", repo,
                 "-p", f"{asset}_{version}_{goos}_{arch}.tar.gz",
                 "-O", str(tgz), "--clobber"],
                capture_output=True, text=True, timeout=600)
            if rc.returncode == 0:
                if arch != goarch:
                    ctx.log.info(f"{asset} {version}: no {goarch} asset - using {arch}")
                break
        if rc is None or rc.returncode != 0:
            raise RuntimeError(
                f"cannot fetch {asset} {version} from {repo} "
                f"(tried {', '.join(arches)}): " + (rc.stderr.strip()[:200] if rc else ""))
        subprocess.run(["tar", "-xzf", str(tgz), "-C", str(dest)], check=True, timeout=300)
    shim.write_text(SHIM_TEMPLATE.replace("VERSION", version))
    shim.chmod(0o755)
    return str(shim)



def _build_and_push_image(ctx, repo: str, sha8: str) -> None:
    """Build the container image for a change-set commit and push it.

    Every other repo in a change-set is self-contained: konvoy2 and
    kommander-cli are compiled here, charts are packaged and pushed here,
    kommander-applications is synced here. `kommander` alone used to assume
    somebody else had already built and pushed the image whose ref it injects -
    so a developer naming their branch got an ImagePullBackOff twenty minutes
    later instead of a cluster running their code.

    Reuses nkp-instant-build's goreleaser-aware planner rather than
    reimplementing it: it reads .goreleaser.yaml to work out which images a
    diff actually affects, so a one-file change does not rebuild everything.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    # Order matters: _dev_image_ref needs GHCR_USER and _dev_image_exists makes
    # a network call, so both must sit BELOW the dry-run guard or `--dry-run`
    # fails on a machine with no registry credentials - and run_e2e.py promises
    # a dry run touches nothing.
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would build and push the {repo} image for {sha8}")
        return
    ref = _dev_image_ref(_image_for_repo(repo), sha8)
    if _dev_image_exists(ref) is True:
        ctx.log.info(f"image already published: {ref}")
        return

    ib = Path(os.environ.get("NKP_INSTANT_BUILD_DIR",
              str(Path.home() / "Documents/nkp/nkp-instant-build")))
    if not (ib / "orchestrator" / "image_plan.py").exists():
        raise RuntimeError(
            f"cannot build the {repo} image: nkp-instant-build not found at {ib}. "
            "Set NKP_INSTANT_BUILD_DIR, or publish the image yourself.")
    _sys.path.insert(0, str(ib))
    from orchestrator import image_plan  # noqa: PLC0415

    src = Path(os.environ.get("NKP_REPOS_DIR",
               str(Path.home() / "Documents/nkp"))) / repo
    # build from a worktree pinned at the sha, so the developer's checkout is
    # never moved under them mid-run
    cache = Path.home() / ".cache/nkp-worktrees"
    wt = cache / f"{repo}-{sha8}"
    if not (wt / ".git").exists() and not wt.exists():
        cache.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-C", str(src), "worktree", "add", "--detach",
                            str(wt), sha8], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"worktree for {repo}@{sha8}: {r.stderr[-300:]}")

    base = _diff_base(src, sha8, os.environ.get(
        "E2E_KOMMANDER_BASE", "origin/release-2.18"))
    diff = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", base, sha8],
                          capture_output=True, text=True, timeout=120).stdout.splitlines()
    builds, need = image_plan.plan(str(wt), diff)
    if not need:
        raise RuntimeError(
            f"{repo}@{sha8} changes no file that feeds a container image "
            f"(diff vs {base[:12]} touched {len(diff)} file(s)) - so there is "
            "nothing to deliver and the cluster would keep running shipped code.")

    user, token = _ghcr_user(), os.environ.get("GHCR_TOKEN", "")
    if not token:
        raise RuntimeError("building images needs GHCR_TOKEN to push")
    li = subprocess.run(["docker", "login", "ghcr.io", "-u", user, "--password-stdin"],
                        input=token, capture_output=True, text=True, timeout=120)
    if li.returncode != 0:
        raise RuntimeError(f"docker login ghcr.io failed: {li.stderr[-200:]}")

    base_ver = os.environ.get("E2E_BASE", "v2.18.0")
    ctx.log.info(f"building {len(need)} image(s) for {repo}@{sha8}: "
                 f"{', '.join(i['image'] for i in need)}")
    built = image_plan.build_and_push(
        str(wt), builds, need, base_ver, f"ghcr.io/{user}",
        log=ctx.log.info,
        ref_maker=lambda im, _s=sha8: _dev_image_ref(im, _s))
    for img, pushed in built:
        ctx.log.info(f"pushed {img} -> {pushed}")


def _image_for_repo(repo: str) -> str:
    """The image name the delivery mapping expects for a repo."""
    ds = REPO_DELIVERY.get(repo) or []
    return ds[0]["image"] if ds else repo


class _KappsSource:
    """Where the kommander-applications tree lives for this change-set.

    Two layouts exist at once (2026-09-07). The release-2.18 line still uses
    the separate `kommander-applications` repository. On kommander `main`
    (since 2026-06-11, "Move kapps contents to a subdirectory") the same tree
    is the `kommander-applications/` directory INSIDE the kommander repo, so a
    single `kommander@ref` carries controller code and application content.
    Every helper below runs git in ``git_root`` and prefixes paths with
    ``prefix``; nothing else in the framework knows which layout it is on.
    """

    def __init__(self, git_root, prefix: str, sha8: str, line: str):
        from pathlib import Path
        self.git_root = Path(git_root)
        self.prefix = prefix              # "" or "kommander-applications/"
        self.sha8 = sha8                  # the commit the tree is read at
        self.line = line                  # "separate" | "subtree"
        self.dir = self.git_root / prefix if prefix else self.git_root

    @property
    def tree(self) -> str:
        """`<sha>` or `<sha>:<prefix>` - what git archive / ls-tree take."""
        return f"{self.sha8}:{self.prefix.rstrip('/')}" if self.prefix else self.sha8

    @property
    def default_base(self) -> str:
        line_default = "origin/main" if self.line == "subtree" else "origin/release-2.18"
        override = os.environ.get("E2E_KAPPS_BASE")
        if not override:
            return line_default
        # An override written for the other layout (nkp-e2e.env carrying
        # origin/release-2.18 while the change-set is on the main subtree) makes
        # every file look changed. A base that does not contain the tree is
        # not a base for it.
        if _git_has(self.git_root, override, self.prefix + "applications"):
            return override
        return line_default

    def strip(self, path: str) -> str:
        return path[len(self.prefix):] if self.prefix and path.startswith(self.prefix) else path

    def __repr__(self) -> str:
        return f"{self.line}:{self.dir}@{self.sha8}"


_KAPPS_SRC: "_KappsSource | None" = None


def _repos_dir():
    from pathlib import Path
    return Path(os.environ.get("NKP_REPOS_DIR", str(Path.home() / "Documents/nkp")))


def _git_has(git_root, ref: str, path: str = "") -> bool:
    import subprocess
    spec = f"{ref}:{path}" if path else f"{ref}^{{commit}}"
    return subprocess.run(["git", "-C", str(git_root), "cat-file", "-e", spec],
                          capture_output=True).returncode == 0


def _git_head8(git_root) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(git_root), "rev-parse", "--short=8", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _select_kapps_source(comps=None) -> "_KappsSource":
    """Decide the layout for a change-set, once, and remember it.

    Order: an explicit ``E2E_KAPPS_DIR``; a `kommander-applications@sha` entry
    (separate repo); a `kommander@sha` whose tree contains
    `kommander-applications/applications` (subtree, main line); otherwise
    whichever checkout exists, at HEAD.
    """
    global _KAPPS_SRC
    import subprocess
    from pathlib import Path
    comps = comps or []
    by = {c.partition("@")[0]: c.partition("@")[2] for c in comps}
    explicit = os.environ.get("E2E_KAPPS_DIR")
    if explicit:
        d = Path(explicit).expanduser().resolve()
        root = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True).stdout.strip()
        if not root or not (d / "applications").is_dir():
            raise RuntimeError(f"E2E_KAPPS_DIR={explicit} is not an applications tree inside a git checkout")
        rel = d.relative_to(Path(root).resolve())
        prefix = (str(rel) + "/") if str(rel) != "." else ""
        _KAPPS_SRC = _KappsSource(root, prefix, by.get("kommander") or by.get("kommander-applications") or _git_head8(root),
                                  "subtree" if prefix else "separate")
        return _KAPPS_SRC
    sep = _repos_dir() / "kommander-applications"
    kom = _repos_dir() / "kommander"
    if by.get("kommander-applications"):
        if not sep.is_dir():
            raise RuntimeError(
                f"changes names kommander-applications@{by['kommander-applications']} but there is no "
                f"checkout at {sep}. On the main line the applications tree lives inside the kommander "
                "repository (kommander-applications/); name it as kommander@<ref> instead, or set E2E_KAPPS_DIR.")
        _KAPPS_SRC = _KappsSource(sep, "", by["kommander-applications"], "separate")
        return _KAPPS_SRC
    if by.get("kommander") and kom.is_dir() and _git_has(kom, by["kommander"], "kommander-applications/applications"):
        _KAPPS_SRC = _KappsSource(kom, "kommander-applications/", by["kommander"], "subtree")
        return _KAPPS_SRC
    if sep.is_dir():
        _KAPPS_SRC = _KappsSource(sep, "", _git_head8(sep), "separate")
        return _KAPPS_SRC
    if kom.is_dir() and (kom / "kommander-applications" / "applications").is_dir():
        _KAPPS_SRC = _KappsSource(kom, "kommander-applications/", _git_head8(kom), "subtree")
        return _KAPPS_SRC
    raise RuntimeError(
        f"no kommander-applications tree: neither {sep} nor {kom}/kommander-applications exists. "
        "Check out one of them under NKP_REPOS_DIR, or set E2E_KAPPS_DIR.")


def _kapps_src() -> "_KappsSource":
    return _KAPPS_SRC or _select_kapps_source([])


def _kapps_sha8(comps) -> str:
    """The commit the applications tree is delivered from, for this change-set."""
    src = _select_kapps_source(comps)
    by = {c.partition("@")[0]: c.partition("@")[2] for c in (comps or [])}
    if by.get("kommander-applications"):
        return by["kommander-applications"]
    if src.line == "subtree" and by.get("kommander"):
        return by["kommander"]
    return ""


def _kapps_requested_chart_tags(kapps_sha8: str) -> dict:
    """Which chart tags kommander-applications ASKS FOR out of our own registry.

    An app in k-apps holds a reference (OCIRepository url + ref.tag), and the
    k-apps delivery route rewrites that reference in the cluster's git. So when
    a scenario changes BOTH repos, k-apps has the last word: its sync lands
    after publish_chart's repoint and overwrites it. Publishing only the
    sha8-suffixed tag then leaves Flux asking for a tag nobody ever pushed.

    Returns {chart-name: tag} for every reference that points at OUR package,
    so _deliver_charts can satisfy what k-apps declares. References to
    mesosphere/upstream registries are left alone - those are not ours to push.
    """
    import re
    import subprocess
    from pathlib import Path

    src = _kapps_src()
    repo = src.git_root
    mine = f"ghcr.io/{_ghcr_user()}/"
    # grep the tree first: k-apps carries thousands of yamls and only the few
    # that name our registry are worth reading
    files = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "-F", mine, kapps_sha8,
         "--", src.prefix + "applications"], capture_output=True, text=True).stdout.split()
    wanted = {}
    for ref in files:
        f = ref.split(":", 1)[1] if ":" in ref else ref
        if not f.endswith((".yaml", ".yml")):
            continue
        body = subprocess.run(["git", "-C", str(repo), "show", f"{kapps_sha8}:{f}"],
                              capture_output=True, text=True).stdout
        # url and ref.tag live in the same OCIRepository doc; documents are
        # '---'-separated and an app dir can hold several of them
        for doc in body.split("\n---"):
            if mine not in doc:
                continue
            tag = re.search(r"^\s*tag:\s*['\"]?([^'\"\s]+)", doc, re.M)
            if not tag:
                continue
            t = tag.group(1)
            # tags are published as "<chart-name>-<version>"; recover the chart
            for chart in _CHART_TAG_OWNERS:
                if t.startswith(chart + "-"):
                    wanted[chart] = t
                    break
            else:
                wanted.setdefault(src.strip(f).split("/")[1], t)
    return wanted


# populated by _deliver_charts with the charts this run actually builds, so a
# k-apps tag can be attributed to the chart that produces it
_CHART_TAG_OWNERS: set = set()


def _deliver_charts(ctx, sha8: str, suffix: str = "", kapps_sha8: str = "") -> None:
    """Package every chart the commit touched, push it, and repoint the cluster.

    The charts repo is a change-set repo like any other: name a branch and the
    framework works out which charts moved (git diff vs the base), builds them
    and points the running cluster at YOUR builds. Before this, a chart change
    had no route to a cluster at all - k-apps only carries a REFERENCE to a
    chart, and nothing here could produce the thing it referenced.
    """
    changed = _changed_charts(sha8)
    if not changed:
        # A warning here is the vacuous green this framework exists to catch:
        # the scenario names a chart change, nothing is packaged or pushed, no
        # OCIRepository moves, and every assertion still passes against the
        # SHIPPED chart. Same reasoning as the unrouted-repo refusal.
        raise RuntimeError(
            f"charts@{sha8} changes no chart vs "
            f"{os.environ.get('E2E_CHARTS_BASE', 'origin/master')}, so this "
            "run would deliver nothing and still pass.\n"
            "  Charts are detected as directories under stable/ or staging/ "
            "with a changed file at depth 3 or more.\n"
            "  Either point charts@ at a commit that touches one, set "
            "E2E_CHARTS_BASE to the right base, or drop charts from changes:.")
    ctx.log.info(f"charts@{sha8} touched {len(changed)} chart(s): {', '.join(changed)}")
    _CHART_TAG_OWNERS.update(c.rsplit("/", 1)[-1] for c in changed)
    asked = _kapps_requested_chart_tags(kapps_sha8) if kapps_sha8 else {}
    for chart in changed:
        publish_chart(ctx, chart=chart, suffix=suffix or sha8, repoint=True)
        # k-apps syncs AFTER this and restores its own reference, so also
        # publish the tag it names - otherwise Flux ends up asking for a tag
        # that only ever existed in someone's shell history.
        want = asked.get(chart.rsplit("/", 1)[-1])
        if want:
            ctx.log.info(f"kommander-applications also asks for {want} - "
                         "publishing that tag so its reference resolves")
            publish_chart(ctx, chart=chart, tag=want, repoint=False)


def _pinned_worktree(repo: str, sha: str, repos_dir) -> "pathlib_Path":
    """A detached worktree of <repo> at <sha>, cached across runs.

    Same cache and mechanism _cli_for_changes uses for konvoy2, so a commit
    named in `changes:` compiles to the same binary no matter what branch the
    developer's checkout is sitting on.
    """
    import subprocess
    from pathlib import Path

    src = Path(repos_dir) / repo
    cache = Path.home() / ".cache/nkp-worktrees"
    wt = cache / f"{repo}-{sha}"
    if not (wt / "go.mod").exists():
        cache.mkdir(parents=True, exist_ok=True)
        # A worktree whose directory was deleted stays REGISTERED, and a plain
        # add then fails with "missing but already registered worktree" forever.
        # Prune first and force the add so a half-removed cache self-heals.
        subprocess.run(["git", "-C", str(src), "worktree", "prune"],
                       capture_output=True, text=True, timeout=60)
        r = subprocess.run(["git", "-C", str(src), "worktree", "add", "--force",
                            "--detach", str(wt), sha],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and not (wt / "go.mod").exists():
            raise RuntimeError(
                f"cannot pin {repo}@{sha} for the upgrade CLI build: "
                f"{r.stderr.strip()[:200]}")
    # konvoy2 does not compile from a bare checkout: pkg/capi/clusterclass
    # embeds defaultmanifests/*.yaml, which are generated and gitignored, so a
    # worktree has the embed directives but not the files. The create path gets
    # away with it because build-nkp-fast.sh runs goreleaser, whose hook
    # generates them; this plain `go build` has no such hook and would die with
    # "pattern defaultmanifests/...: no matching files found".
    embeds = wt / "pkg/capi/clusterclass/defaultmanifests"
    if repo == "konvoy2" and embeds.is_dir() and not list(embeds.glob("*.yaml")):
        raise RuntimeError(
            f"cannot build konvoy2@{sha} from a pinned worktree: "
            f"{embeds} holds no generated manifests, and they are gitignored so "
            "a worktree can never contain them. Check the konvoy2 checkout out "
            "at that commit and re-run - the working tree is then the pin - or "
            "deliver konvoy2 through create_cluster, whose builder runs the "
            "goreleaser hook that generates them.")
    return wt


def _cli_for_upgrade(ctx, comps, version: str, to_version_needs_cli: bool = True):
    """Build the CLI that PERFORMS an upgrade, stamped with the target version.

    `nkp upgrade kommander` reads its target from the binary, so an upgrade to
    a developer's build needs a CLI compiled with that version. The create-path
    shim deliberately routes kommander verbs to NKP_BIN - correct there, wrong
    here, because those are exactly the verbs under test.

    Builds konvoy2 and/or kommander-cli from the change-set commits with
    -ldflags gitVersion=<version>, and shims them as one `nkp`.
    """
    import subprocess
    from pathlib import Path

    wanted = {c.split("@")[0]: c.split("@", 1)[1] for c in (comps or [])
              if c.split("@")[0] in ("konvoy2", "kommander-cli")}
    # kommander-cli is a BUILD requirement, not a change requirement. The
    # target version of `nkp upgrade kommander` is the version compiled into
    # the binary, so upgrading to v2.18.1-mine needs a kommander binary stamped
    # with it - whether or not you changed a line of CLI code. Listing
    # kommander-cli in `changes:` just to get that build was wrong: `changes`
    # should name repos whose CODE you altered. So build it from whatever the
    # local checkout is on, unless the change-set pins a specific commit.
    if to_version_needs_cli and "kommander-cli" not in wanted:
        wanted["kommander-cli"] = "HEAD"
    if not wanted or ctx.config.dry_run:
        return None
    maj, mino = version.lstrip("v").split(".")[:2]
    V = "github.com/mesosphere/dkp-cli-runtime/core/cmd/version"
    ld = f"-X {V}.gitVersion={version} -X {V}.major={maj} -X {V}.minor={mino}"
    out = ctx.artifacts / f"upgrade-cli-{version}"
    out.mkdir(parents=True, exist_ok=True)
    repos_dir = Path(os.environ.get("NKP_REPOS_DIR", str(Path.home() / "Documents/nkp")))
    env = {k: v for k, v in os.environ.items() if k != "GOROOT"}
    built = []
    for repo, target, main in (("konvoy2", "konvoy", "./cmd/konvoy"),
                               ("kommander-cli", "kommander", ".")):
        if repo not in wanted:
            continue
        # Build the COMMIT the change-set resolved, not whatever the checkout
        # happens to be on. This used to compile repos_dir/<repo> directly
        # while logging "building <repo>@<sha>" - so a scenario pinning a
        # commit got a binary from the working tree and a log line claiming
        # otherwise. The create path pins konvoy2 with a detached worktree
        # (_cli_for_changes); the upgrade path did not. Found by audit
        # 2026-09-01. "HEAD" is the force-added kommander-cli case, where the
        # working tree IS the intent - say so rather than implying a pin.
        ref = wanted[repo]
        head = subprocess.run(["git", "-C", str(repos_dir / repo), "rev-parse",
                               "--short=8", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if ref == "HEAD":
            src = repos_dir / repo
            ctx.log.info(f"building {repo} from the working tree ({head}) as {version}")
        elif ref == head:
            # already sitting on the commit - the working tree IS the pin, and
            # using it avoids a worktree that may lack generated files
            src = repos_dir / repo
            ctx.log.info(f"building {repo}@{ref} as {version} (checkout is on it)")
        else:
            src = _pinned_worktree(repo, ref, repos_dir)
            ctx.log.info(f"building {repo}@{ref} as {version} (pinned worktree)")
        r = subprocess.run(["devbox", "run", "--", "go", "build", "-trimpath",
                            "-ldflags", ld, "-o", str(out / target), main],
                           cwd=str(src), env=env, capture_output=True,
                           text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"building {repo} as {version} failed:\n{r.stderr[-400:]}")
        built.append(target)
    if not built:
        return None
    # anything not built falls through to NKP_BIN, so a change-set naming only
    # konvoy2 still gets a real kommander binary
    ga = str(ctx.config.nkp_bin)
    shim = out / "nkp"
    body = SHIM_TEMPLATE.replace("VERSION", version)
    if "kommander" not in built:
        body = body.replace('"$D/kommander"', f'"{ga}"')
    if "konvoy" not in built:
        body = body.replace('exec "$D/konvoy"', f'exec "{ga}"')
    shim.write_text(body)
    shim.chmod(0o755)
    # Probe the BUILT kommander binary, not the shim. `nkp version` is not one
    # of the verbs the shim routes, so it falls through to konvoy - and when
    # konvoy was not part of this build that is the GA binary, which reports
    # the GA version. Trusting the shim here made upgrade_cluster believe the
    # freshly built CLI was still v2.18.0 and try to download a release for the
    # target instead of using what it had just compiled.
    if "kommander" in built:
        probe = subprocess.run([str(out / "kommander"), "version"],
                               capture_output=True, text=True, timeout=120)
        reported = probe.stdout.strip().splitlines()[:1]
        ctx.remember("upgrade_cli_version", version)
        ctx.log.info(f"upgrade CLI ready: {shim} (kommander reports {reported})")
    else:
        ctx.log.info(f"upgrade CLI ready: {shim}")
    ctx.remember("upgrade_cli", str(shim))
    return str(shim)


def _oci_tag_exists(repo: str, tag: str) -> bool:
    """Does ghcr already serve this tag? Anonymous, like the cluster does.

    GHCR answers 401 to an unauthenticated manifest GET even for public
    repositories, so the anonymous token dance is required - a bare curl
    returning 401 means nothing about visibility.
    """
    import json as _j
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
                f"https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io",
                timeout=30) as r:
            tok = _j.loads(r.read()).get("token", "")
    except Exception:
        return False
    req = urllib.request.Request(
        f"https://ghcr.io/v2/{repo}/manifests/{tag}", method="HEAD")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.oci.image.manifest.v1+json,"
                             "application/vnd.oci.image.index.v1+json,"
                             "application/vnd.docker.distribution.manifest.v2+json")
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return False
        raise
    except Exception:
        return False


def _kapps_declared_version(sha8: str) -> str:
    """The platform version a kommander-applications commit declares ITSELF to be.

    Every operator kustomization in the bundle is annotated
    ``managementplane.nkp.nutanix.com/version: "${kommanderChartVersion:=X}"``,
    and the SAME variable is the tag of the images those manifests run
    (``mesosphere/kommander2-core-installer:${kommanderChartVersion}``). X is
    therefore not a label that can be renamed: it is the release this tree is,
    and the only tag whose images exist.
    """
    import re
    import subprocess
    from pathlib import Path

    src = _kapps_src()
    body = subprocess.run(
        ["git", "-C", str(src.git_root), "show",
         f"{sha8}:{src.prefix}common/managementplane/flux-kustomization.yaml"],
        capture_output=True, text=True).stdout
    m = re.search(r"\$\{kommanderChartVersion:=([^}]+)\}", body)
    return m.group(1).strip() if m else ""


def _staged_kapps_repo(ctx, sha8: str, url: str) -> str:
    """A checkout of the change-set's k-apps with the OCI origin baked in.

    `nkp upgrade kommander` writes application definitions into the cluster's
    git from --kommander-applications-repository, and Flux applies them. That
    write includes common/managementplane/manifests/all.yaml - the
    ManagementPlane controller's own Deployment. Handing it the raw checkout
    therefore re-applies that Deployment WITHOUT KAPPS_OCI_URL, the controller
    restarts having lost the override, and the k-apps OCIRepository it then
    creates points back at ghcr.io/mesosphere.

    Live-caught 2026-09-01: the env was set at 01:36:55, the CLI's git write
    landed at 01:38:27, and the ManagementPlane step at 01:39:11 pulled the
    SHIPPED bundle. Two writers of the same file, one of them un-baked.

    So the CLI gets a staged copy carrying the same bake as the bundle. The
    developer's own checkout is never modified.
    """
    import io
    import shutil
    import subprocess
    import tarfile
    from pathlib import Path

    src = _kapps_src()
    dest = Path(ctx.artifacts) / "kapps-repo"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tree = f"{sha8}:{src.prefix.rstrip('/')}" if src.prefix else sha8
    tar = subprocess.run(["git", "-C", str(src.git_root), "archive", tree],
                         capture_output=True, timeout=300)
    if tar.returncode != 0:
        raise RuntimeError(f"git archive {sha8} failed: {tar.stderr.decode()[:200]}")
    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
        t.extractall(dest)
    _bake_kapps_oci_url(dest, url)
    return str(dest)


def _ensure_kapps_bundle(ctx, target: str, comps, applications_repository: str) -> str:
    """Make sure the version being upgraded to resolves to the RIGHT k-apps bundle.

    Two separate failures live here, both live-caught 2026-09-01:

    1. The ManagementPlane controller does not read the CLI's
       ``--kommander-applications-repository``. It resolves k-apps from
       ``ghcr.io/mesosphere/kommander-applications:<version>`` and blocks on it,
       so an unpublished version waits out a hardcoded 10-minute timeout and
       dies with "client rate limiter Wait returned an error".

    2. Even when the bundle exists, an upgrade REINSTALLS app definitions from
       it - quietly reverting anything the devloop route synced into the
       cluster's git. So a k-apps change in the change-set only survives an
       upgrade if the bundle itself is yours.

    Both are fixed the same way: publish this change-set's kommander-applications
    as the bundle for the target version and point the controller at it.
    """
    if ctx.config.dry_run or not target:
        return ""
    kapps = _kapps_sha8(comps)
    mesosphere = _KAPPS_OCI_DEFAULT.replace("oci://ghcr.io/", "")

    if not kapps:
        if _oci_tag_exists(mesosphere, target):
            return ""  # a real release upgrading to shipped apps: nothing to do
        raise RuntimeError(
            f"upgrade target {target} has no published kommander-applications "
            f"bundle ({_KAPPS_OCI_DEFAULT}:{target} does not exist), and the "
            "change-set does not name kommander-applications, so this framework "
            "has nothing to publish as one.\n"
            "  The ManagementPlane controller resolves k-apps from that OCI tag "
            "and blocks until Flux can pull it - it does NOT read "
            "--kommander-applications-repository.\n"
            "  Either add kommander-applications@<branch> (2.18 line) or kommander@<branch> "
            "(main line, where the tree lives inside the kommander repo) to changes:, "
            "or pick a to_version that is actually published.")

    # The bundle's own declared version is also the image tag its operators
    # run. Renaming it would point the cluster at images nobody built.
    declared = _kapps_declared_version(kapps)
    if declared and declared != target:
        raise RuntimeError(
            f"upgrade target {target} does not match the version "
            f"kommander-applications@{kapps} declares ({declared}).\n"
            f"  In the bundle, 'managementplane.nkp.nutanix.com/version' and the "
            f"operator image tags are the SAME variable "
            f"(${{kommanderChartVersion:={declared}}}). The ManagementPlane step "
            f"requires that annotation to equal the target exactly, and the tag "
            f"has to be one whose images exist - "
            f"mesosphere/kommander2-core-installer:{target} does not.\n"
            f"  So an arbitrary version name is not possible without also "
            f"publishing the whole kommander image set under it. Use "
            f"to_version: {declared} - that is what this branch IS, and your "
            f"chart, k-apps and controller changes still ride on it.")

    # Publish OUR bundle even when mesosphere has one for this version: the
    # upgrade installs app definitions from whatever the bundle holds, so
    # using theirs would silently discard the k-apps change under test.
    ctx.log.info(f"publishing kommander-applications@{kapps} as the {target} "
                 "bundle - an upgrade reinstalls apps from the bundle, so the "
                 "k-apps change only survives if the bundle is yours")
    base = publish_kapps_artifact(ctx, version=target, sha8=kapps)
    # the CLI writes the same operator manifests into the cluster's git, so it
    # has to write the baked ones or it undoes the override
    staged = _staged_kapps_repo(ctx, kapps, base)
    ctx.log.info(f"the upgrade CLI will write app definitions from {staged} "
                 "(your checkout, with the bundle origin baked in)")
    return staged


def _autorecord_upgrade_baseline(ctx, verify: bool) -> None:
    """Snapshot what the scenario did not snapshot itself.

    An explicit ``record_*`` earlier in the scenario is a deliberate baseline
    and must never be overwritten - but a snapshot THIS step took on a previous
    upgrade must be, or a scenario that upgrades twice measures its second
    upgrade against the state before its first (kommander-upgrade-existing
    does exactly that).
    """
    if not verify or ctx.config.dry_run:
        return
    if not ctx.recall("carriers_before") or ctx.recall("carriers_before_auto"):
        record_platform_carriers(ctx)
        ctx.remember("carriers_before_auto", True)
    if not ctx.recall("app_versions_before") or ctx.recall("app_versions_before_auto"):
        record_app_versions(ctx)
        ctx.remember("app_versions_before_auto", True)
    if not ctx.recall("chart_refs_before") or ctx.recall("chart_refs_before_auto"):
        record_chart_refs(ctx)
        ctx.remember("chart_refs_before_auto", True)



def _refuse_offline_federation(ctx) -> None:
    """Fail in seconds, by name, instead of letting the CLI time out in 40 minutes.

    `nkp upgrade kommander` waits on federated propagation to every
    KubeFedCluster. A member that is Offline/ClusterNotReachable (2026-09-07:
    a detached workload cluster whose kubefed member was left in a frozen
    template) can never acknowledge, and the CLI reports only "platform
    upgrade did not complete successfully" after its full timeout. Naming the
    member here is the difference between a 5-second diagnosis and an hour.
    """
    if ctx.config.dry_run:
        return
    doc = ctx.kube.json(["get", "kubefedcluster", "-A"]) or {}
    bad = []
    for m in doc.get("items", []):
        conds = {c.get("type"): c for c in (m.get("status", {}) or {}).get("conditions", [])}
        off = conds.get("Offline", {}).get("status") == "True"
        ready = conds.get("Ready", {}).get("status") == "True"
        if off or (conds and not ready):
            ep = (m.get("spec", {}) or {}).get("apiEndpoint", "")
            why = (conds.get("Offline") or conds.get("Ready") or {}).get("reason", "")
            bad.append(f"{m['metadata']['name']} ({ep}: {why})")
    if bad:
        raise RuntimeError(
            "upgrade_kommander refused: federated member(s) not Ready - the upgrade "
            "would wait on propagation to them and time out: " + "; ".join(bad) +
            ". If the cluster no longer exists, delete its KubeFedCluster (and "
            "secretRef, and <name>-* namespace) in kube-federation-system.")


def _unresolvable_pins(ctx) -> list[str]:
    """Platform AppDeployments pinned to a ClusterApp version that does not exist.

    Root-caused 2026-09-07 after three 40-minute `nkp upgrade kommander`
    timeouts: a `-dev` to_version makes the CLI pull the NIGHTLY operator
    images (docker.io/mesosphere/kommander2-core-installer:v2.18.1-dev, built
    from release-2.18), and that operator pins each platform app to the
    version ITS build knows. The bundle came from a branch forked earlier.
    When release-2.18 bumped kommander-ui 17.234.32 -> 17.234.34 (k-apps
    #5105, 2026-09-02 03:23Z) the pin pointed at a ClusterApp the bundle did
    not provide, and the CLI waited on it until its timeout. Nothing in the
    cluster is unhealthy while that happens, which is why it hid.
    """
    have = {i["metadata"]["name"] for i in (ctx.kube.json(["get", "clusterapp"]) or {}).get("items", [])}
    bad = []
    for ad in (ctx.kube.json(["get", "appdeployment", "-n", "kommander"]) or {}).get("items", []):
        name = ad["metadata"]["name"]
        for ov in (ad.get("spec", {}) or {}).get("clusterConfigOverrides") or []:
            v = ov.get("appVersion")
            if v and f"{name}-{v}" not in have:
                near = sorted(h[len(name) + 1:] for h in have if h.startswith(name + "-"))
                bad.append(f"{name} pinned to {v}, bundle provides {near or 'nothing'}")
    return bad


def _pin_drift_watch(ctx, to_version: str, kubeconfig):
    """While the CLI runs, kill it the moment a platform pin cannot resolve.

    Returns a threading.Event; ``.reason`` carries the diagnosis once set.
    The CLI is killed by its own argv (the kubeconfig path is unique per
    run), because shell.run owns the Popen and this has to work from outside.
    """
    import subprocess as _sp
    import threading
    found = threading.Event()
    found.reason = ""
    if ctx.config.dry_run:
        return found

    def loop():
        seen = 0
        while not found.is_set():
            time.sleep(20)
            try:
                bad = _unresolvable_pins(ctx)
            except Exception:  # noqa: BLE001 - an API blip is not drift
                continue
            if not bad:
                seen = 0
                continue
            seen += 1
            if seen < 3:                  # ~1 min of consistent drift, then act
                continue
            found.reason = (
                f"platform pin(s) cannot resolve - the {to_version} operator images (a "
                "floating nightly tag built from the release branch) pin versions this "
                "bundle does not carry: " + "; ".join(bad) + ". Fix: merge/rebase the "
                "kommander-applications branch onto its release branch so the bundle "
                "carries what the operator pins, then re-run.")
            found.set()
            ctx.log.error(found.reason)
            _sp.run(["pkill", "-f", f"upgrade kommander --kubeconfig {kubeconfig}"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

    threading.Thread(target=loop, daemon=True).start()
    return found

@step("upgrade_kommander")
def upgrade_platform(ctx, *, applications_repository: str = "",
                     binary_from: str = "", to_version: str = "",
                     changes: list | None = None, verify: bool = True,
                     timeout: str = "60m") -> None:
    """`nkp upgrade kommander` - move the PLATFORM to a version, and say which.

    Named for the verb it runs. It is step one of the three-step customer
    order; ``upgrade_workspace`` is step two and ``upgrade_nodes``
    (`nkp upgrade cluster nutanix`) is step three. The old name
    ``upgrade_cluster`` was wrong twice over: it did not upgrade a cluster,
    and it read like the verb that does.

    ``to_version`` is the scenario's declaration of what the cluster should
    look like afterwards. It is not a flag on the command - `upgrade kommander`
    has none. The target is `version.GetVersion().GitVersion` of the binary
    that runs it (kommander-cli cmd/upgrade/kommander/kommander.go:72,124), and
    EVERY downstream carrier follows from that one value: the k-apps tarball
    URL, KommanderCore.spec.version, ManagementPlane.spec.version, the
    OCIRepository tag, the operators' Kustomization annotations, and the
    NKP Version a user is shown. So ``to_version`` does three concrete things:

      1. preflights the artifacts, so a version that does not exist fails in
         seconds instead of wedging the run 40 minutes in;
      2. picks the binary - if the CLI in hand does not report that version,
         the released CLI for it is fetched and used, because otherwise the
         upgrade silently goes somewhere else;
      3. records the target so ``assert_platform_upgraded`` can hold every
         carrier to it rather than to whatever the cluster happened to reach.

    ``changes`` delivers a developer's commits to the cluster BEFORE the
    upgrade runs, the same way ``create_cluster: {changes: [...]}`` does on the
    create path. The ordering is the whole point of an upgrade test: land the
    change on the old cluster, upgrade over it, then prove the upgrade did not
    put the shipped code back.

    ``verify`` (default true) records the before-state and runs the full set of
    post-upgrade assertions - every version carrier, the content that carries
    the release, the operator roll-out, the applications, and every delivered
    change still running. It is on by default because an upgrade nobody
    asserted is the framework's worst failure mode: `upgrade kommander` returns
    early and exits 0 when the cluster is already at the CLI's version, so a
    scenario that forgets an assertion reports a green run that upgraded
    nothing. Set ``verify: false`` only to assert something unusual by hand.

    ``binary_from`` names a CLI stored earlier (``changeset_cli`` from a
    change-set build, ``baseline_bin`` from resolve_baseline); the default is
    the CLI under test, NKP_BIN. When given, it WINS over to_version's fetch -
    testing your own build is the point - but a disagreement is reported, not
    hidden, because the assertions afterwards will be held to the real target.
    """
    comps = _resolve_changes(ctx, changes) if changes else []
    # Snapshot BEFORE anything this step does. Delivering first and recording
    # after made the baseline include the very delivery it was meant to
    # measure: the istio chart ref was already the new tag when it was
    # recorded, so assert_chart_refs_changed compared the new tag against
    # itself and saw no movement - a green run that proved nothing, or a
    # confusing failure. "Before" means before the delivery AND the upgrade.
    _refuse_offline_federation(ctx)
    _autorecord_upgrade_baseline(ctx, verify)
    if comps:
        ctx.remember("changeset_components", comps)
        ctx.log.info(f"delivering {len(comps)} change(s) BEFORE the upgrade, so "
                     "the upgrade has something to try to revert")
        _deliver_changes(ctx, comps, on_upgrade=True)
    elif changes:
        ctx.log.info("no change-set for this run - the CLI under test (NKP_BIN) "
                     "is what is being exercised")
    cli = ctx.nkp
    chosen = str(ctx.config.nkp_bin)
    if binary_from:
        path = ctx.recall(binary_from) or ""
        if not path:
            raise RuntimeError(
                f"upgrade_cluster: binary_from={binary_from!r} has no value - "
                "the step that builds it must run first")
        if not path.startswith("<"):  # "<...>" is the dry-run placeholder
            cli = ctx.nkp.with_binary(path)
            chosen = path
            ctx.log.info(f"upgrading with {binary_from} CLI: {path}")

    if to_version and not to_version.startswith("v"):
        to_version = "v" + to_version
    # A change-set naming konvoy2 or kommander-cli means "upgrade with MY
    # build": compile it stamped with the target version and use it. Without
    # this the developer has to build and pass a binary by hand, and the
    # create-path shim would send kommander verbs to NKP_BIN anyway.
    self_built = False
    if to_version and not binary_from:
        built = _cli_for_upgrade(ctx, comps, to_version)
        if built:
            cli = ctx.nkp.with_binary(built)
            chosen = built
            self_built = bool(ctx.recall("upgrade_cli_version"))
    if self_built:
        # we compiled it with this exact version a moment ago and verified the
        # binary reports it; re-probing through the shim would read konvoy
        actual = ctx.recall("upgrade_cli_version")
    else:
        actual = "" if ctx.config.dry_run else _cli_kommander_version(chosen)
    fetched = False
    if to_version and actual and actual != to_version:
        if binary_from:
            # Never silently swap out the developer's own build.
            raise AssertionError(
                f"upgrade_cluster: to_version={to_version} but the CLI from "
                f"{binary_from} reports kommander {actual}.\n"
                f"  `upgrade kommander` takes its target from the binary, so this "
                f"run would upgrade to {actual}, not {to_version}, and every "
                f"version assertion afterwards would fail.\n"
                f"  Either build that CLI with -ldflags "
                f"'-X .../version.gitVersion={to_version}', or drop binary_from "
                f"and let the framework fetch the {to_version} release, or set "
                f"to_version: {actual}.")
        ctx.log.info(f"CLI in hand reports kommander {actual}; fetching the "
                     f"{to_version} release so the target is what the scenario "
                     f"declares")
        preflight_version_artifacts(ctx, version=to_version, need_cli=True)
        cli = ctx.nkp.with_binary(_fetch_release_cli(ctx, to_version))
        actual = to_version
        fetched = True

    # The target is a fact about the binary whether or not the scenario names
    # it, so record and preflight it either way: a CLI whose own version has no
    # published k-apps (every `go build` without release ldflags reports
    # v0.0.0-dev, and a goreleaser snapshot reports <base>-dev-SNAPSHOT-<sha>)
    # cannot upgrade anything, and that is worth knowing now rather than at
    # minute 40.
    target = to_version or actual
    if target:
        ctx.remember("upgrade_target_version", target)
        # A scenario written for the 2.18 line passes <NKP_REPOS_DIR>/kommander-applications;
        # on the main line that directory does not exist because the tree lives inside
        # the kommander repository. Fall back to wherever the resolver found it.
        if applications_repository and not os.path.isdir(applications_repository):
            ctx.log.info(f"applications_repository {applications_repository} does not exist - "
                         f"using the applications tree at {_kapps_src().dir}")
            applications_repository = str(_kapps_src().dir)
        if applications_repository:
            # the preflight asks downloads.d2iq.com for this version's k-apps
            # tarball, which is the right question ONLY when that is where the
            # apps come from. With an explicit repository - a local checkout or
            # a git ref, which is how a custom build supplies its own apps -
            # the tarball is never fetched and its absence means nothing.
            ctx.log.info(f"apps come from {applications_repository} - skipping "
                         "the published-tarball preflight")
        elif not fetched:
            preflight_version_artifacts(ctx, version=target)
        # ...but --kommander-applications-repository only feeds the CLI's OWN
        # steps. The ManagementPlane controller resolves k-apps from an OCI
        # bundle keyed by version, and blocks on it. So a local repository is
        # NOT sufficient for a custom version: the bundle has to exist too.
        _staged = _ensure_kapps_bundle(ctx, target, comps, applications_repository)
        if _staged:
            applications_repository = _staged
        ctx.log.info(f"upgrade target: {target} "
                     f"({'declared by the scenario' if to_version else 'from the CLI binary'})"
                     " - every version carrier will be held to it")
    elif not ctx.config.dry_run:
        ctx.log.warn("could not read the CLI's kommander version; the upgrade "
                     "target cannot be preflighted or asserted")

    # the upgrade re-pulls the docker.io kommander charts, so it needs the
    # same authenticated hold the claim path uses
    with _dockerhub_hold(ctx):
        _drift = _pin_drift_watch(ctx, to_version or "the target", ctx.kubeconfig)
        try:
            cli.upgrade_platform(
                ctx.kubeconfig,
                applications_repository=applications_repository,
                timeout_minutes=max(1, parse_duration(timeout) // 60),
            )
        except Exception as _exc:  # noqa: BLE001
            if _drift.is_set():
                raise RuntimeError(_drift.reason) from _exc
            raise
        if _drift.is_set():
            raise RuntimeError(_drift.reason)

        if not verify or ctx.config.dry_run:
            return
        ctx.log.info("verifying the upgrade (verify: false turns this off)")
        before = ctx.recall("carriers_before") or {}
        started_at = before.get("C2  kommandercore.status") or ""
        moved = bool(target) and started_at != target
        if not moved and started_at:
            ctx.log.warn(
                f"the cluster was already at {started_at} before this upgrade - "
                "`upgrade kommander` returns early and exits 0 in that case, so "
                "this run proves nothing about upgrading. Application movement is "
                "not asserted.")
        assert_platform_upgraded(ctx)
        assert_kapps_collection_synced(ctx)
        assert_platform_operators_at_version(ctx)
        assert_platform_apps_moved(ctx, require_movement=moved)
        # if this run published its own k-apps bundle, the cluster must still be
        # resolving apps from it - see assert_kapps_bundle_source for why
        assert_kapps_bundle_source(ctx)
        # An upgrade that leaves the platform broken is a failed upgrade, not a
        # passed one with follow-up steps a scenario might forget to write.
        wait_pods_healthy(ctx, namespace="kommander", timeout="30m")
        assert_all_helmreleases_ready(ctx, timeout="25m")
        # Anything delivered on this cluster must still be running afterwards: an
        # upgrade re-renders every HelmRelease from the new release's directory,
        # and "did that quietly restore the shipped image?" is the question an
        # upgrade test exists to answer.
        for comp in ctx.recall("changeset_components") or []:
            repo = comp.split("@", 1)[0]
            if repo in REPO_DELIVERY:
                assert_change_running(ctx, repo=repo, strict=False, timeout="20m")


#: Every object that must carry the platform version after an upgrade, with the
#: writer that owns it. Traced from source 2026-08-31; `assert_platform_changed`
#: reads only C2, so a cluster can report an upgrade that most of these never
#: saw - including status.platformVersion, which is the "NKP Version" a user
#: actually sees (nkpcluster_types.go:232,277).
#:
#: 2.17 and 2.18 have DIFFERENT carrier sets - release-2.17 has no
#: ManagementPlane or NKPCluster at all - so presence is part of the check, not
#: an assumption. Missing-before is fine; missing-after when it existed before,
#: or a value that disagrees, is not.
_PLATFORM_CARRIERS = (
    ("C1  kommandercore.spec",            ("kommandercore", None, "kommander-core"), "spec.version"),
    ("C2  kommandercore.status",          ("kommandercore", None, "kommander-core"), "status.version"),
    ("C3  managementplane.spec",          ("managementplane", None, "nkp-platform"), "spec.version"),
    ("C4  managementplane.status",        ("managementplane", None, "nkp-platform"), "status.version"),
    ("C5  nkpcluster.spec",               ("nkpcluster", "kommander", None), "spec.version"),
    ("C6  nkpcluster.embedded-platform",  ("nkpcluster", "kommander", None),
        "spec.kommanderCluster.spec.platform.version"),
    ("C7  nkpcluster.status (user-visible)", ("nkpcluster", "kommander", None), "status.platformVersion"),
    ("C8  kommandercluster.spec",         ("kommandercluster", "kommander", None), "spec.platform.version"),
    ("C9  kommandercluster.status",       ("kommandercluster", "kommander", None), "status.platform.version"),
)


def _dig(obj, path):
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj if isinstance(obj, str) and obj else None


def _list_lenient(ctx, kind: str, namespace: str = "") -> list:
    """`kubectl get <kind>` that tolerates the kind not existing at all.

    Two reasons this cannot use ``ctx.kube.json``:
      * it runs with check=True, so on a 2.17 cluster - which has no
        ManagementPlane or NKPCluster CRD - the whole step dies with
        `the server doesn't have a resource type "managementplane"` instead of
        recording "absent", which is exactly the fact the caller wants;
      * a NAMED get cannot be combined with -A, so carriers are always listed
        and then matched by name in python.
    """
    args = ["kubectl", "--kubeconfig", str(ctx.kubeconfig), "get", kind,
            "-o", "json"]
    args += ["-n", namespace] if namespace else ["-A"]
    res = run(args, ctx.log, check=False, quiet=True, timeout=120,
              dry_run=ctx.config.dry_run)
    out = (res.stdout or "").strip()
    if res.code != 0 or not out:
        return []
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    if start < 0:
        return []
    try:
        data = json.loads(out[start:])
    except ValueError:
        return []
    return data.get("items", []) if isinstance(data, dict) else []


def _platform_carriers(ctx) -> dict:
    """Read every version carrier. Value is None when the carrier is ABSENT."""
    if ctx.config.dry_run:
        return {}
    listed: dict = {}
    out = {}
    for label, (kind, ns, name), path in _PLATFORM_CARRIERS:
        key = (kind, ns)
        if key not in listed:
            listed[key] = _list_lenient(ctx, kind, ns or "")
        items = [i for i in listed[key] if isinstance(i, dict) and i.get("metadata")]
        picked = None
        if name:
            picked = next((i for i in items if i["metadata"].get("name") == name), None)
        else:
            # the HOST cluster is the one labelled kommander.d2iq.io/host=true;
            # fall back to a lone item when the label is absent (2.17 shapes).
            picked = next((i for i in items
                           if (i["metadata"].get("labels") or {})
                           .get("kommander.d2iq.io/host") == "true"), None)
            if picked is None and len(items) == 1:
                picked = items[0]
        out[label] = _dig(picked, path) if picked else None
    # C10 - the kommander HelmRelease chart tag. Not a CRD field, easiest to
    # miss, and it is what the NEXT upgrade's preflight reads
    # (kommander-cli/pkg/upgrade/preflight.go).
    hrs = [i for i in _list_lenient(ctx, "helmrelease", "kommander")
           if i.get("metadata", {}).get("name") == "kommander"]
    hr = hrs[0] if hrs else None
    tag = _dig(hr, "spec.chart.spec.version")
    if not tag and hr:
        ref = _dig(hr, "spec.chartRef.name")
        if ref:
            oci = [i for i in _list_lenient(ctx, "ocirepository", "kommander")
                   if i.get("metadata", {}).get("name") == ref]
            tag = _dig(oci[0], "spec.ref.tag") if oci else None
    out["C10 kommander HelmRelease chart tag"] = tag
    return out


@step("upgrade_workspace")
def upgrade_workspace(ctx, *, workspace: str = "@workload", timeout: str = "60m",
                      verify: bool = True) -> None:
    """`nkp upgrade workspace` - step 2 of the customer's three-step order.

    The order NKP documents is `upgrade kommander` -> `upgrade workspace` (each
    workspace) -> `upgrade cluster nutanix`. ``upgrade_cluster`` is only the
    first of those: it moves the MANAGEMENT cluster's platform. Platform apps on
    an attached workload cluster stay where they are until this runs.

    It deliberately takes **no** ``changes:``. The change-set was already
    delivered by ``upgrade_cluster`` - images pushed, charts published, k-apps
    synced - and this verb moves a workspace's platform apps up to the versions
    the management cluster now has. Repeating the change-set here would imply a
    second delivery that does not happen.
    """
    ns = ""
    if workspace == "@workload":
        ns = ctx.recall("workload_namespace") or ""
        if not ns and not ctx.config.dry_run:
            raise RuntimeError(
                "upgrade_workspace: @workload needs a prior create_workload_cluster")
        workspace = ""

    if ctx.config.dry_run:
        ctx.log.info(f"DRY RUN: nkp upgrade workspace {workspace or f'<name-of-{ns}>'}")
        return

    # `nkp upgrade workspace` takes the workspace NAME; a workload cluster is
    # remembered by its NAMESPACE, so resolve one to the other rather than
    # assuming they are spelled the same (they are not: the default workspace is
    # `default-workspace` in namespace `kommander-default-workspace`).
    if not workspace:
        found = ""
        for ws in _list_lenient(ctx, "workspace", ""):
            if (ws.get("spec", {}) or {}).get("namespaceName") == ns:
                found = ws.get("metadata", {}).get("name", "")
                break
        if not found:
            raise RuntimeError(
                f"upgrade_workspace: no Workspace has spec.namespaceName={ns!r}")
        workspace = found
        ctx.log.info(f"workspace namespace {ns} -> workspace {workspace}")

    ctx.log.info(f"upgrading workspace {workspace} - platform apps move up to "
                 "the management cluster's versions")
    ctx.nkp.upgrade_workspace(ctx.kubeconfig, workspace,
                              timeout_minutes=max(1, parse_duration(timeout) // 60))
    ctx.remember("upgraded_workspace", workspace)

    if verify:
        ctx.log.info("workspace upgraded; assert on the workload cluster to prove it")



@step("record_platform_carriers")
def record_platform_carriers(ctx, *, key: str = "carriers_before") -> None:
    """Snapshot every platform-version carrier before an upgrade."""
    if ctx.config.dry_run:
        return
    snap = _platform_carriers(ctx)
    ctx.remember(key, snap)
    present = {k: v for k, v in snap.items() if v}
    ctx.log.info(f"platform carriers before: {len(present)}/{len(snap)} present")
    for k, v in sorted(present.items()):
        ctx.log.info(f"    {k} = {v}")


@step("assert_platform_upgraded")
def assert_platform_upgraded(ctx, *, to_version: str = "", was: str = "carriers_before",
                             timeout: str = "15m") -> None:
    """Every version carrier agrees on the new version - or name the ones that do not.

    `assert_platform_changed` compares ONE carrier (KommanderCore.status). A
    cluster whose KommanderCore moved while NKPCluster.status.platformVersion,
    the embedded KommanderCluster platform version, or the kommander
    HelmRelease tag stayed behind is a PARTIAL upgrade that reports success -
    and status.platformVersion is the value a user is shown.

    Presence-aware by necessity: a 2.17 baseline has no ManagementPlane or
    NKPCluster, so those carriers appear only after the upgrade. Missing-before
    is fine; disagreeing, or vanishing after having existed, is not.
    """
    if ctx.config.dry_run:
        return
    before = ctx.recall(was) or {}
    target = to_version or ctx.recall("upgrade_target_version") or ""
    if not target:
        # No declared target: infer it from the carrier the CLI writes first.
        target = (_platform_carriers(ctx).get("C1  kommandercore.spec") or "")
        if not target:
            raise RuntimeError(
                "assert_platform_upgraded: no target version - pass to_version: "
                "or run upgrade_cluster with to_version so the target is recorded")
        ctx.log.info(f"no to_version given; inferring target {target} from KommanderCore.spec")

    def agreed():
        now = _platform_carriers(ctx)
        return all(v == target for k, v in now.items()
                   if v is not None or before.get(k) is not None)

    try:
        wait_for(agreed, ctx.log, what=f"all platform carriers to reach {target}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception:
        pass  # fall through to the detailed report below

    now = _platform_carriers(ctx)
    mismatched, vanished, skipped, ok = [], [], [], []
    for label in now:
        val, prev = now.get(label), before.get(label)
        if val is None and prev is None:
            skipped.append(label)
        elif val is None:
            vanished.append(f"{label}: was {prev!r}, now ABSENT")
        elif val != target:
            mismatched.append(f"{label}: {val!r} (expected {target!r})")
        else:
            ok.append(label)

    for label in skipped:
        ctx.log.info(f"    skipped (absent before and after): {label}")
    if not ok and not mismatched and not vanished:
        # VACUOUS GREEN GUARD. Every carrier absent is not "nothing to check" -
        # it is "nothing was visible". No real NKP cluster has none of these:
        # the kommander HelmRelease chart tag and KommanderCore exist on 2.17
        # and 2.18 alike. Reading zero means the kubeconfig, the namespace or
        # RBAC is wrong, and without this the step reports success for a
        # cluster it never actually looked at. Caught by the kind fixture
        # 2026-08-31, where the bare cluster PASSED.
        raise AssertionError(
            f"assert_platform_upgraded saw NONE of the {len(now)} version "
            f"carriers on this cluster.\n  Every NKP cluster has at least "
            f"KommanderCore and the kommander HelmRelease, so this is not a "
            f"version-line difference - the kubeconfig points somewhere else, "
            f"or the CRDs/RBAC are not what this step can read.\n  Refusing "
            f"to report an upgrade to {target} that was never observed.")
    if mismatched or vanished:
        raise AssertionError(
            f"PARTIAL upgrade to {target}: {len(ok)} carrier(s) agree, "
            f"{len(mismatched) + len(vanished)} do not.\n  "
            + "\n  ".join(mismatched + vanished)
            + "\n\nThe cluster reports an upgrade that these objects never saw. "
              "status.platformVersion is what a user is shown, and the kommander "
              "HelmRelease tag is what the NEXT upgrade's preflight reads.")
    ctx.log.info(f"all {len(ok)} present platform carrier(s) agree on {target} "
                 f"({len(skipped)} absent on this version line)")


def _rfc1123(version: str) -> str:
    """kommander's own name mangling (common/pkg/helpers/helpers.go ToRFC1123)."""
    v = version.lower()
    for ch in "+._":
        v = v.replace(ch, "-")
    v = re.sub(r"[^a-z0-9-]+", "-", v)
    v = re.sub(r"(^-+|-+$)", "", v)
    return v[:64]


#: The two Flux OCIRepositories a platform version is actually MADE of. Both
#: are created per version, named with the version in them, and both must be
#: Ready at the target tag - the version strings are written by controllers,
#: but these carry the CONTENT, so a cluster can report v2.19.0 while still
#: running v2.18.0 manifests. Verified 2026-08-31 against
#: kommander/common/pkg/oci/kapps.go:16 + kapps_repository.go:37-57 and
#: common/pkg/installer/platformversionartifact/operation.go:98-116.
_VERSION_OCI_REPOS = (
    ("kommander-applications-{rfc}",
     "oci://ghcr.io/mesosphere/kommander-applications",
     "the k-apps manifest tree the platform operators deploy from"),
    ("platform-version-{rfc}",
     "oci://ghcr.io/mesosphere/kommander-applications/collection",
     "the catalog collection that resolves each app to its version for this release"),
)


@step("assert_kapps_collection_synced")
def assert_kapps_collection_synced(ctx, *, to_version: str = "",
                                   timeout: str = "15m") -> None:
    """The new version's application content actually arrived.

    This is the check the string comparisons cannot make. Every version field
    on the cluster can say v2.19.0 while the platform still runs the previous
    release's manifests: the fields are written by controllers, the content
    comes from two Flux OCIRepositories created per version. If either is
    missing, stuck at the old tag, or not Ready, the version number is a label
    on an unchanged cluster.

    Absent on a pre-collection layout (2.17 has neither), so a complete absence
    is reported and skipped; anything present is held to the target tag.
    """
    if ctx.config.dry_run:
        return
    target = to_version or ctx.recall("upgrade_target_version") or ""
    if not target:
        raise RuntimeError("assert_kapps_collection_synced needs to_version: or "
                           "a prior upgrade_cluster that recorded a target")
    rfc = _rfc1123(target)
    wanted = {name.format(rfc=rfc): (url, why) for name, url, why in _VERSION_OCI_REPOS}
    state: dict = {}

    def look() -> bool:
        state.clear()
        found = {r["metadata"]["name"]: r
                 for r in _list_lenient(ctx, "ocirepository", "kommander")}
        for name in wanted:
            repo = found.get(name)
            if repo is None:
                state[name] = ("ABSENT", "", "")
                continue
            spec = repo.get("spec", {})
            ready = next((c.get("status") for c in
                          repo.get("status", {}).get("conditions", [])
                          if c.get("type") == "Ready"), "?")
            state[name] = ((spec.get("ref") or {}).get("tag", ""),
                           ready, spec.get("url", ""))
        present = {n: v for n, v in state.items() if v[0] != "ABSENT"}
        return bool(present) and all(t == target and r == "True"
                                     for t, r, _ in present.values())

    try:
        wait_for(look, ctx.log, what=f"version OCIRepositories at tag {target}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception:
        pass  # report precisely below
    look()

    if all(v[0] == "ABSENT" for v in state.values()):
        others = sorted(r["metadata"]["name"]
                        for r in _list_lenient(ctx, "ocirepository", "kommander")
                        if "kommander-applications" in r.get("spec", {}).get("url", ""))
        ctx.log.info(f"no per-version k-apps OCIRepository on this cluster "
                     f"(pre-collection layout); k-apps repos present: {others or 'none'}")
        return
    bad = []
    for name, (tag, ready, url) in sorted(state.items()):
        if tag == "ABSENT":
            # Which of the two a given version line creates has NOT been
            # observed on every layout here, so a missing one is reported
            # rather than failed - a stale tag or a not-Ready repo is the
            # failure this check exists for, and neither is weakened by it.
            ctx.log.warn(f"{name} not present - {wanted[name][1]}")
        elif tag != target:
            bad.append(f"{name}: tag={tag!r} (expected {target!r})")
        elif ready != "True":
            bad.append(f"{name}: Ready={ready}")
        elif url != wanted[name][0]:
            ctx.log.warn(f"{name} url is {url!r}, expected {wanted[name][0]!r} "
                         "(mirror, or kommander changed the registry)")
    if bad:
        raise AssertionError(
            f"the content for {target} did not arrive:\n  " + "\n  ".join(bad)
            + "\n\nThe version FIELDS can be correct while the application "
              "definitions are still the previous release's - these two "
              "OCIRepositories are what tells those apart.")
    ctx.log.info(f"both version OCIRepositories for {target} are Ready at the "
                 f"target tag ({', '.join(sorted(state))})")


#: The Flux Kustomizations the management plane rolls forward, one per platform
#: operator, each stamped with the version it was deployed at. Names and the
#: annotation key are verified against
#: kommander/managementplane/pkg/controllers/operators.go:89-132 and
#: managementplane/pkg/step/flux_kustomization.go:29.
_PLATFORM_OPERATOR_KUSTOMIZATIONS = (
    "management-plane-operator",
    "nkpcluster-operator",
    "upgrade-plan-operator",
    "release-operator",
    "kommander-operator",
    "logging-stack-operator",
)
_KUSTOMIZATION_VERSION_ANNOTATION = "managementplane.nkp.nutanix.com/version"


@step("assert_platform_operators_at_version")
def assert_platform_operators_at_version(ctx, *, to_version: str = "",
                                         timeout: str = "20m") -> None:
    """Every platform operator was actually rolled to the new version.

    The management plane upgrades six operators by committing their manifests
    into the cluster's git and waiting for each Flux Kustomization to come back
    stamped with the target version and Ready. That stamp is the difference
    between "the ManagementPlane says v2.19.0" and "the controllers running
    this cluster are v2.19.0 controllers".

    Absent on a 2.17-shaped cluster - there is no ManagementPlane there - so a
    complete absence is reported and skipped, while a PARTIAL set (some
    operators moved, some did not) fails: that is a half-finished upgrade.
    Version comparison mirrors kustomizationVersionMatches: a -SNAPSHOT
    prerelease on the annotation is stripped before comparing.
    """
    if ctx.config.dry_run:
        return
    target = to_version or ctx.recall("upgrade_target_version") or ""
    if not target:
        raise RuntimeError("assert_platform_operators_at_version needs to_version: "
                           "or a prior upgrade_cluster with to_version")

    def stamp(k) -> str:
        meta = k.get("metadata", {})
        return ((meta.get("annotations") or {}).get(_KUSTOMIZATION_VERSION_ANNOTATION)
                or (meta.get("labels") or {}).get(_KUSTOMIZATION_VERSION_ANNOTATION)
                or "")

    def matches(current: str) -> bool:
        return current == target or target == current.split("-SNAPSHOT")[0]

    state: dict = {}

    def all_at_version() -> bool:
        state.clear()
        found = {k["metadata"]["name"]: k
                 for k in _list_lenient(ctx, "kustomization", "kommander")
                 if k.get("metadata", {}).get("name") in _PLATFORM_OPERATOR_KUSTOMIZATIONS}
        for name in _PLATFORM_OPERATOR_KUSTOMIZATIONS:
            k = found.get(name)
            if k is None:
                state[name] = ("ABSENT", "")
                continue
            ready = next((c.get("status") for c in
                          k.get("status", {}).get("conditions", [])
                          if c.get("type") == "Ready"), "?")
            state[name] = (stamp(k) or "<no version stamp>", ready)
        present = {n: v for n, v in state.items() if v[0] != "ABSENT"}
        return bool(present) and all(matches(v) and r == "True"
                                     for v, r in present.values())

    try:
        wait_for(all_at_version, ctx.log,
                 what=f"platform operator Kustomizations at {target}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception:
        pass  # report precisely below

    all_at_version()
    absent = [n for n, (v, _) in state.items() if v == "ABSENT"]
    if len(absent) == len(_PLATFORM_OPERATOR_KUSTOMIZATIONS):
        ctx.log.info("no platform-operator Kustomizations on this cluster - "
                     "pre-ManagementPlane layout (2.17), check skipped")
        return
    for name in absent:
        # A release line that simply does not ship one of these would look
        # identical to a half-finished upgrade, so absence is reported and the
        # check holds every operator that IS present to the target.
        ctx.log.warn(f"platform operator Kustomization {name} not present")
    bad = [f"{n}: version={v!r} ready={r}" for n, (v, r) in sorted(state.items())
           if v != "ABSENT" and (not matches(v) or r != "True")]
    if bad:
        raise AssertionError(
            f"platform operators did not all reach {target}:\n  "
            + "\n  ".join(bad)
            + "\n\nA cluster whose version FIELDS moved but whose operator "
              "Kustomizations did not is running the old controllers under the "
              "new version number.")
    ctx.log.info(f"all {len(state)} platform operator Kustomizations are at "
                 f"{target} and Ready")


def _app_id(app: dict) -> str:
    """kommander's own appID derivation (app_deployment_types.go:175-199).

    appRef.appID when set, else appRef.name with its trailing semver stripped -
    the ClusterApp for an app version is named `<appID>-<version>`
    (common/pkg/installer/certmanageradoption/operation.go:170).
    """
    ref = app.get("spec", {}).get("appRef") or {}
    if ref.get("appID"):
        return ref["appID"]
    name = ref.get("name", "")
    if not name:
        return ""
    m = re.search(r"(\d+\.\d+\.\d+(-[\w.\-]+)?(\+[\w.\-]+)?)$", name)
    if m:
        return name[:m.start()].rstrip("-")
    return name.rsplit("-", 1)[0] if "-" in name else name


def _app_versions(ctx) -> dict:
    """AppDeployment (ns/name) -> (appID, resolved appVersion).

    appVersion on clusterConfigOverrides is what the deployment resolves
    against - it is written per cluster by the platform-version defaulter from
    the catalog collection, so it is the field that moves on an upgrade
    (kommander clientapis/.../customdefaults/appdeployment_platformversion_defaults.go).
    """
    out = {}
    for app in _list_lenient(ctx, "appdeployment"):
        meta = app["metadata"]
        key = f"{meta.get('namespace')}/{meta['name']}"
        version = ""
        for ov in app.get("spec", {}).get("clusterConfigOverrides") or []:
            if ov.get("appVersion"):
                version = ov["appVersion"]
                break
        out[key] = (_app_id(app), version)
    return out


@step("record_app_versions")
def record_app_versions(ctx, *, key: str = "app_versions_before") -> None:
    """Snapshot every AppDeployment's resolved appVersion before an upgrade."""
    if ctx.config.dry_run:
        return
    snap = _app_versions(ctx)
    ctx.remember(key, snap)
    withver = sum(1 for _, v in snap.values() if v)
    ctx.log.info(f"recorded appVersion for {len(snap)} AppDeployment(s) "
                 f"({withver} carry one)")


@step("assert_platform_apps_moved")
def assert_platform_apps_moved(ctx, *, was: str = "app_versions_before",
                               require_movement: bool = True) -> None:
    """The platform applications themselves moved, and point at real ClusterApps.

    The last place a version bump has to reach is the workloads. Two facts,
    both checkable:

      * at least one AppDeployment's resolved appVersion changed - otherwise
        the upgrade renamed the platform without moving it;
      * every resolved appVersion has a ClusterApp `<appID>-<version>` to
        resolve to - otherwise it moved to something that was never published,
        which surfaces later as an app that will not deploy.

    ``require_movement: false`` keeps the second half on a cluster where no
    movement is expected (a same-version reconcile).
    """
    if ctx.config.dry_run:
        return
    before = ctx.recall(was) or {}
    now = _app_versions(ctx)
    if before:
        # recall() round-trips through JSON, so tuples come back as lists.
        prev = {k: tuple(v) for k, v in before.items()}
        changed = {k: (prev.get(k, ("", ""))[1], v[1])
                   for k, v in now.items() if prev.get(k, ("", ""))[1] != v[1]}
        if not changed and require_movement:
            raise AssertionError(
                f"no AppDeployment changed appVersion across the upgrade "
                f"({len(now)} inspected). The version fields moved but the "
                f"platform applications did not - the apps are still the "
                f"previous release's.")
        ctx.log.info(f"{len(changed)} AppDeployment(s) moved appVersion:")
        for key, (old, new) in sorted(changed.items())[:10]:
            ctx.log.info(f"    {key}: {old or '<none>'} -> {new or '<none>'}")
    else:
        ctx.log.warn(f"no {was} snapshot - checking resolvability only; add "
                     "record_app_versions before the upgrade to assert movement")

    known = {c["metadata"]["name"]
             for c in _list_lenient(ctx, "clusterapp", "kommander")}
    resolvable = {k: v for k, (a, v) in now.items() if v}
    if not known:
        # Same vacuous-green guard: no ClusterApps AND AppDeployments that
        # claim versions means the read failed, not that the cluster is bare.
        if resolvable:
            raise AssertionError(
                f"{len(resolvable)} AppDeployment(s) carry an appVersion but "
                f"this cluster reports NO ClusterApps at all - the read failed "
                f"rather than the cluster being empty.")
        ctx.log.info("no ClusterApps and no resolved appVersions - "
                     "resolvability check skipped")
        return
    dangling = sorted({f"{k} -> {app_id}-{v}"
                       for k, (app_id, v) in now.items()
                       if v and app_id and f"{app_id}-{v}" not in known})
    if dangling:
        raise AssertionError(
            f"{len(dangling)} AppDeployment(s) resolve to a ClusterApp that does "
            f"not exist:\n  " + "\n  ".join(dangling[:10])
            + "\n\nThe platform points at app versions that were never "
              "published for this release.")
    resolved = sum(1 for _, v in now.values() if v)
    ctx.log.info(f"{resolved} resolved appVersion(s) all have a matching "
                 f"ClusterApp ({len(known)} published)")


def _chart_refs(ctx) -> dict:
    """Every app's chart source, as {OCIRepository name: url@tag}.

    An app directory in kommander-applications holds a REFERENCE to a chart -
    an OCIRepository url plus ref.tag - not the chart itself. So "the chart
    changed" is observable here, and nowhere in the AppDeployment.

    Identified by what the object IS, not by what its url looks like. The
    previous test - "charts" appears somewhere in the url - was blind to
    exactly the charts this framework publishes: publish_chart pushes to
    ghcr.io/<you>/nkp-dev, which contains no such word, so a repointed app
    dropped out of the snapshot entirely and assert_chart_refs_changed
    reported "no chart reference changed" while looking at an empty set
    (live-caught 2026-09-01). It also quietly missed shipped charts whose
    registry is not named that way - kommander-chart and
    kommander-appmanagement-chart on docker.io, and both traefik ones - so the
    kommander chart tag the docs call a version carrier was never in the
    snapshot at all.

    Only the two per-version BUNDLE repositories are excluded; those carry the
    release's manifests, not a chart.
    """
    out = {}
    for r in _list_lenient(ctx, "ocirepository"):
        meta, spec = r.get("metadata", {}), r.get("spec", {})
        name = meta.get("name", "")
        url = spec.get("url", "")
        if not url or name.startswith(("kommander-applications-", "platform-version-")):
            continue
        tag = (spec.get("ref") or {}).get("tag", "")
        out[f"{meta.get('namespace')}/{name}"] = f"{url}:{tag}"
    return out


@step("record_chart_refs")
def record_chart_refs(ctx, *, key: str = "chart_refs_before") -> None:
    """Snapshot which chart every app is pointing at, before an upgrade."""
    if ctx.config.dry_run:
        return
    snap = _chart_refs(ctx)
    ctx.remember(key, snap)
    ctx.log.info(f"recorded {len(snap)} chart reference(s)")
    for k, v in sorted(snap.items())[:8]:
        ctx.log.info(f"    {k} -> {v}")


@step("assert_chart_refs_changed")
def assert_chart_refs_changed(ctx, *, was: str = "chart_refs_before",
                              app: str = "", timeout: str = "10m") -> None:
    """The app is now pulling a DIFFERENT chart than before.

    This is the only assertion that can see a chart change: the AppDeployment,
    the HelmRelease values and the rendered workload can all be identical while
    the chart underneath is a different build. ``app:`` narrows it to one
    application; without it, any chart moving satisfies the check.
    """
    if ctx.config.dry_run:
        return
    before = ctx.recall(was) or {}
    if not before:
        raise RuntimeError(f"assert_chart_refs_changed needs a prior record_chart_refs ({was})")
    seen = {}

    def moved() -> bool:
        seen.clear()
        seen.update(_chart_refs(ctx))
        for k, v in seen.items():
            if app and app not in k:
                continue
            if before.get(k) and before[k] != v:
                return True
        return False

    try:
        wait_for(moved, ctx.log, what=f"chart reference for {app or 'any app'} to change",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        rel = {k: v for k, v in seen.items() if not app or app in k}
        raise AssertionError(
            f"no chart reference changed for {app or 'any app'}.\n"
            f"  before: { {k: v for k, v in before.items() if not app or app in k} }\n"
            f"  now:    {rel}\n"
            "  The app is still pulling the same chart, so a chart change did "
            "not reach this cluster - check the OCIRepository tag in the app's "
            "kommander-applications directory.") from exc
    for k, v in sorted(seen.items()):
        if before.get(k) and before[k] != v and (not app or app in k):
            ctx.log.info(f"chart moved: {k}  {before[k]} -> {v}")


@step("assert_controller_log")
def assert_controller_log(ctx, *, deployment: str, contains: str,
                          namespace: str = "kommander", since: str = "30m",
                          timeout: str = "10m") -> None:
    """A controller change is VISIBLE in its logs.

    assert_change_running proves the image ref is what you built; this proves
    the code in it actually ran. For a demo that is the difference between
    "the tag changed" and "my change executed".
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would look for {contains!r} in {namespace}/{deployment} logs")
        return
    last = [""]

    def found() -> bool:
        res = ctx.kube._run(  # noqa: SLF001
            ["logs", f"deployment/{deployment}", "-n", namespace,
             "--all-containers", f"--since={since}", "--tail=4000"],
            check=False, quiet=True)
        last[0] = res.stdout or ""
        return contains in last[0]

    try:
        wait_for(found, ctx.log, what=f"{contains!r} in {namespace}/{deployment} logs",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        tail = "\n    ".join(last[0].splitlines()[-6:]) or "(no log output)"
        # A startup-only line is invisible once the container log has rotated past
        # it (2026-09-07: a 5-day-old pod, 0 restarts, still running the change;
        # the marker was long gone). Say so, instead of "never appeared".
        _started = [(p["metadata"]["name"], (p.get("status") or {}).get("startTime", ""))
                    for p in (ctx.kube.json(["get", "pods", "-n", namespace]) or {}).get("items", [])
                    if p["metadata"]["name"].startswith(deployment + "-")
                    and "webhook" not in p["metadata"]["name"]]
        _hint = ("" if not _started else
                 f" Pod(s) {_started} predate the --since={since} window: a line logged only at\n"
                 f"  startup has rotated out of the retained log. Restart the workload\n"
                 f"  (restart_workload) before asserting, or assert the running image instead.")

        raise AssertionError(
            f"{contains!r} never appeared in {namespace}/{deployment} logs.{_hint}\n"
            f"  last lines:\n    {tail}") from exc
    ctx.log.info(f"controller log carries {contains!r} - the change ran")


@step("assert_app_metadata")
def assert_app_metadata(ctx, *, app: str, field: str = "description",
                        contains: str = "", namespace: str = "kommander",
                        timeout: str = "10m") -> None:
    """A kommander-applications metadata change reached the cluster.

    App metadata lands on the ClusterApp the AppDeployment resolves to, so this
    is where a metadata.yaml edit becomes observable.
    """
    if ctx.config.dry_run:
        return
    last = [""]

    def has() -> bool:
        for c in _list_lenient(ctx, "clusterapp", namespace):
            name = c.get("metadata", {}).get("name", "")
            if not name.startswith(app):
                continue
            # kommander writes metadata.yaml onto the ClusterApp as
            # apps.kommander.d2iq.io/<kebab-field> annotations (display-name,
            # description, category, scope, ...). Live-verified 2026-09-08; the
            # spec.<field> and bare-annotation lookups never matched anything.
            kebab = "".join("-" + ch.lower() if ch.isupper() else ch for ch in field)
            ann = (c.get("metadata", {}) or {}).get("annotations") or {}
            val = (ann.get(f"apps.kommander.d2iq.io/{kebab}") or ann.get(f"apps.kommander.d2iq.io/{field}")
                   or _dig(c, f"spec.{field}") or ann.get(field) or "")
            last[0] = val
            if contains and contains in val:
                return True
        return False

    try:
        wait_for(has, ctx.log, what=f"ClusterApp {app} {field} to contain {contains!r}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        raise AssertionError(
            f"ClusterApp for {app!r}: {field} does not contain {contains!r} "
            f"(last saw {last[0]!r}). The kommander-applications metadata change "
            "did not reach this cluster.") from exc
    ctx.log.info(f"ClusterApp {app} {field} carries {contains!r}")


def _charts_dir() -> "pathlib_Path":
    from pathlib import Path

    return Path(os.environ.get("NKP_REPOS_DIR",
                str(Path.home() / "Documents/nkp"))) / "charts"


def _changed_charts(sha: str) -> list:
    """Which charts a commit in the charts repo touches, vs the base branch."""
    import subprocess

    repo = _charts_dir()
    base = _diff_base(repo, sha, os.environ.get("E2E_CHARTS_BASE", "origin/master"))
    r = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", base, sha],
                       capture_output=True, text=True, timeout=60)
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split("/")
        if len(parts) > 2 and parts[0] in ("stable", "staging"):
            out.add(f"{parts[0]}/{parts[1]}")
    return sorted(out)


def _oci_push_chart(tgz, repo: str, tag: str, user: str, token: str, log,
                    *, config_media: str = "application/vnd.cncf.helm.config.v1+json",
                    layer_media: str = "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                    config_obj=None, annotations=None) -> str:
    """Push a packaged chart as a TAG on an existing repository.

    `helm push oci://host/ns/pkg` always appends the chart name, creating a new
    GHCR package - and every new package starts PRIVATE, needing a manual
    visibility flip in the web UI before cluster nodes (which pull
    anonymously) can fetch it. There is no API for that flip on user-scoped
    packages, so each new chart would cost a manual step.

    Pushing the chart as a tag on a repository that is ALREADY public avoids
    that entirely. This is the plain OCI distribution flow - two blobs then a
    manifest - with helm's own media types, which is exactly what helm itself
    uploads; Flux's OCIRepository fetches url+tag and does not care that the
    repository is not named after the chart.
    """
    import base64
    import hashlib
    import json as _j
    import urllib.error
    import urllib.request

    auth = "Bearer " + base64.b64encode(token.encode()).decode()
    host = "https://ghcr.io"

    def req(method, url, data=None, headers=None):
        r = urllib.request.Request(url, method=method, data=data)
        r.add_header("Authorization", auth)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        return urllib.request.urlopen(r, timeout=180)

    def put_blob(payload: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        try:  # already there?
            req("HEAD", f"{host}/v2/{repo}/blobs/{digest}")
            return digest
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        with req("POST", f"{host}/v2/{repo}/blobs/uploads/", data=b"",
                 headers={"Content-Length": "0"}) as resp:
            loc = resp.headers["Location"]
        if loc.startswith("/"):
            loc = host + loc
        sep = "&" if "?" in loc else "?"
        req("PUT", f"{loc}{sep}digest={digest}", data=payload,
            headers={"Content-Type": "application/octet-stream"})
        return digest

    chart_bytes = pathlib_Path(tgz).read_bytes()
    layer_digest = put_blob(chart_bytes)
    cfg = _j.dumps(config_obj if config_obj is not None
                   else {"name": tag.split("-")[0], "version": tag}).encode()
    cfg_digest = put_blob(cfg)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": config_media,
                   "digest": cfg_digest, "size": len(cfg)},
        "layers": [{"mediaType": layer_media,
                    "digest": layer_digest, "size": len(chart_bytes)}],
    }
    if annotations:
        manifest["annotations"] = annotations
    body = _j.dumps(manifest).encode()
    req("PUT", f"{host}/v2/{repo}/manifests/{tag}", data=body,
        headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"})
    ref = f"oci://ghcr.io/{repo}"
    log(f"pushed {pathlib_Path(tgz).name} -> {ref}:{tag}")
    return ref


#: Where the ManagementPlane controller pulls the k-apps bundle from, and the
#: env var that overrides it (kommander managementplane/cmd/main.go:130 ->
#: DefaultStepsBuilder -> EnsureKAppsOCIRepositoryStep). The URL itself is a
#: hardcoded constant (common/pkg/oci/kapps.go:16), so this env var is the
#: ONLY supported way to point a cluster at a k-apps bundle you built.
_KAPPS_OCI_ENV = "KAPPS_OCI_URL"
_KAPPS_OCI_DEFAULT = "oci://ghcr.io/mesosphere/kommander-applications"
_MP_CONTROLLER = "nkp-management-plane-controller-manager"


def _kapps_artifact_tree(sha8: str, dest) -> int:
    """Materialise the k-apps tree that BELONGS in the published bundle.

    The repo decides, not this code: `.include-airgapped` lists the top-level
    directories that ship, `.exclude-airgapped` the app directories that do
    not. Verified against the real artifact - rebuilding v2.18.0 this way
    reproduces the released bundle's file list exactly (543 files).
    """
    import shutil
    import subprocess
    import tarfile
    from pathlib import Path

    src = _kapps_src()
    dest = Path(dest)
    work = dest.parent / (dest.name + "-src")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    tree = f"{sha8}:{src.prefix.rstrip('/')}" if src.prefix else sha8
    tar = subprocess.run(["git", "-C", str(src.git_root), "archive", tree],
                         capture_output=True, timeout=300)
    if tar.returncode != 0:
        raise RuntimeError(f"git archive {sha8} failed: {tar.stderr.decode()[:200]}")
    import io
    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
        t.extractall(work)

    inc = [ln.strip().lstrip("./") for ln in
           (work / ".include-airgapped").read_text().splitlines() if ln.strip()]
    exc = [ln.strip() for ln in
           (work / ".exclude-airgapped").read_text().splitlines() if ln.strip()]
    if not inc:
        raise RuntimeError(".include-airgapped is empty - cannot decide what to publish")
    for e in exc:
        shutil.rmtree(work / e, ignore_errors=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for d in inc:
        if (work / d).exists():
            shutil.copytree(work / d, dest / d)
    n = sum(1 for _ in dest.rglob("*") if _.is_file())
    shutil.rmtree(work, ignore_errors=True)
    return n


def _bake_kapps_oci_url(dest, url: str) -> None:
    """Make the bundle say where it came from, durably.

    Setting KAPPS_OCI_URL with `kubectl set env` only survives until the
    upgrade re-applies the management-plane operator from the bundle - which it
    does as one of its own steps, wiping the env. The controller then rewrites
    the OCIRepository back to ghcr.io/mesosphere on its next reconcile (server-
    side apply, ForceOwnership) and Flux silently re-pulls the SHIPPED apps.
    The upgrade still goes green, having installed none of the k-apps change
    under test. Live-caught 2026-09-01, after the ManagementPlane reported
    v2.18.1-dev reconciled from mesosphere's bundle rather than ours.

    So the operator manifests inside OUR bundle carry the env themselves: once
    Flux applies them, the cluster keeps resolving k-apps from here.

    Only the one Deployment document is re-serialised; every other document in
    the file is left byte-for-byte alone.
    """
    import yaml
    from pathlib import Path

    f = Path(dest) / "common/managementplane/manifests/all.yaml"
    if not f.exists():
        raise RuntimeError(f"k-apps bundle has no {f.name} to carry KAPPS_OCI_URL")
    docs = f.read_text().split("\n---")
    out, patched = [], 0
    for chunk in docs:
        try:
            d = yaml.safe_load(chunk)
        except Exception:
            out.append(chunk)
            continue
        if not (isinstance(d, dict) and d.get("kind") == "Deployment"
                and d.get("metadata", {}).get("name") == _MP_CONTROLLER):
            out.append(chunk)
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            env = [e for e in (c.get("env") or []) if e.get("name") != _KAPPS_OCI_ENV]
            env.append({"name": _KAPPS_OCI_ENV, "value": url})
            c["env"] = env
            patched += 1
        lead = "\n" if chunk.startswith("\n") else ""
        out.append(lead + yaml.safe_dump(d, default_flow_style=False, sort_keys=False))
    if not patched:
        raise RuntimeError(
            f"could not find Deployment/{_MP_CONTROLLER} in the k-apps bundle - "
            "refusing to publish a bundle that would silently fall back to the "
            "shipped applications")
    f.write_text("\n---".join(out))


@step("publish_kapps_artifact")
def publish_kapps_artifact(ctx, *, version: str, sha8: str = "",
                           package: str = "nkp-dev", point_cluster: bool = True) -> str:
    """Publish YOUR kommander-applications as the bundle a version resolves to.

    This is what makes `upgrade_cluster` to a custom version possible at all.
    In 2.18 the ManagementPlane controller does not read the CLI's
    ``--kommander-applications-repository``: it creates an OCIRepository
    ``kommander-applications-<rfc1123 version>`` pointing at
    ``oci://ghcr.io/mesosphere/kommander-applications:<version>`` and BLOCKS
    until Flux can pull it (managementplane/pkg/step/ensure_oci_repository.go).
    For a version nobody published, that tag does not exist, so the upgrade
    waits out ManagementPlane's hardcoded 10-minute WaitTimeout and dies with
    "client rate limiter Wait returned an error" - a message that says nothing
    about the real cause. Live-caught 2026-09-01.

    The URL is a compile-time constant, but the controller reads
    ``KAPPS_OCI_URL`` from its own environment first, so pointing the cluster
    at a bundle you built is supported without patching an image.

    Pushed as a TAG on the already-public package for the same reason
    publish_chart is: a new GHCR package is private, and the cluster pulls
    anonymously.
    """
    from pathlib import Path

    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would publish k-apps bundle for {version}")
        return ""
    user, token = _ghcr_user(), os.environ.get("GHCR_TOKEN", "")
    if not token:
        raise RuntimeError(
            "publish_kapps_artifact needs GHCR_TOKEN to push - export it or "
            "put it in ~/.nkp-dev-registry.env")
    repo = f"{user}/{package}"
    staged = Path(ctx.artifacts) / "kapps-artifact"
    n = _kapps_artifact_tree(sha8, staged)
    base = f"oci://ghcr.io/{repo}"
    # the bundle carries its own origin, or the upgrade reverts to the shipped
    # applications the moment it re-applies the operator
    _bake_kapps_oci_url(staged, base)
    ctx.log.info(f"packaging kommander-applications@{sha8} as the {version} "
                 f"bundle ({n} files, resolving k-apps from {base})")

    import tarfile
    tgz = Path(ctx.artifacts) / f"kapps-{version}.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        for child in sorted(staged.iterdir()):
            t.add(child, arcname=child.name)
    _oci_push_chart(
        tgz, repo, version, user, token, ctx.log.info,
        config_media="application/vnd.oci.empty.v1+json",
        layer_media="application/vnd.oci.image.layer.v1.tar+gzip",
        config_obj={},
        annotations={"org.opencontainers.image.source": f"kommander-applications@{sha8}"})
    ctx.remember("kapps_bundle_url", base)
    if point_cluster:
        point_cluster_at_kapps_bundle(ctx, url=base)
    return base


def _kapps_rfc1123(version: str) -> str:
    """The OCIRepository name suffix a version becomes (helpers.ToRFC1123)."""
    import re

    return re.sub(r"[^a-z0-9-]", "-", version.lower()).strip("-")


@step("assert_kapps_bundle_source")
def assert_kapps_bundle_source(ctx, *, version: str = "", url: str = "") -> None:
    """The apps this cluster resolved came from YOUR bundle, not the shipped one.

    Without this the worst outcome of the k-apps route is invisible: the
    ManagementPlane reports the target version reconciled, every app is
    healthy, and the bundle it actually pulled was ghcr.io/mesosphere's -
    so the kommander-applications change under test was never installed.
    Exactly that happened on 2026-09-01 when the operator re-apply wiped
    KAPPS_OCI_URL.
    """
    if ctx.config.dry_run:
        return
    version = version or ctx.recall("upgrade_target_version") or ""
    url = url or ctx.recall("kapps_bundle_url") or ""
    if not version or not url:
        return  # no bundle was published for this run; nothing to hold
    name = f"kommander-applications-{_kapps_rfc1123(version)}"
    obj = ctx.kube.json(["get", "ocirepository", name, "-n", "kommander"]) or {}
    got = (obj.get("spec") or {}).get("url", "")
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in ((obj.get("status") or {}).get("conditions") or []))
    if got != url or not ready:
        raise AssertionError(
            f"the cluster is not resolving kommander-applications from your "
            f"bundle.\n  OCIRepository {name}\n    want url: {url}\n"
            f"     got url: {got or '<missing>'}   Ready={ready}\n"
            "  The upgrade re-applies the management-plane operator from the "
            "bundle, which wipes KAPPS_OCI_URL unless the bundle itself carries "
            "it; the controller then rewrites this OCIRepository back to "
            "ghcr.io/mesosphere and Flux re-pulls the SHIPPED applications. "
            "The upgrade would look green having installed none of your k-apps "
            "change.")
    ctx.log.info(f"k-apps bundle confirmed: {name} -> {got}")


@step("point_cluster_at_kapps_bundle")
def point_cluster_at_kapps_bundle(ctx, *, url: str) -> None:
    """Set KAPPS_OCI_URL on the ManagementPlane controller and wait for it.

    Without the rollout wait the upgrade races the old pod, which would
    re-apply the mesosphere URL over ours - the OCIRepository is written with
    server-side apply and ForceOwnership on every reconcile.
    """
    if ctx.config.dry_run:
        return
    ctx.kube._run(["set", "env", f"deploy/{_MP_CONTROLLER}", "-n", "kommander",
                   f"{_KAPPS_OCI_ENV}={url}"], check=True)
    ctx.log.info(f"ManagementPlane controller now resolves k-apps from {url}")
    ctx.kube._run(["rollout", "status", f"deploy/{_MP_CONTROLLER}",
                   "-n", "kommander", "--timeout=180s"], check=True)


@step("publish_chart")
def publish_chart(ctx, *, chart: str, app: str = "", suffix: str = "",
                  namespace: str = "kommander", repoint: bool = True,
                  package: str = "nkp-dev", tag: str = "") -> None:
    """Package a chart, push it to YOUR ghcr, and point the cluster at it.

    This is the manual chart loop, automated: `helm package` the chart, `helm
    push` it to your own registry, then rewrite the app's OCIRepository so Flux
    pulls YOUR build instead of the shipped one.

    It exists because an app directory in kommander-applications holds only a
    REFERENCE to a chart (an OCIRepository url + ref.tag). The k-apps delivery
    route can move that reference, but nothing in this framework could produce
    the chart it points at - so a chart change had no path to a cluster at all.

    ``chart`` is a path under the charts repo ("staging/istio") or an absolute
    path. ``suffix`` is appended to the chart's own version so the pushed tag
    can never collide with a real release; it defaults to the run's cluster
    name. ``repoint: false`` publishes without touching the cluster, for when
    the k-apps branch already carries the new url.

    ONE-TIME: a newly created ghcr package is PRIVATE. The cluster pulls
    anonymously, so the package must be flipped public once in the GitHub UI -
    the same flip nkp-dev needed. Until then the pull fails with 401.
    """
    import subprocess
    from pathlib import Path

    src = Path(chart)
    if not src.is_absolute():
        src = _charts_dir() / chart
    if not (src / "Chart.yaml").exists():
        raise RuntimeError(f"publish_chart: no Chart.yaml under {src}")

    meta = {}
    for line in (src / "Chart.yaml").read_text().splitlines():
        for k in ("name", "version"):
            if line.startswith(k + ":"):
                meta[k] = line.split(":", 1)[1].strip().strip('"\'')
    name, base_ver = meta.get("name", src.name), meta.get("version", "0.0.0")
    user = _ghcr_user()
    # a tag on an EXISTING public package, so no new private package is created
    oci_repo = f"{user}/{package}"
    repo_url = f"oci://ghcr.io/{oci_repo}"
    if tag:
        # caller names the published tag outright (used to satisfy a reference
        # kommander-applications already carries). helm derives the chart
        # version from the tag, so strip the "<name>-" the tag begins with.
        oci_tag = tag
        chart_ver = tag[len(name) + 1:] if tag.startswith(name + "-") else tag
    else:
        chart_ver = f"{base_ver}-{suffix or (ctx.cluster_name or 'dev')}"[:63]
        oci_tag = f"{name}-{chart_ver}"
    tag = chart_ver

    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would publish {name} {tag} to {repo_url} "
                     f"and repoint {app or name}")
        return

    out = ctx.artifacts / "charts"
    out.mkdir(parents=True, exist_ok=True)
    ctx.log.info(f"packaging {src.name} as {name}-{tag}")
    r = subprocess.run(["helm", "package", str(src), "--version", tag,
                        "--destination", str(out)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"helm package failed: {r.stderr.strip()[:300]}")
    tgz = out / f"{name}-{tag}.tgz"
    if not tgz.exists():
        raise RuntimeError(f"helm package produced no {tgz.name} (got {list(out.iterdir())})")

    token = os.environ.get("GHCR_TOKEN", "")
    if not token:
        raise RuntimeError(
            "publish_chart needs GHCR_TOKEN to push - export it or put it in "
            "~/.nkp-dev-registry.env")
    _oci_push_chart(tgz, oci_repo, oci_tag, user, token, ctx.log.info)
    pushed, tag = repo_url, oci_tag
    ctx.log.info(f"published {pushed}:{tag}")
    ctx.remember(f"chart_{name}", f"{pushed}:{tag}")

    if not repoint:
        ctx.log.info("repoint: false - put this url and tag in the k-apps branch yourself")
        return
    target_app = app or name
    hit = 0
    # An app enabled only on an ATTACHED cluster has its OCIRepository in that
    # cluster's workspace namespace, not in `kommander`. Searching only the
    # management workspace then reports "no OCIRepository to repoint" for an app
    # that is deployed and healthy - live-caught 2026-09-02 with istio-helm on a
    # workload cluster. Search every namespace, and say which ones were hit.
    seen_ns = set()
    for r_ in _list_lenient(ctx, "ocirepository", ""):
        rname = r_["metadata"]["name"]
        rns = r_["metadata"].get("namespace") or namespace
        url = r_.get("spec", {}).get("url", "")
        if target_app not in rname and not url.rstrip("/").endswith("/" + target_app):
            continue
        patch = _json.dumps({"spec": {"url": pushed, "ref": {"tag": tag}}})
        ctx.kube._run(["patch", "ocirepository", rname, "-n", rns,
                       "--type=merge", "-p", patch], quiet=True, check=False)
        ctx.kube._run(["annotate", "ocirepository", rname, "-n", rns,
                       f"reconcile.fluxcd.io/requestedAt={int(time.time())}",
                       "--overwrite"], quiet=True, check=False)
        ctx.log.info(f"repointed ocirepository/{rns}/{rname} -> {pushed}:{tag}")
        seen_ns.add(rns)
        hit += 1
    if not hit:
        # An app enabled ONLY on an attached cluster has its OCIRepositories on
        # THAT cluster (created by its own flux from the federated content), and
        # they carry the version in the name - `istio-helm-1.25.0-gateway`, not
        # `istio-helm-gateway`. Verified live 2026-09-02: zero on the management
        # cluster, five on the workload cluster. The repoint is a
        # management-cluster shortcut; the k-apps route is what actually carries
        # the reference there, and it has already published this tag. So a miss
        # is only fatal when the app is not deployed ANYWHERE.
        # target_app is the CHART name (istio-helm-gateway); the AppDeployment is
        # named for the APP (istio-helm). Match by prefix, not equality - the
        # first cut of this compared the two directly and reported "no
        # AppDeployment" for an app that was deployed and converged.
        deployed = [a for a in _list_lenient(ctx, "appdeployment", "")
                    if (lambda n: n and (n == target_app or target_app.startswith(n + "-")))(
                        a.get("metadata", {}).get("name", ""))]
        if deployed:
            where = ", ".join(sorted(
                a["metadata"].get("namespace", "?") for a in deployed))
            ctx.log.warn(
                f"no OCIRepository for {target_app!r} on the management cluster to "
                f"repoint - the app is deployed in {where}, so its chart reference "
                "lives on the attached cluster and k-apps carries it. "
                f"Chart published as {pushed}:{tag}.")
            return
        raise AssertionError(
            f"published the chart but found no OCIRepository for {target_app!r} in "
            "ANY namespace, and no AppDeployment for it either - deploy the app "
            "first.")
    ctx.log.info(f"repointed {hit} OCIRepository(ies) for {target_app} "
                 f"in {', '.join(sorted(seen_ns))}")


@step("assert_platform_changed")
def assert_platform_changed(ctx, *, was: str = "platform_before") -> None:
    """Fail if the platform version did not move - i.e. the upgrade no-opped."""
    if ctx.config.dry_run:
        return
    before, after = ctx.recall(was), ctx.nkp.platform_version(ctx.kubeconfig)
    ctx.log.info(f"platform version {before} -> {after}")
    if before and before == after:
        raise AssertionError(f"platform version did not change (still {after})")


@step("assert_platform_version_unchanged")
def assert_platform_version_unchanged(ctx, *, was: str = "platform_before") -> None:
    """CONTAINMENT: a node roll must not move the platform version.

    The mirror of assert_platform_changed. Rolling nodes is a kubernetes-layer
    operation; if the platform version moves too, the blast radius is wider
    than the matrix claims and the two upgrade paths are not separable.
    """
    if ctx.config.dry_run:
        return
    before, after = ctx.recall(was), ctx.nkp.platform_version(ctx.kubeconfig)
    if before and after and before != after:
        raise AssertionError(
            f"containment FAILED: the node roll also moved the platform "
            f"version ({before} -> {after}) - a kubernetes-layer upgrade is "
            "not supposed to touch the platform")
    ctx.log.info(f"containment verified: platform version still {after or before}")


@step("upgrade_catalog_app")
def upgrade_catalog_app(ctx, *, app: str, to_version: str = "",
                        to_version_from: str = "", workspace: str = "") -> None:
    """Upgrade one catalog application.

    ``to_version_from`` names a value stored earlier by ``record_app_version``,
    which is how a rollback targets whatever happened to be installed before -
    a version nobody can hard-code in the file.
    """
    target = to_version or (ctx.recall(to_version_from) if to_version_from else "")
    if not target and not ctx.config.dry_run:
        raise RuntimeError(
            f"upgrade_catalog_app({app}): no version to upgrade to - set "
            "to_version, or to_version_from pointing at a record_app_version key"
        )
    ctx.nkp.upgrade_catalog_app(
        ctx.kubeconfig, app, to_version=target, workspace=workspace
    )


@step("record_app_version")
def record_app_version(ctx, *, app: str, key: str = "app_before") -> None:
    """Remember an app's installed version, so a rollback can be verified."""
    version = ctx.nkp.app_version(ctx.kubeconfig, app)
    ctx.remember(key, version)
    ctx.log.info(f"{app} installed at {version or '(unknown)'}")


@step("set_app_version_pin")
def set_app_version_pin(ctx, *, app: str, version: str, namespace: str = "",
                        index: int = 0) -> None:
    """Move a platform app's per-cluster version pin.

    VERIFIED LIVE 2026-08-29: `nkp upgrade catalogapp` refuses platform apps
    ("Platform Apps can't be upgraded individually"), and the EFFECTIVE
    version comes from spec.clusterConfigOverrides[].appVersion
    (version_selector.go:39-60). Moving that pin is therefore the upgrade
    operation for a platform app - and it is a step, not a `run:`, because a
    JSON patch cannot survive YAML quoting.
    """
    import json as _json

    ns = namespace or ctx.recall("app_namespace") or "kommander"
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would pin {app} to {version} in {ns}")
        return
    # A pin to a version with no ClusterApp is accepted by the API and then
    # resolves to nothing: the old release keeps running while every later
    # assertion reads the pin field back and passes. That is exactly how
    # app-upgrade.yaml went green for weeks against logging-operator 6.4.1,
    # a version that exists in no branch of kommander-applications (each
    # release branch carries exactly ONE version per app). Refuse up front.
    known = {c["metadata"]["name"]
             for c in _list_lenient(ctx, "clusterapp", ns)}
    if known and f"{app}-{version}" not in known:
        near = sorted(n for n in known if n.startswith(app + "-"))
        raise AssertionError(
            f"set_app_version_pin: no ClusterApp {app}-{version} on this cluster, "
            f"so the pin would resolve to nothing and the app would keep running "
            f"its current version while every later assertion still passed.\n"
            f"  Published for {app} here: {near or 'none'}\n"
            f"  Each kommander-applications release branch carries exactly one "
            f"version per app, so a same-cluster app upgrade needs a version the "
            f"catalog actually has - a platform upgrade is what moves app versions.")
    patch = _json.dumps([{"op": "replace",
                          "path": f"/spec/clusterConfigOverrides/{index}/appVersion",
                          "value": version}])
    ctx.kube._run(["patch", "appdeployment", app, "-n", ns,
                   "--type=json", "-p", patch], quiet=True)
    ctx.log.info(f"{app}: per-cluster version pin moved to {version}")


@step("assert_app_effective_version")
def assert_app_effective_version(ctx, *, app: str, expected: str,
                                 namespace: str = "", timeout: str = "15m") -> None:
    """Assert the version an app EFFECTIVELY runs at on this cluster.

    Source-verified selection order (kommander
    common/pkg/appdeployment/version_selector.go:39-60): the first matching
    ``spec.clusterConfigOverrides[].appVersion`` WINS; only if none matches
    does ``spec.appRef.name`` apply. A test that reads appRef alone can pass
    while a stale per-cluster override silently pins the old version - the
    exact trap that makes an upgrade look successful when nothing moved.
    """
    ns = namespace or ctx.recall("app_namespace") or "kommander"
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert {app} effective version {expected} in {ns}")
        return
    seen = {"effective": "", "source": ""}

    def matches() -> bool:
        ad = ctx.kube.json(["get", "appdeployment", app, "-n", ns])
        if not ad:
            return False
        spec = ad.get("spec", {})
        overrides = spec.get("clusterConfigOverrides") or []
        pinned = next((o.get("appVersion") for o in overrides if o.get("appVersion")), "")
        if pinned:
            seen["effective"], seen["source"] = pinned, "clusterConfigOverrides[].appVersion"
        else:
            seen["effective"] = (spec.get("appRef") or {}).get("name", "")
            seen["source"] = "appRef.name"
        return expected in seen["effective"]

    try:
        wait_for(matches, ctx.log,
                 what=f"{app} effective version to be {expected} in {ns}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except TimeoutError:
        raise AssertionError(
            f"{app} in {ns}: effective version {seen['effective']!r} "
            f"(from {seen['source']}) != expected {expected!r}") from None
    ctx.log.info(f"{app}: effective version {seen['effective']} "
                 f"(selected via {seen['source']})")


@step("assert_app_instances")
def assert_app_instances(ctx, *, app: str, namespace: str = "",
                         min_count: int = 1, timeout: str = "15m") -> None:
    """Assert the app fanned out to every selected cluster.

    One AppDeploymentInstance is created per cluster matched by the
    AppDeployment's selector (synchronizer.go:187-220) - asserting on a
    single HelmRelease misses whether federation happened at all.
    """
    ns = namespace or ctx.recall("app_namespace") or "kommander"
    if ctx.config.dry_run:
        return
    found = {"n": 0}

    def enough() -> bool:
        items = ctx.kube.json(["get", "appdeploymentinstances", "-A"]).get("items", [])
        found["n"] = sum(1 for i in items
                         if i["metadata"].get("labels", {}).get(
                             "apps.kommander.d2iq.io/appdeployment-name") == app
                         or app in i["metadata"]["name"])
        return found["n"] >= min_count

    try:
        wait_for(enough, ctx.log, what=f"{min_count}+ AppDeploymentInstance(s) for {app}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except TimeoutError:
        raise AssertionError(
            f"{app}: {found['n']} AppDeploymentInstance(s), expected >= {min_count} "
            "- the app did not fan out to the selected cluster(s)") from None
    ctx.log.info(f"{app}: {found['n']} AppDeploymentInstance(s) - federation confirmed")


def _app_instances(ctx, app: str = "", cluster: str = "") -> list:
    """AppDeploymentInstances, read from the MANAGEMENT cluster.

    Kommander creates one per (AppDeployment, matched cluster). The status
    carries both halves of the question:

      * ``contentHash``          - what git says the app should be;
      * ``observedContentHash``  - what flux actually applied ON THE TARGET
        CLUSTER (app_deployment_instance_types.go:78-83).

    So an attached workload cluster can be asserted from the management
    cluster, with no kubeconfig switching and no second kubectl context.
    """
    items = ctx.kube.json(["get", "appdeploymentinstances", "-A"]).get("items", [])
    out = []
    for i in items:
        spec = i.get("spec", {}) or {}
        ref = ((spec.get("appRef") or {}).get("name") or "")
        cl = ((spec.get("kommanderClusterRef") or {}).get("name") or "")
        ad = ((i.get("metadata", {}).get("labels") or {})
              .get("apps.kommander.d2iq.io/appdeployment-name") or "")
        if app and ad != app and ref != app and not ref.startswith(app + "-"):
            continue
        if cluster and cl != cluster:
            continue
        out.append(i)
    return out


def _resolve_cluster(ctx, cluster: str) -> str:
    if cluster == "@workload":
        name = ctx.recall("workload_name") or ""
        if not name and not ctx.config.dry_run:
            raise RuntimeError(
                "cluster: @workload needs a prior create_workload_cluster")
        return name
    if cluster == "@management":
        return ctx.recall("mgmt_cluster_name") or ctx.cluster_name or ""
    return cluster


@step("record_app_instance")
def record_app_instance(ctx, *, app: str, cluster: str = "@workload",
                        key: str = "app_instance_before") -> None:
    """Record an app's content hash ON A GIVEN CLUSTER, to compare after."""
    if ctx.config.dry_run:
        return
    name = _resolve_cluster(ctx, cluster)
    inst = _app_instances(ctx, app, name)
    if not inst:
        raise AssertionError(
            f"{app}: no AppDeploymentInstance for cluster {name!r} - the app "
            "was never federated there, so there is nothing to compare against")
    st = inst[0].get("status", {}) or {}
    ctx.remember(key, {"contentHash": st.get("contentHash", ""),
                       "observedContentHash": st.get("observedContentHash", "")})
    ctx.log.info(f"{app} on {name}: contentHash={st.get('contentHash','')[:12] or '(none)'} "
                 f"observed={st.get('observedContentHash','')[:12] or '(none)'}")


@step("assert_app_on_cluster")
def assert_app_on_cluster(ctx, *, app: str, cluster: str = "@workload",
                          converged: bool = True, moved_from: str = "",
                          moved: bool = True, timeout: str = "20m") -> None:
    """Assert an app's state on a target cluster, FROM the management cluster.

    ``converged``   observedContentHash has caught up with contentHash, i.e.
                    flux applied on the target cluster what git asked for.
    ``moved_from``  the key a previous ``record_app_instance`` wrote; asserts
                    contentHash is now DIFFERENT. Without this an assertion
                    passes on an app that never moved - the exact vacuous
                    green this framework exists to catch.
    ``moved``       set false with ``moved_from`` to assert the opposite: that
                    the app has NOT moved yet. That is how a scenario proves a
                    second upgrade verb was actually needed, instead of
                    assuming it.
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert {app} on {cluster}")
        return
    name = _resolve_cluster(ctx, cluster)
    before = (ctx.recall(moved_from) or {}) if moved_from else {}
    seen = {"why": "no AppDeploymentInstance yet"}

    def ok() -> bool:
        inst = _app_instances(ctx, app, name)
        if not inst:
            seen["why"] = f"no AppDeploymentInstance for cluster {name!r}"
            return False
        st = inst[0].get("status", {}) or {}
        want, got = st.get("contentHash", ""), st.get("observedContentHash", "")
        if moved_from:
            same = want == before.get("contentHash", "")
            if moved and same:
                seen["why"] = f"contentHash still {want[:12]!r} - the app has not moved"
                return False
            if not moved and not same:
                seen["why"] = (f"contentHash already moved to {want[:12]!r} - "
                               "it was expected to still be unchanged here")
                return False
        if converged:
            if not want or not got:
                seen["why"] = f"hashes not populated yet (want={want[:12]!r} got={got[:12]!r})"
                return False
            if want != got:
                seen["why"] = (f"flux has not caught up on {name}: "
                               f"want {want[:12]}, applied {got[:12]}")
                return False
        seen["hash"] = want
        return True

    try:
        wait_for(ok, ctx.log, what=f"{app} converged on {name}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except TimeoutError:
        raise AssertionError(f"{app} on {name}: {seen['why']}") from None
    ctx.log.info(f"{app} on {name}: applied contentHash {seen.get('hash','')[:12]}"
                 + (" (moved)" if moved_from else ""))



@step("assert_app_version")
def assert_app_version(ctx, *, app: str, expected: str = "", was: str = "") -> None:
    """Assert an app's version - literally, or against an earlier recording.

    Passing ``was`` is how a rollback is verified: it compares against whatever
    ``record_app_version`` stored before the upgrade.
    """
    if ctx.config.dry_run:
        return
    want = expected or ctx.recall(was)
    found = ctx.nkp.app_version(ctx.kubeconfig, app)
    ctx.log.info(f"{app} version: {found}")
    if want and found != want:
        raise AssertionError(f"{app}: expected version {want}, found {found}")


# --------------------------------------------------------------- preconditions
@step("require_prism_central")
def require_prism_central(ctx) -> None:
    """Fail early if Prism Central is unreachable, before anything is created.

    Names the actual reason. A rejected password (401 - the dev PC's
    credentials rotate every few days) and a missing VPN route look identical
    from a boolean, and on 2026-09-07 the generic "check the VPN" message sent
    a diagnosis down the network path for an hour while the log's WARN line
    one row above already said "401". The exception text from the probe is
    the diagnosis; pass it through.
    """
    if ctx.config.dry_run:
        return
    ctx.log.info(f"nkp: {ctx.nkp.version()}")
    # A rejected password is final; a truncated or refused read is not. The
    # dev PC is shared and busy (2026-09-07: IncompleteRead while another run's
    # `nkp diagnose` was pulling from it) - one blip must not end a run in 13s.
    last = None
    for attempt in range(1, 4):
        try:
            ctx.pc._call("POST", "/api/nutanix/v3/clusters/list", {"kind": "cluster", "length": 1})
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if "401" in str(exc):
                break
            ctx.log.warn(f"Prism Central probe attempt {attempt}/3 failed: {exc}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"Prism Central check failed: {last}") from last


# --------------------------------------------------- app deployments / flux
#: AppDeployment lives in the kommander app-management API
_APPDEPLOYMENT_API = "apps.kommander.d2iq.io/v1alpha3"


def _app_target(ctx, name: str, namespace: str) -> tuple[str, str]:
    """Resolve the AppDeployment name/namespace, falling back to what deploy_app
    recorded, so a scenario names them once."""
    return (name or ctx.recall("app_name") or "",
            namespace or ctx.recall("app_namespace") or "")


@step("deploy_app")
def deploy_app(ctx, *, app: str, version: str = "", namespace: str = "",
               workspace: str = "", name: str = "", kind: str = "ClusterApp",
               config_overrides: str = "", config_values: str = "",
               wait: bool = True, timeout: str = "20m") -> None:
    """Deploy a catalog app by creating an AppDeployment - what the UI does.

    ``app`` and ``version`` form the appRef the catalog publishes
    (``logging-operator`` + ``6.4.0`` -> ``logging-operator-6.4.0``).

    ``kind`` matters: on a management cluster the platform catalog is
    cluster-scoped ClusterApps (verified live 2026-08-26 - every platform
    AppDeployment references kind: ClusterApp with an appID). ``kind: App``
    is the workspace/project catalog used on attached clusters; an
    AppDeployment pointing at a kind that has no matching object sits at
    observedGeneration -1 forever, which is exactly how the first live run
    failed.
    """
    # The UI asks "which workspace?" - accept the workspace NAME and resolve
    # its namespace the way the platform records it (spec.namespaceName).
    if workspace == "@workload" and not namespace:
        # the attached workload cluster's own (auto-created) workspace
        namespace = ctx.recall("workload_namespace") or ""
        workspace = ""
        if not namespace and not ctx.config.dry_run:
            raise RuntimeError("workspace: @workload needs a prior create_workload_cluster")
        if ctx.config.dry_run:
            namespace = namespace or "<workload-ns>"
    if workspace and not namespace:
        if ctx.config.dry_run:
            namespace = f"<ns-of-{workspace}>"
        else:
            ws = ctx.kube.json(["get", "workspace", workspace])
            namespace = (ws.get("spec") or {}).get("namespaceName") or ""
            if not namespace:
                raise RuntimeError(f"workspace {workspace!r} not found or has no namespaceName")
            ctx.log.info(f"workspace {workspace} -> namespace {namespace}")
    if not namespace:
        raise RuntimeError("deploy_app needs namespace: or workspace:")
    app_ref = f"{app}-{version}" if version else app
    deployment_name = name or app
    app_ref_spec = {"name": app_ref, "kind": kind}
    if kind == "ClusterApp":
        app_ref_spec["appID"] = app
    manifest = {
        "apiVersion": _APPDEPLOYMENT_API,
        "kind": "AppDeployment",
        "metadata": {"name": deployment_name, "namespace": namespace},
        "spec": {"appRef": app_ref_spec},
    }
    if config_values:
        # The UI flow, byte for byte: custom config becomes a ConfigMap with a
        # values.yaml key, and the AppDeployment references it via
        # spec.configOverrides. (Shape verified against the platform's own
        # kuttl fixtures.)
        config_overrides = config_overrides or deployment_name + "-overrides"
        ctx.kube.apply({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config_overrides, "namespace": namespace},
            "data": {"values.yaml": config_values},
        })
        ctx.remember("app_overrides_cm", config_overrides)
    if config_overrides:
        manifest["spec"]["configOverrides"] = {"name": config_overrides}

    # ENABLEMENT vs CONFIG - two different selectors, and getting this wrong
    # fails silently. spec.clusterSelector "select[s] clusters on which to
    # ENABLE the AppDeployment"; spec.clusterConfigOverrides[].clusterSelector
    # only picks which clusters get the overrides ConfigMap
    # (clientapis/pkg/apis/apps/v1alpha3/app_deployment_types.go:95,124-126).
    # Setting only the latter leaves the app enabled NOWHERE: the object is
    # accepted, status stays {"observedGeneration": 1}, no
    # AppDeploymentInstance is ever created, and the workload cluster simply
    # never grows the deployment. Live-diagnosed 2026-08-30 by diffing our
    # AppDeployment against cert-manager's, which kommander itself creates
    # with BOTH selectors set to the same value.
    target_cluster = ctx.recall("workload_name") or ""
    if target_cluster and not manifest["spec"].get("clusterSelector"):
        manifest["spec"]["clusterSelector"] = {
            "matchExpressions": [{
                "key": "kommander.d2iq.io/cluster-name",
                "operator": "In",
                "values": [target_cluster],
            }]
        }
        ctx.log.info(f"enabling {deployment_name} on cluster {target_cluster}")

    ctx.remember("app_name", deployment_name)
    ctx.remember("app_namespace", namespace)
    ctx.log.info(f"deploying {app_ref} as {namespace}/{deployment_name}")
    ctx.kube.apply(manifest)
    # Deploying is not finished when the object is accepted - it is finished
    # when the app is up. And if the caller supplied config_values, checking
    # that they reached the HelmRelease is part of the same contract: an
    # override that never lands looks exactly like one that did, until the
    # workload is inspected. Scenarios used to write both of these by hand.
    if wait and not ctx.config.dry_run:
        wait_app_deployed(ctx, name=deployment_name, namespace=namespace,
                          timeout=timeout)
        if config_values:
            assert_app_config_applied(ctx, name=deployment_name,
                                      namespace=namespace, timeout="10m")


@step("delete_app")
def delete_app(ctx, *, name: str = "", namespace: str = "") -> None:
    """Remove an AppDeployment. Safe to run when it was never created."""
    target, ns = _app_target(ctx, name, namespace)
    if not target or not ns:
        return
    ctx.kube.delete("appdeployment", target, namespace=ns)


def _heal_wedged_appmanagement(ctx, why: str) -> bool:
    """Restart kommander-appmanagement if it is stuck in a retry storm.

    Live-caught 2026-09-01: the controller could not clone the cluster's git
    repo - "cannot fork() for remote-https: Resource temporarily unavailable" -
    and retried with no backoff, 3708 failures in 90 seconds. Nothing was
    actually short of resources (node memory 34%, no PID pressure, the pod at
    181Mi of a 512Mi limit, git-operator healthy); the storm sustained itself.
    One rollout restart cleared it and the app installed normally, but by then
    it had burned 14 of the step's 20 minutes.

    Restarts at most once per run - a controller that wedges again immediately
    is a real problem and should surface as the timeout it is, not be papered
    over by a restart loop.
    """
    if ctx.config.dry_run or ctx.recall("appmanagement_healed"):
        return False
    logs = ctx.kube._run(
        ["logs", "-n", "kommander", "deploy/kommander-appmanagement",
         "--since=60s", "--tail=400"], check=False, quiet=True).stdout or ""
    storm = logs.count("cannot fork") + logs.count("Resource temporarily unavailable")
    if storm < 20:
        return False
    ctx.remember("appmanagement_healed", True)
    ctx.log.warn(f"kommander-appmanagement is in a retry storm ({storm} fork "
                 f"failures in the last 60s) while {why} - restarting it once")
    ctx.kube._run(["rollout", "restart", "deploy/kommander-appmanagement",
                   "-n", "kommander"], check=False, quiet=True)
    ctx.kube._run(["rollout", "status", "deploy/kommander-appmanagement",
                   "-n", "kommander", "--timeout=180s"], check=False, quiet=True)
    ctx.log.info("kommander-appmanagement restarted; the reconcile should resume")
    return True


@step("wait_app_deployed")
def wait_app_deployed(ctx, *, name: str = "", namespace: str = "",
                      timeout: str = "20m") -> None:
    """Wait until an AppDeployment has been processed and its release is up.

    Two things have to be true, and they fail differently: app-management has
    to have reconciled the AppDeployment (observedGeneration caught up), and the
    HelmRelease it produces has to be Ready and Released. Checking only the
    AppDeployment would pass while the chart install is still failing.
    """
    target, ns = _app_target(ctx, name, namespace)
    if not target or not ns:
        raise RuntimeError("wait_app_deployed needs name and namespace")

    last: list[str] = []
    started = time.monotonic()

    def deployed() -> bool:
        last.clear()
        # give it a fair run first; only a genuinely stuck reconcile is worth
        # restarting a platform controller for
        if time.monotonic() - started > 180:
            _heal_wedged_appmanagement(ctx, f"waiting for {ns}/{target}")
        app = ctx.kube.json(["get", "appdeployment", target, "-n", ns])
        if not app:
            last.append(f"AppDeployment {ns}/{target} does not exist yet")
            return False
        generation = app.get("metadata", {}).get("generation")
        observed = app.get("status", {}).get("observedGeneration")
        if observed is None or (generation is not None and observed < generation):
            last.append(f"{ns}/{target}: not reconciled yet (observedGeneration={observed})")
            return False

        # An app is not necessarily ONE HelmRelease named after it. istio-helm
        # renders five - istio-helm-base/-cni/-istiod/-ztunnel/-gateway - and
        # none is called "istio-helm", so looking up that exact name waited
        # forever while every release was already healthy. Live-caught
        # 2026-09-01: the app finished installing and this still timed out.
        releases = [r for r in _list_lenient(ctx, "helmrelease", ns)
                    if (r.get("metadata", {}).get("name", "") == target
                        or r.get("metadata", {}).get("name", "").startswith(f"{target}-"))]
        if not releases:
            # An app deployed to an ATTACHED cluster's workspace renders no
            # HelmRelease here: kommander federates an AppDeploymentInstance and
            # the workload cluster's own flux owns the release. Waiting for a
            # local HelmRelease then times out on a deployment that worked -
            # live-caught 2026-09-02, kube-prometheus-stack on a workload
            # workspace: instance synced, no local release, 35m wasted.
            # ONLY for a genuinely attached cluster. On the management cluster
            # an AppDeploymentInstance appears within seconds, long before the
            # HelmReleases are healthy - taking it as success there returns a
            # green deploy_app on an app that has not installed yet. Introduced
            # and caught in the same run, 2026-09-02.
            wl_ns = ctx.recall("workload_namespace") or ""
            insts = _app_instances(ctx, target, "") if (wl_ns and ns == wl_ns) else []
            here = [i for i in insts
                    if i.get("metadata", {}).get("namespace") == ns]
            synced = [i for i in here
                      if any(c.get("type") == "Synced" and c.get("status") == "True"
                             for c in (i.get("status", {}) or {}).get("conditions", []))
                      or (i.get("status", {}) or {}).get("contentHash")]
            if synced:
                ctx.log.info(f"{ns}/{target}: federated to an attached cluster "
                             f"({len(synced)} AppDeploymentInstance(s)); the release "
                             "lives on that cluster - use assert_app_on_cluster to "
                             "prove it converged")
                return True
            if here:
                last.append(f"{ns}/{target}: AppDeploymentInstance exists but has "
                            "no contentHash yet")
                return False
            last.append(f"{ns}/{target}: no HelmRelease created yet")
            return False
        # every release the app renders has to be healthy, not just the first:
        # a dependency chain reports the tail as pending while the head is Ready
        for rel in sorted(releases, key=lambda r: r["metadata"]["name"]):
            problem = ctx.kube.helmrelease_problem(rel)
            if problem:
                last.append(f"{rel['metadata']['name']}: {problem}"
                            if len(releases) > 1 else problem)
                return False
        if len(releases) > 1:
            names = ", ".join(sorted(r["metadata"]["name"] for r in releases))
            ctx.log.debug(f"{target}: all {len(releases)} releases ready ({names})")
        return True

    try:
        wait_for(
            deployed,
            ctx.log,
            what=f"AppDeployment {ns}/{target} deployed",
            timeout_s=parse_duration(timeout),
            dry_run=ctx.config.dry_run,
        )
    except Exception as exc:
        detail = f" Last seen: {last[0]}" if last else ""
        raise AssertionError(f"{exc}.{detail}") from exc


@step("assert_helmrelease_ready")
def assert_helmrelease_ready(ctx, *, name: str = "", namespace: str = "",
                             timeout: str = "15m") -> None:
    """Assert one HelmRelease is Ready and Released, waiting up to ``timeout``.

    Defaults to the release produced by the last ``deploy_app``.
    """
    target, ns = _app_target(ctx, name, namespace)
    if not target or not ns:
        raise RuntimeError("assert_helmrelease_ready needs name and namespace")

    last: list[str] = []

    def ready() -> bool:
        last.clear()
        item = ctx.kube.json(["get", "helmrelease", target, "-n", ns])
        if not item:
            last.append(f"HelmRelease {ns}/{target} does not exist")
            return False
        problem = ctx.kube.helmrelease_problem(item)
        if problem:
            last.append(problem)
        return problem is None

    try:
        wait_for(
            ready,
            ctx.log,
            what=f"HelmRelease {ns}/{target} Ready",
            timeout_s=parse_duration(timeout),
            dry_run=ctx.config.dry_run,
        )
    except Exception as exc:
        detail = f" Last seen: {last[0]}" if last else ""
        raise AssertionError(f"{exc}.{detail}") from exc
    ctx.log.info(f"HelmRelease {ns}/{target} is Ready and Released")


@step("assert_app_workload_running")
def assert_app_workload_running(ctx, *, namespace: str = "", timeout: str = "10m",
                                tolerate: int = 0) -> None:
    """Assert the pods the app actually runs are healthy.

    A Ready HelmRelease means Helm reported success, not that the workload
    survived - this is the difference between "installed" and "running".
    """
    ns = namespace or ctx.recall("app_namespace") or ""
    if not ns:
        raise RuntimeError("assert_app_workload_running needs a namespace")
    ctx.kube.wait_pods_healthy(
        namespace=ns, timeout_s=parse_duration(timeout), tolerate=tolerate
    )


@step("assert_all_helmreleases_ready")
def assert_all_helmreleases_ready(ctx, *, namespace: str = "", selector: str = "",
                                  timeout: str = "25m") -> None:
    """Wait until every HelmRelease is Ready and Released.

    ``selector`` narrows it, e.g. the label kommander puts on releases it owns:
    ``kommander.d2iq.io/managed-by-kind=AppDeployment``.
    """
    last: list[str] = []

    def all_ready() -> bool:
        last.clear()
        last.extend(
            ctx.kube.unready_helmreleases(
                namespace=namespace or None, selector=selector or None
            )
        )
        return not last

    try:
        wait_for(
            all_ready,
            ctx.log,
            what="all HelmReleases Ready",
            timeout_s=parse_duration(timeout),
            dry_run=ctx.config.dry_run,
        )
    except Exception as exc:
        listed = "; ".join(last[:8])
        more = f" (+{len(last) - 8} more)" if len(last) > 8 else ""
        raise AssertionError(f"{len(last)} HelmRelease(s) not up: {listed}{more}") from exc


@step("assert_appdeployments_ready")
def assert_appdeployments_ready(ctx, *, namespace: str = "", timeout: str = "25m") -> None:
    """Every AppDeployment in a namespace has a Ready, Released HelmRelease.

    This is the platform-wide version: after an install or upgrade, nothing the
    platform deployed should be sitting broken.
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would check every AppDeployment in {namespace or 'all namespaces'}")
        return

    args = ["get", "appdeployments"]
    args += ["-n", namespace] if namespace else ["-A"]
    apps = ctx.kube.json(args).get("items", [])
    if not apps:
        raise AssertionError(
            f"no AppDeployments found in {namespace or 'any namespace'} - "
            "is the platform installed?"
        )
    ctx.log.info(f"checking {len(apps)} AppDeployment(s)")

    last: list[str] = []

    def all_up() -> bool:
        last.clear()
        for app in apps:
            meta = app["metadata"]
            name, ns = meta["name"], meta["namespace"]
            release = ctx.kube.json(["get", "helmrelease", name, "-n", ns])
            if not release:
                last.append(f"{ns}/{name}: no HelmRelease")
                continue
            problem = ctx.kube.helmrelease_problem(release)
            if problem:
                last.append(problem)
        return not last

    try:
        wait_for(
            all_up,
            ctx.log,
            what=f"{len(apps)} AppDeployment(s) deployed",
            timeout_s=parse_duration(timeout),
            dry_run=False,
        )
    except Exception as exc:
        raise AssertionError(
            f"{len(last)} of {len(apps)} AppDeployment(s) not up: {'; '.join(last[:8])}"
        ) from exc


@step("use_existing_cluster")
def use_existing_cluster(ctx, *, kubeconfig: str = "", cluster_name: str = "") -> None:
    """Run against a cluster that already exists instead of building one.

    Lets an app-level scenario be re-run in minutes rather than paying the
    40-minute cluster build every time. The cluster is never deleted by
    cleanup, because this scenario did not create it.

    Cluster-LEVEL steps (``upgrade_nodes``) need the cluster's name, which a
    supplied kubeconfig does not carry, so it is read from the CAPI Cluster
    rather than made the author's problem. ``cluster_name:`` overrides that
    when several clusters share one kubeconfig.
    """
    # finish_cluster must be able to tell a supplied cluster from one we
    # made. Without this its mode is unset, every guard falls through, and
    # it reaches delete_cluster - i.e. a scenario that merely LISTS
    # finish_cluster in cleanup would delete a cluster it did not create.
    ctx.remember("smart_mode", "existing")
    from pathlib import Path

    path = Path(kubeconfig or ctx.config.kubeconfig_env or "").expanduser()
    if not str(path):
        raise RuntimeError(
            "use_existing_cluster needs a kubeconfig - pass kubeconfig: or set "
            "E2E_KUBECONFIG"
        )
    if not path.exists() and not ctx.config.dry_run:
        raise RuntimeError(f"kubeconfig not found: {path}")
    ctx.kubeconfig = path
    ctx.log.info(f"using existing cluster via {path}")
    _announce_kubeconfig(ctx, path, "existing")
    if not ctx.config.dry_run and not ctx.kube.server_reachable():
        raise RuntimeError(f"cluster at {path} is not reachable")
    if cluster_name:
        ctx.cluster_name = cluster_name
    elif not ctx.config.dry_run:
        items = ctx.kube.json(["get", "cluster", "-A"]).get("items", [])
        if len(items) == 1:
            meta = items[0]["metadata"]
            ctx.cluster_name = meta["name"]
            ctx.remember("cluster_namespace", meta.get("namespace", ""))
            ctx.log.info(f"cluster: {meta['name']} (namespace {meta.get('namespace')})")
        elif len(items) > 1:
            raise RuntimeError(
                f"{len(items)} CAPI Clusters on this kubeconfig "
                f"({', '.join(i['metadata']['name'] for i in items)}) - pass "
                "cluster_name: so cluster-level steps act on the right one")


@step("reset_bootstrap")
def reset_bootstrap(ctx, *, name: str = "konvoy-capi-bootstrapper") -> None:
    """Delete a leftover local bootstrap cluster before creating a new one.

    `nkp create cluster --self-managed` reuses an existing kind bootstrapper if
    it finds one. A half-finished one from an earlier failed run reports
    "Using existing bootstrap cluster" and then fails deep inside admission:

        admission webhook "webhook.nkpcluster.kommander.mesosphere.io" denied
        the request: ... dryRun=All: context deadline exceeded

    which says nothing about the real cause. Starting from a known state is
    cheaper than diagnosing that a second time.
    """
    from .shell import run

    existing = run(["kind", "get", "clusters"], ctx.log,
                   dry_run=ctx.config.dry_run, check=False, quiet=True, timeout=60)
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would delete a leftover '{name}' bootstrap cluster")
        return
    if name not in (existing.stdout or ""):
        return
    # The bootstrapper name is fixed, so another build on this machine (a
    # template build, a colleague's create) may be mid-pivot inside it right
    # now. Deleting it under them kills their build; a bootstrapper carrying
    # any CAPI cluster is treated as busy and waited on - a self-managed
    # create deletes its own bootstrapper when the pivot completes.
    import json as _json
    import subprocess as _sp
    for _ in range(60):  # up to 30 min
        try:
            probe = _sp.run(
                ["kubectl", "--context", f"kind-{name}", "get", "clusters",
                 "-A", "-o", "json"],
                capture_output=True, text=True, timeout=60,
            )
            live = _json.loads(probe.stdout or "{}").get("items", []) if probe.returncode == 0 else []
        except Exception:  # noqa: BLE001 - unreachable kind == not busy
            live = []
        foreign = [
            i["metadata"]["name"] for i in live
            if ctx.cluster_name not in i["metadata"]["name"]
        ]
        if not foreign:
            break
        ctx.log.warn(
            f"bootstrap cluster busy with {foreign} - waiting for that build "
            "to pivot rather than deleting it underneath them"
        )
        time.sleep(30)
        existing = run(["kind", "get", "clusters"], ctx.log,
                       dry_run=False, check=False, quiet=True, timeout=60)
        if name not in (existing.stdout or ""):
            return
    ctx.log.warn(f"deleting leftover bootstrap cluster '{name}' from an earlier run")
    run(["kind", "delete", "cluster", "--name", name], ctx.log,
        dry_run=False, check=False, timeout=300)


# ------------------------------------------------------------ instant-cluster
def _ensure_claim_vip(ctx, vip: str) -> None:
    import json as _json
    import subprocess

    def vip_up() -> bool:
        r = subprocess.run(["curl", "-sk", "-o", "/dev/null", "-m", "6",
                            f"https://{vip}:6443/version"], capture_output=True)
        return r.returncode == 0

    if vip_up():
        return
    ctx.log.warn(f"claim VIP {vip} not answering - repairing kube-vip on the CP")
    # CP node IP from the claim map written by stage A
    from pathlib import Path
    state = Path(os.environ.get("SPEEDSTART_DIR", "."))
    maps = sorted(state.glob(f"claim-map-{ctx.cluster_name[:20]}*.json"),
                  key=lambda f: f.stat().st_mtime)
    cp_ip = ""
    if maps:
        m = _json.loads(maps[-1].read_text())
        cps = m.get("cps") or []
        if cps:
            cp_ip = (cps[0].get("new_ip") or cps[0].get("ip") or "")
    if not cp_ip:
        ctx.log.warn("could not find the CP IP in the claim map - skipping repair")
        return
    key = str(Path.home() / ".ssh/nkp_cluster")
    # replace WHATEVER value sits on the address line - the baked-in wrong
    # value has already surprised us twice (node IP once, a stale mint IP once)
    remote = ("sudo sed -i '/name: address/{n;s|value: .*|value: \"" + vip + "\"|;}' "
              "/etc/kubernetes/manifests/kube-vip.yaml && "
              "sudo grep -A1 'name: address' /etc/kubernetes/manifests/kube-vip.yaml && "
              "C=$(sudo crictl ps | grep kube-vip | awk '{print $1}') && "
              "sudo crictl stop $C")
    r = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
                        "-i", key, f"konvoy@{cp_ip}", remote],
                       capture_output=True, text=True, timeout=90)
    ctx.log.info(f"kube-vip repair on {cp_ip}: rc={r.returncode} "
                 f"{(r.stdout or r.stderr)[-120:].strip()}")
    for _ in range(10):
        time.sleep(10)
        if vip_up():
            ctx.log.info(f"claim VIP {vip} repaired and answering")
            return
    ctx.log.warn(f"claim VIP {vip} still down after repair attempt")


def _sweep_lb_configmaps(ctx, old_lb: str, new_lb: str) -> None:
    """Rewrite the template's LB address out of kommander ConfigMaps.

    appmanagement re-renders some of these from template-state AFTER boot, and
    it does so on its own schedule - so one pass is not enough; the sweep has
    to keep verifying until scans come back clean. That convergence
    requirement is real and is preserved here.

    What is NOT required is waiting blindly. This used to sleep 45s between
    rounds (even when round one found nothing) and then a flat 75s for an
    appmanagement restart, so 2-4 minutes of a claim was spent asleep rather
    than working. Measured 2026-08-30 on workload-attach: 6m25s in this
    function. Now it polls, and it restarts each deployment once instead of
    once per ConfigMap that happens to share its name prefix - dex was being
    rolled twice and dex-k8s-authenticator three times in a single sweep.
    """
    if ctx.config.dry_run or old_lb == new_lb:
        return
    started = time.monotonic()
    deadline = started + 300          # the old worst case was ~255s of sleeps alone
    clean_scans = 0
    rounds = 0
    ctx.log.info(f"sweeping kommander ConfigMaps for the template LB {old_lb} "
                 f"-> {new_lb}; appmanagement can re-render a stale one, so "
                 "this needs two consecutive clean scans, usually 60-120s")
    while time.monotonic() < deadline:
        rounds += 1
        fixed = _sweep_lb_configmaps_once(ctx, old_lb, new_lb)
        ctx.log.info(f"  scan {rounds}: {fixed} ConfigMap(s) rewritten, "
                     f"{clean_scans}/2 consecutive clean")
        clean_scans = clean_scans + 1 if fixed == 0 else 0
        # two consecutive clean scans: appmanagement has had a chance to
        # re-poison and did not. One is not enough - that was the original bug.
        if clean_scans >= 2:
            ctx.log.info(f"LB sweep converged after {rounds} scan(s), "
                         f"{int(time.monotonic() - started)}s")
            break
        time.sleep(8)
    else:
        ctx.log.warn("LB sweep still finding stale ConfigMaps after 5 minutes - continuing")

    # Force the re-render NOW rather than letting it happen minutes later:
    # live-seen, dex-k8s-authenticator dialled the dead LB 40 min post-sweep.
    # Wait for the rollout to actually finish instead of guessing 75 seconds.
    ctx.log.info("restarting kommander-appmanagement so it re-renders off the "
                 "corrected values - waiting for the rollout, up to 180s")
    ctx.kube._run(["rollout", "restart", "deploy", "-n", "kommander",
                   "kommander-appmanagement"], quiet=True, check=False)
    ctx.kube._run(["rollout", "status", "deploy", "-n", "kommander",
                   "kommander-appmanagement", "--timeout=180s"],
                  quiet=True, check=False)
    fixed = _sweep_lb_configmaps_once(ctx, old_lb, new_lb)
    ctx.log.info(f"post-re-render sweep: {fixed} CM(s) fixed")
    # Some of these ConfigMaps are RENDERED BY HELM from valuesFrom sources, so
    # patching them is futile - the next render puts the template address back,
    # minutes later, after the sweep has declared itself converged. Live-caught
    # twice on 2026-08-31: cm/dex-k8s-authenticator was rewritten, the sweep
    # reported 2 clean scans and "0 CM(s) fixed", and the pod still died on
    # "dial <lb-address>: no route to host" ten minutes on. The durable fix is
    # to re-render off the (now corrected) sources rather than patch output, so
    # force a reconcile of the SSO HelmReleases and let helm regenerate them.
    #
    # requestedAt alone is NOT enough. If the release already tried to upgrade
    # with the pre-sweep values it rendered the stale address, its pods failed
    # to start, the upgrade timed out and remediation left the HelmRelease
    # Stalled=True (MissingRollbackTarget: "cannot remediate failed release").
    # A stalled release has exhausted its retries and ignores requestedAt, so
    # the corrected sources are never read and the stale render stands - the
    # gates then time out on a Ready=False kustomization. Live-caught
    # 2026-09-01: cm/dex-k8s-authenticator held <lb-address> while its own
    # values source already said <lb-address>. Toggling suspend resets the
    # remediation state; the very next reconcile rendered the right address and
    # went Ready.
    #
    # The reset below only catches a release ALREADY stalled by the time the
    # sweep runs - a leftover from an earlier attempt. In the case above the
    # stall appeared minutes later, triggered by this very re-render, so the
    # layer that actually recovers it is the stalled-HelmRelease self-heal in
    # the flux gate (konvoy2/hack/instant-cluster/gates.py). Both exist because
    # either one can be the first to see it.
    for hr in ("dex", "dex-k8s-authenticator", "traefik-forward-auth-mgmt",
               "kube-oidc-proxy"):
        stalled = any(
            c.get("type") == "Stalled" and c.get("status") == "True"
            for c in (((ctx.kube.json(["get", "hr", hr, "-n", "kommander"]) or {})
                       .get("status") or {}).get("conditions") or []))
        if stalled:
            ctx.log.info(f"{hr}: HelmRelease is stalled - resetting it so the "
                         "corrected values are read")
            for val in ("true", "false"):
                ctx.kube._run(["patch", "hr", hr, "-n", "kommander",
                               "--type=merge", "-p", f'{{"spec":{{"suspend":{val}}}}}'],
                              quiet=True, check=False)
        else:
            ctx.kube._run(["annotate", "hr", hr, "-n", "kommander",
                           f"reconcile.fluxcd.io/requestedAt={int(time.time())}",
                           "--overwrite"], quiet=True, check=False)
    ctx.log.info("forced a re-render of the SSO HelmReleases off corrected sources")
    for dep in ("dex", "dex-k8s-authenticator", "traefik-forward-auth-mgmt"):
        ctx.kube._run(["rollout", "restart", "deploy", "-n", "kommander", dep],
                      quiet=True, check=False)
    ctx.log.info(f"SSO repair complete in {int(time.monotonic() - started)}s")


def _sweep_lb_configmaps_once(ctx, old_lb: str, new_lb: str) -> int:
    import json as _json

    try:
        cms = ctx.kube.json(["get", "cm", "-n", "kommander"]).get("items", [])
    except Exception as exc:  # noqa: BLE001 - the sweep is best-effort
        ctx.log.warn(f"LB ConfigMap sweep skipped: {exc}")
        return 0
    deploys = {d["metadata"]["name"] for d in
               ctx.kube.json(["get", "deploy", "-n", "kommander"]).get("items", [])}

    def _retrying(args, what):
        # the claim's apiserver can drop connections while kube-vip and the LB
        # settle right after boot - a hygiene pass must shrug, retry, and never
        # fail the run
        for attempt in (1, 2, 3):
            r = ctx.kube._run(args, quiet=True, check=False)
            if r.ok:
                return True
            time.sleep(5 * attempt)
        ctx.log.warn(f"{what} did not stick after 3 tries - continuing")
        return False

    fixed = 0
    owners: set = set()
    for cm in cms:
        name = cm["metadata"]["name"]
        data = cm.get("data") or {}
        if old_lb not in _json.dumps(data):
            continue
        for k, v in data.items():
            data[k] = v.replace(old_lb, new_lb)
        if _retrying(["patch", "cm", "-n", "kommander", name, "--type=merge",
                      "-p", _json.dumps({"data": data})], f"rewrite of cm/{name}"):
            ctx.log.warn(f"rewrote template LB in cm/{name}")
        owner = next((d for d in sorted(deploys, key=len, reverse=True)
                      if name.startswith(d)), None)
        if owner:
            owners.add(owner)
        fixed += 1
    # One restart per deployment, after every ConfigMap it reads has been
    # rewritten - restarting per ConfigMap rolled the same pod repeatedly and
    # could pick up a half-swept set.
    for owner in sorted(owners):
        if _retrying(["rollout", "restart", "deploy", "-n", "kommander", owner],
                     f"restart of deploy/{owner}"):
            ctx.log.info(f"restarted deploy/{owner} to pick up the rewrite")
    return fixed


def _instant_cluster_dir():
    """The freeze/claim machinery home, wherever this framework lives.

    dev-e2e used to sit in konvoy2/hack next to instant-cluster; now that it
    is a standalone folder, resolve by env override first, then the sibling
    layout (still valid on the DevVM copy), then the canonical konvoy2 path.
    """
    from pathlib import Path

    # Order: explicit override, then the copy INSIDE this repository (the
    # framework ships its own claim machinery now), then the historical
    # konvoy2 location, then a sibling directory (the old DevVM layout). A
    # stale sibling copy once shadowed the real machinery and ran an old
    # claim.py, which is why the in-repo copy outranks the sibling guess.
    candidates = [
        os.environ.get("NKP_INSTANT_CLUSTER_DIR"),
        Path(__file__).resolve().parents[1] / "instant-cluster",
        Path.home() / "Documents/nkp/konvoy2/hack/instant-cluster",
        Path(__file__).resolve().parents[2] / "instant-cluster",
    ]
    for c in candidates:
        if c and Path(c).joinpath("claim.py").exists():
            return Path(c)
    raise RuntimeError(
        "instant-cluster machinery not found; set NKP_INSTANT_CLUSTER_DIR")

def _dockerhub_watch(ctx):
    """Hold authenticated Docker Hub pulls in place, if we have credentials.

    The `kommander` and `kommander-appmanagement` charts come from
    oci://docker.io/mesosphere/... on both release lines; every other chart is
    on ghcr. A shared lab egress IP burns Docker Hub's anonymous allowance
    (100 pulls / 6h / IP), and those two OCIRepositories fail with
    TOOMANYREQUESTS - which surfaces as flux Kustomizations that will not go
    Ready, i.e. an opaque gate timeout.

    install_platform already did this for the CREATE path. A CLAIM needs it
    too: the clone re-pulls those charts on first convergence. Live-caught
    2026-09-01, where the gates timed out after 20 minutes with
    'health check failed ... timeout waiting for: [OCIRepository ...]'.

    Returns a Popen to terminate, or None when there are no credentials -
    never fatal, because a run that is not rate-limited does not need it.
    """
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path as _P

    if ctx.config.dry_run or not os.environ.get("DOCKERHUB_USER"):
        return None
    helper = _P(__file__).resolve().parent.parent / "dockerhub_auth.py"
    if not helper.exists():
        return None
    ctx.log.info("holding authenticated Docker Hub pulls for the mesosphere charts")
    return _sp.Popen([_sys.executable, str(helper), str(ctx.kubeconfig), "--watch", "5"],
                     stdout=_sp.DEVNULL, stderr=_sp.STDOUT)


@contextlib.contextmanager
def _dockerhub_hold(ctx):
    """_dockerhub_watch for the length of a block, always terminated.

    An UPGRADE re-pulls the kommander and kommander-appmanagement charts from
    docker.io just as a claim does, so it hits the same anonymous rate limit -
    and there it surfaces as `nkp upgrade kommander` sitting on "Ensuring
    KommanderCore is upgraded" while two OCIRepositories report "failed to
    determine artifact digest". Live-caught 2026-09-01: 14 minutes of no
    progress, cleared within 45s of authenticating.
    """
    proc = _dockerhub_watch(ctx)
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()


@step("claim_cluster")
def claim_cluster(ctx, *, template: str = "", template_vip: str = "",
                  template_lb: str = "", name: str = "", gates: bool = True,
                  preserve_addresses: bool = False, timeout: str = "20m") -> None:
    """Claim a clone of a frozen template instead of building a cluster.

    The per-change flow this belongs to: the FIRST cluster for a change is
    built the traditional way (build_template.sh - that run proves the create
    path), frozen once, and every scenario after that claims a clone in ~9
    minutes with a fresh name/VIP/LB. konvoy2/hack/instant-cluster owns the
    machinery; this step only orchestrates it and hands the framework a
    kubeconfig, so scenarios cannot tell a claim from a build.
    """
    import subprocess
    from pathlib import Path

    # Identity comes from the caller (smart_cluster reads the registry) or an
    # explicit YAML option in machinery-test scenarios. The env fallbacks are
    # for pipeline debugging only - developers never export these; the
    # framework decides claim-vs-create itself (use smart_cluster).
    tpl = template or os.environ.get("E2E_TEMPLATE", "qa-nrm3")
    old_vip = template_vip or os.environ.get("E2E_TEMPLATE_VIP", "<lb-address>")
    template_lb = template_lb or os.environ.get("E2E_TEMPLATE_LB", "<lb-address>")
    ic_dir = _instant_cluster_dir()

    # Claim names are embedded in CAPI Machine names: lowercase RFC 1123, short.
    claim_name = (name or f"{os.environ.get('USER', 'dev').replace('.', '')[:8]}-{ctx.scenario_slug[:12]}").lower()
    claim_name = re.sub(r"[^a-z0-9-]", "-", claim_name).strip("-")[:32]

    ctx.cluster_name = claim_name
    # Mark the run as a claim before anything else, dry-run included:
    # finish_cluster keys off this to sweep the clone rather than run
    # `nkp delete cluster`, which a claim does not answer - live 2026-08-31 the
    # delete errored and only the VM sweep actually removed the clone.
    ctx.remember("smart_mode", "claimed")
    if preserve_addresses:
        # Give the clone the template's OWN VIP and LB. The template is frozen
        # powered OFF, so both are free on the wire. Every address-rewrite in
        # the claim then becomes a no-op replace of X with X, and
        # _sweep_lb_configmaps short-circuits on old_lb == new_lb - so this is
        # also the control that answers "does a claim need SSO repair at all,
        # or only because we changed the address?".
        # COST: only ONE live clone per template, since they share the pair.
        new_vip, lb = old_vip, f"{template_lb}-{template_lb}"
        # HARD guard, not a warning: this path does not go through the pool, so
        # without an explicit hold two concurrent claims of the same template
        # would both proceed and collide on the wire.
        from .netpool import hold as _hold_addresses

        _hold_addresses([new_vip, template_lb], claim_name, ctx.log,
                        dry_run=ctx.config.dry_run)
        ctx.log.warn(f"preserving template addresses: vip {new_vip}, lb {template_lb} "
                     "- at most one live clone of this template")
    else:
        new_vip, lb = ctx.addresses()
    # claim.py rewrites addresses byte-for-byte in at-rest state, so it takes a
    # SINGLE LB IP of the same string length as the template's - not a range.
    lb_ip = lb.split("-")[0]
    if len(lb_ip) != len(template_lb) or len(new_vip) != len(old_vip):
        raise RuntimeError(
            f"claim needs same-length addresses: vip {old_vip}->{new_vip}, "
            f"lb {template_lb}->{lb_ip}"
        )

    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would claim {tpl} -> {claim_name} vip={new_vip} lb={lb.split('-')[0]}")
        ctx.kubeconfig = ctx.artifacts / f"{claim_name}-claim.conf"
        return

    env = {**os.environ,
           "NKP_TEMPLATE_LB": template_lb,
           "NKP_PC_URL": ctx.config.pc_url,
           "NKP_NUTANIX_USER": ctx.config.pc_user,
           "NKP_NUTANIX_PASSWORD": ctx.config.pc_password}
    started = time.monotonic()
    ctx.log.info(f"claiming {tpl} -> {claim_name} (vip {new_vip}, lb {lb})")
    log_file = ctx.artifacts / f"claim-{claim_name}.log"
    ctx.log.info("claim: stage A (clone, power-on, re-identity), then the "
                 "private-apiserver window (at-rest LB/secret/machine rewrite, "
                 "git re-hydrate), then first boot - usually 400-450s")
    # Streamed, not swallowed. claim.py is already a curated progress feed;
    # writing it only to a file left ~7 minutes of blank terminal at the very
    # start of a run, which reads as a hang.
    rc = run(["python3", "claim.py", tpl, claim_name, old_vip, new_vip, lb_ip],
             ctx.log, cwd=ic_dir, env=env, log_file=log_file,
             timeout=parse_duration(timeout), check=False, stream_all=True).code
    if rc != 0:
        tail = "\n".join(log_file.read_text().splitlines()[-8:])
        raise RuntimeError(f"claim failed (rc={rc}); tail of {log_file.name}:\n{tail}")

    # claim.py writes into SPEEDSTART_DIR when set (its state home), else next
    # to itself; resolve the same way or a successful claim looks like a miss.
    state_dir = Path(os.environ.get("SPEEDSTART_DIR") or ic_dir)
    kc = state_dir / f"{claim_name}-claim.conf"
    if not kc.exists():
        kc = ic_dir / f"{claim_name}-claim.conf"
    if not kc.exists():
        raise RuntimeError(f"claim reported success but {kc} is missing")
    ctx.kubeconfig = ctx.artifacts / kc.name
    ctx.kubeconfig.write_text(kc.read_text())
    ctx.log.info(f"claimed in {time.monotonic()-started:.0f}s -> {ctx.kubeconfig.name}")
    _announce_kubeconfig(ctx, ctx.kubeconfig, "management")

    # DHCP/VIP-pool collisions can leave kube-vip broadcasting the node's own
    # address instead of the claim VIP (template minted while DHCP invaded the
    # pool: one string, two meanings). Verify the VIP answers; if not, repair
    # kube-vip's manifest on the CP over ssh - the exact fix proven live.
    _ensure_claim_vip(ctx, new_vip)

    # The claim pipeline's LB rewrite covers secrets; ConfigMaps rendered at
    # template time can still carry the template LB (live-caught: tfa's CM ->
    # any later rollout crashloops on the dead address and wedges gates).
    # Sweep them here until the pipeline's on-seed payload learns ConfigMaps.
    _sweep_lb_configmaps(ctx, template_lb, lb_ip)

    # the clone re-pulls the docker.io charts on first convergence, so the
    # rate limit lands here just as it does on an install
    _dh = _dockerhub_watch(ctx)
    try:
        if gates:
            # Clones keep their unique VM names (safe for concurrent claims), so
            # gates assert a READY NutanixMachine rather than VM-name equality.
            env["BOC_SKIP_VM_RENAME"] = "1"
            grc = 1
            gates_log = ctx.artifacts / f"gates-{claim_name}.log"
            for attempt in (1, 2):
                ctx.log.info(f"identity gates attempt {attempt}/2: pod health, "
                             "flux kustomizations, then CAPI parity - usually "
                             "2-4 min, budget 20 min")
                try:
                    grc = run(["python3", "gates.py", claim_name], ctx.log,
                              cwd=ic_dir, env=env, log_file=gates_log,
                              timeout=1200, check=False, stream_all=True).code
                except subprocess.TimeoutExpired:
                    # A timed-out attempt is a FAILED attempt, not a fatal -
                    # gates are often seconds from green when the budget ends.
                    grc = 124
                    with gates_log.open("a") as sink:
                        sink.write("\n[e2e] gates attempt timed out after 1200s\n")
                if grc == 0:
                    break
                if attempt == 1:
                    # post_boc's durable-rebind tail may still be running right
                    # after the window closes; give it one settle window. Also
                    # bounce the known startup-racer: dex-k8s-authenticator
                    # crashloops until dex settles (product race, seen 4x) and a
                    # fresh pod clears its backoff instantly.
                    ctx.log.info(f"gates not green yet (rc={grc}) - settling 120s and retrying")
                    ctx.kube._run(["rollout", "restart", "deploy", "-n", "kommander",
                                   "dex-k8s-authenticator"], quiet=True, check=False)
                    time.sleep(120)
            if grc != 0:
                raise RuntimeError(
                    f"claim {claim_name} failed identity gates (rc={grc}) - "
                    f"see gates-{claim_name}.log"
                )
            ctx.log.info("identity gates: PASS")
    finally:
        # a gate failure must not leak the watcher subprocess
        if _dh is not None:
            _dh.terminate()
        # PARITY BAR, derived from the gate log rather than measured by a
        # separate poll: `nkp create cluster nutanix` + `nkp install kommander`
        # (--wait defaults true) complete when CAPI is ready AND every enabled
        # application's Kustomization is Ready. PHASE 11.5 is that second half,
        # so its completion is the parity moment. Deriving it costs no time; an
        # earlier version polled for it separately and added 20 minutes to the
        # very run it was timing.
        try:
            gl = (ctx.artifacts / f"gates-{claim_name}.log").read_text(errors="ignore")
            m = re.search(r"\[(\d\d:\d\d:\d\d)\]   ALL KUSTOMIZATIONS READY", gl)
            if m:
                ctx.log.info(
                    f"PARITY BAR reached at {m.group(1)} - CAPI ready and every "
                    "application Kustomization Ready, which is what a traditional "
                    "create + install waits for")
        except Exception:  # noqa: BLE001 - a measurement must never fail a run
            pass
        # Announced once already when the clone came up, but that was minutes
        # and two screens of SSO-repair and gate output ago. Repeat it here,
        # where "the cluster is ready" is actually true, so the path is the
        # last thing on screen when a human takes over.
        _announce_kubeconfig(ctx, ctx.kubeconfig, "cluster ready")


@step("delete_claim")
def delete_claim(ctx, *, timeout: str = "15m") -> None:
    """Tear down a claimed clone: delete its VMs from Prism Central.

    Claims are disposable by design - the template is the durable artifact -
    so teardown is a VM sweep rather than a full nkp delete, and it polls the
    same way assert_no_leftover_vms does.
    """
    if ctx.config.dry_run or not ctx.cluster_name:
        return
    swept = ctx.pc.sweep(ctx.cluster_name)
    ctx.log.info(f"swept {swept} VM(s) of claim {ctx.cluster_name}")
    deadline = time.monotonic() + parse_duration(timeout)
    while time.monotonic() < deadline:
        if not ctx.pc.vms_named(ctx.cluster_name):
            ctx.log.info("claim fully deleted")
            return
        time.sleep(15)
    raise AssertionError(f"claim {ctx.cluster_name} VMs still present after {timeout}")


SHIM_TEMPLATE = '#!/bin/bash\n# nkp VERSION baseline shim (generated by resolve_baseline):\n# kommander-owned verbs -> kommander CLI, the rest -> konvoy.\nD="$(cd "$(dirname "$0")" && pwd)"\nif [ "$1" = install ] && [ "$2" = kommander ]; then shift 2; exec "$D/kommander" install kommander "$@"; fi\nif [ "$1" = upgrade ] && [ "$2" = kommander ]; then shift 2; exec "$D/kommander" upgrade kommander "$@"; fi\nif [ "$1" = upgrade ] && [ "$2" = catalogapp ]; then shift 1; exec "$D/kommander" "$@"; fi\nexec "$D/konvoy" "$@"\n'


@step("assert_app_config_applied")
def assert_app_config_applied(ctx, *, name: str = "", namespace: str = "",
                              timeout: str = "10m") -> None:
    """Assert the config override actually reached the app's HelmRelease.

    The platform contract (asserted by kommander's own kuttl tests): an
    AppDeployment with spec.configOverrides gets its override ConfigMap
    appended to the rendered HelmRelease's spec.valuesFrom. Checking only the
    AppDeployment would pass even if the override never made it into helm.
    """
    target, ns = _app_target(ctx, name, namespace)
    cm = ctx.recall("app_overrides_cm")
    if ctx.config.dry_run:
        return
    if not target or not cm:
        raise RuntimeError(
            "assert_app_config_applied needs a prior deploy_app with config_values"
        )

    app = ctx.kube.json(["get", "appdeployment", target, "-n", ns])
    ref = (app.get("spec", {}).get("configOverrides") or {}).get("name")
    if ref != cm:
        raise AssertionError(
            f"AppDeployment {ns}/{target} configOverrides={ref!r}, expected {cm!r}"
        )

    # A federated app renders its HelmRelease ON THE ATTACHED CLUSTER, so there
    # is no local valuesFrom to inspect. Kommander records what it federated on
    # the AppDeploymentInstance instead: spec.configOverrides carries the
    # source ConfigMap and the target name it is rewritten to. That is the
    # management-side proof the override travelled. Live-verified 2026-09-02:
    #   configOverrides: [{source: ConfigMap/istio-helm-config-overrides,
    #                      target: istio-helm-config-overrides}]
    wl_ns = ctx.recall("workload_namespace") or ""
    if wl_ns and ns == wl_ns:
        generated_name = f"{target}-config-overrides"
        insts = [i for i in _app_instances(ctx, target, "")
                 if i.get("metadata", {}).get("namespace") == ns]
        if not insts:
            raise AssertionError(
                f"{ns}/{target}: no AppDeploymentInstance, so no override could "
                "have been federated to the attached cluster")
        for inst in insts:
            for ov in ((inst.get("spec", {}) or {}).get("configOverrides") or []):
                src = (ov.get("source") or {}).get("name", "")
                tgt = (ov.get("target") or {}).get("name", "")
                if src in (cm, generated_name) or tgt in (cm, generated_name):
                    ctx.log.info(
                        f"{ns}/{target}: override federated to the attached cluster "
                        f"(source {src!r} -> target {tgt!r})")
                    return
        raise AssertionError(
            f"{ns}/{target}: the AppDeploymentInstance carries no configOverride "
            f"referencing {cm!r} or {generated_name!r} - the override did not "
            "reach the attached cluster")

    # An app is not always ONE HelmRelease named after it. kommander appends the
    # override to a HelmRelease selected by the exact app ID
    # (common/pkg/fluxkustomization/helpers.go:78-84, an anchored kustomize name
    # regex), which is why single-release apps like istio or reloader name their
    # HelmRelease literally after the app. istio-helm instead ships FIVE -
    # istio-helm-base/-cni/-istiod/-ztunnel/-gateway - so that selector matches
    # nothing and no valuesFrom entry is ever appended.
    #
    # Those apps get the override a different way: kommander's generator writes
    # the override ConfigMap into the cluster's git renamed to
    # "<appdeployment>-config-overrides"
    # (federation/pkg/controllers/helpers/appdeployments.go:125-127), and the
    # app's own templates hardcode that name in every release's valuesFrom.
    # Both routes are legitimate; both must be accepted, or this assertion
    # fails on a multi-release app whose override DID apply.
    generated = f"{target}-config-overrides"
    seen: dict = {}

    def _cm_exists(nm: str) -> bool:
        return bool(ctx.kube.json(["get", "configmap", nm, "-n", ns]))

    def wired() -> bool:
        seen.clear()
        for hr in _list_lenient(ctx, "helmrelease", ns):
            meta = hr.get("metadata", {})
            hname = meta.get("name", "")
            if hname != target and not hname.startswith(f"{target}-"):
                continue
            sources = [v.get("name") for v in
                       (hr.get("spec", {}).get("valuesFrom") or [])]
            seen[hname] = sources
            # The NAME being listed proves nothing: these entries are
            # `optional: true`, so a HelmRelease happily renders with the
            # reference dangling and the shipped defaults winning. Live-caught
            # 2026-09-01 - istio-helm listed istio-helm-config-overrides in all
            # five releases while no such ConfigMap existed, and the override
            # silently did not apply (HPA stayed at the default 2). The
            # ConfigMap has to be THERE.
            for candidate in (cm, generated):
                if candidate in sources and _cm_exists(candidate):
                    seen["_hit"] = hname
                    seen["_cm"] = candidate
                    return True
        return False

    try:
        wait_for(wired, ctx.log,
                 what=f"override for {target} to reach a HelmRelease's valuesFrom",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        raise AssertionError(
            f"the config override for {ns}/{target} never reached helm.\n"
            f"  looked for ConfigMap {cm!r} (the one this run created) or "
            f"{generated!r} (the name kommander rewrites it to in git)\n"
            f"  in the valuesFrom of every HelmRelease named {target} or "
            f"{target}-*\n"
            f"  found: { {k: v for k, v in seen.items() if not k.startswith('_')} or 'no HelmRelease matched that name at all'}\n"
            "  NOTE: a name listed in valuesFrom is not enough - those entries "
            "are optional, so the ConfigMap must actually exist. If the app's "
            "releases reference a fixed name, create the override under THAT "
            "name with deploy_app's config_overrides:."
        ) from exc
    ctx.log.info(f"config override reached HelmRelease {ns}/{seen['_hit']} "
                 f"via existing ConfigMap {seen['_cm']}")


@step("assert_jsonpath")
def assert_jsonpath(ctx, *, kind: str, name: str, path: str, equals: str,
                    namespace: str = "", timeout: str = "5m") -> None:
    """Assert a JSONPath on any object equals a value - the generic "did my
    config actually land on the workload" check.
    """
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert {kind}/{name} {path} == {equals!r}")
        return
    if not namespace:
        # fall back to the namespace the last deploy_app resolved
        namespace = ctx.recall("app_namespace") or ""
    args = ["get", kind, name, "-o", f"jsonpath={path}"]
    if namespace:
        args += ["-n", namespace]
    last = [""]

    def matches() -> bool:
        result = ctx.kube._run(args, check=False, quiet=True)
        last[0] = (result.stdout or "").strip()
        return last[0] == equals

    try:
        wait_for(matches, ctx.log, what=f"{kind}/{name} {path} == {equals!r}",
                 timeout_s=parse_duration(timeout), dry_run=False)
    except Exception as exc:
        raise AssertionError(
            f"{kind}/{name} {path}: expected {equals!r}, last saw {last[0]!r}"
        ) from exc
    ctx.log.info(f"asserted {kind}/{name} {path} == {equals!r}")


@step("resolve_baseline")
def resolve_baseline(ctx, *, version: str = "", binary: str = "",
                     key: str = "baseline_bin") -> None:
    """Give the upgrade scenarios their starting point with least effort.

    A developer testing an upgrade-path change provides EITHER a base NKP
    version (e.g. v2.17.0 - the CLIs are fetched from GitHub releases and
    shimmed automatically) OR a path to a binary they already have. The
    developer's own build (NKP_BIN) is what performs the upgrade, so "my
    change arrives as the new NKP" is exactly what gets tested.
    """
    import platform as _platform
    import subprocess

    if binary:
        ctx.remember(key, binary)
        ctx.log.info(f"baseline CLI: {binary}")
        return
    if not version:
        if ctx.config.dry_run:
            ctx.remember(key, "<baseline>")
            return
        raise RuntimeError(
            "provide a baseline: E2E_BASE_NKP_VERSION=v2.17.0 (fetched "
            "automatically) or E2E_BASELINE_NKP_BIN=/path/to/old/nkp"
        )
    if not version.startswith("v"):
        version = "v" + version

    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would fetch konvoy+kommander {version} and shim them as nkp")
        ctx.remember(key, f"<nkp-{version}>")
        return

    goos = "linux" if _platform.system() == "Linux" else "darwin"
    goarch = {"x86_64": "amd64", "amd64": "amd64",
              "arm64": "arm64", "aarch64": "arm64"}[_platform.machine()]
    dest = ctx.artifacts / f"baseline-{version}"
    dest.mkdir(parents=True, exist_ok=True)
    shim = dest / "nkp"
    if not shim.exists():
        ctx.log.info(f"fetching konvoy+kommander {version} ({goos}/{goarch}) from GitHub releases")
        for repo, asset, out in (
            ("mesosphere/konvoy2", "konvoy", "konvoy"),
            ("mesosphere/kommander-cli", "kommander", "kommander"),
        ):
            tgz = dest / (out + ".tgz")
            # Older releases ship darwin_amd64 only - Rosetta runs it fine,
            # so fall back across architectures rather than failing.
            arches = [goarch] + (["amd64"] if goarch != "amd64" else [])
            rc = None
            for arch in arches:
                rc = subprocess.run(
                    ["gh", "release", "download", version, "-R", repo,
                     "-p", f"{asset}_{version}_{goos}_{arch}.tar.gz",
                     "-O", str(tgz), "--clobber"],
                    capture_output=True, text=True, timeout=600,
                )
                if rc.returncode == 0:
                    if arch != goarch:
                        ctx.log.info(f"{asset} {version}: no {goarch} asset - using {arch}")
                    break
            if rc is None or rc.returncode != 0:
                raise RuntimeError(
                    f"cannot fetch {asset} {version} from {repo} "
                    f"(tried {', '.join(arches)}): " + rc.stderr.strip()[:200]
                )
            subprocess.run(["tar", "-xzf", str(tgz), "-C", str(dest)],
                           check=True, timeout=300)
        shim.write_text(SHIM_TEMPLATE.replace("VERSION", version))
        shim.chmod(0o755)
    probe = subprocess.run([str(shim), "version"], capture_output=True, text=True, timeout=120)
    head = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else "version unknown"
    ctx.log.info(f"baseline CLI ready: {shim} ({head})")
    ctx.remember(key, str(shim))


@step("pause")
def pause(ctx, *, message: str = "", timeout: str = "2h") -> None:
    """Stop here and hand the cluster to the developer.

    Nothing further touches the cluster until the run is resumed. Interactive
    runs resume on Enter; background runs (nohup/CI) resume when the file
    named in the log is created - both are printed, so there is no guessing.

    There is no "skip" switch. A pause is a deliberate part of a scenario, so
    turning it off is an edit to the scenario - comment the step out - not an
    environment variable that silently changes what a run does.
    """
    import sys

    ctx.log.banner("AUTOMATION STOPPED" + (f" - {message}" if message else ""), level=2)
    ctx.log.info("  the cluster is all yours; nothing will touch it until you resume")
    if ctx.kubeconfig:
        ctx.log.info(f"  kubeconfig: {ctx.kubeconfig}")
        ctx.log.info(f"  inspect:    kubectl --kubeconfig {ctx.kubeconfig} get pods -A")
    if ctx.config.dry_run:
        ctx.log.info("[dry-run] would pause here")
        return

    resume_file = ctx.artifacts / "resume"
    if sys.stdin.isatty():
        ctx.log.info(f"  resume:     press Enter here to continue")
        ctx.log.info(f"              (nothing happens on its own; the run fails after {timeout} if nobody does)")
        try:
            input()
            ctx.log.info("resumed by keyboard")
            return
        except EOFError:
            pass  # stdin closed mid-wait - fall through to the file mode
    ctx.log.info(f"  resume:     touch {resume_file}")
    deadline = time.monotonic() + parse_duration(timeout)
    while time.monotonic() < deadline:
        if resume_file.exists():
            resume_file.unlink()
            ctx.log.info("resumed by file")
            return
        time.sleep(5)
    raise RuntimeError(
        f"pause not resumed within {timeout} - failing so cleanup still runs. "
        f"Nobody touched {resume_file}. A pause is for a human: if this run has "
        "nobody attached (CI, nohup, a scheduled verification), comment the "
        "pause out of the scenario rather than leaving it to burn the whole "
        "timeout and then fail a run whose actual work had already passed.")


@step("assert_change_running")
def assert_change_running(ctx, *, repo: str, strict: bool = False,
                          timeout: str = "10m") -> None:
    """Assert the cluster is running the image built from ``repo``'s change.

    The expected image ref is DERIVED - change-set sha8 + the repo's delivery
    mapping - so the scenario never names an image. Works on both paths:
    freshly injected (create) or baked into the claimed template.
    """
    comps = ctx.recall("changeset_components") or []
    sha8 = next((c.split("@", 1)[1] for c in comps if c.startswith(repo + "@")), "")
    if not sha8:
        raise RuntimeError(
            f"assert_change_running: no change for repo {repo!r} in this run "
            f"(smart_cluster changes: {comps or 'none'})")
    deliveries = REPO_DELIVERY.get(repo)
    if not deliveries:
        raise RuntimeError(f"no delivery mapping for repo {repo!r}")
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would assert {repo}@{sha8} images are running")
        return
    for d in deliveries:
        expected = _dev_image_ref(d["image"], sha8)

        def running() -> bool:
            dep = ctx.kube.json(["get", "deployment", d["deployment"],
                                 "-n", d["namespace"]])
            images = [c.get("image", "") for c in
                      dep.get("spec", {}).get("template", {})
                         .get("spec", {}).get("containers", [])]
            return any(expected == i for i in images)

        wait_for(running, ctx.log,
                 what=f"{d['namespace']}/{d['deployment']} running {expected}",
                 timeout_s=parse_duration(timeout), dry_run=False)
        ctx.log.info(f"verified: {d['deployment']} runs YOUR build ({repo}@{sha8})")
        if strict:
            # The no-old-code proof: every pod generation is a ReplicaSet,
            # and restarts legitimately mint new generations with the SAME
            # image. The invariant is that EVERY generation's manager
            # container ran YOUR image - one generation with a GA manager
            # image anywhere in history is the violation.
            dep = ctx.kube.json(["get", "deployment", d["deployment"],
                                 "-n", d["namespace"]])
            uid = dep["metadata"]["uid"]
            rss = [r for r in ctx.kube.json(["get", "rs", "-n", d["namespace"]])
                   .get("items", [])
                   if any(o.get("uid") == uid for o in
                          r["metadata"].get("ownerReferences", []))]
            repo_part = expected.rsplit(":", 1)[0].rsplit("/", 1)[-1]
            offenders = []
            for r in rss:
                mgr = [c.get("image", "") for c in
                       r["spec"]["template"]["spec"].get("containers", [])
                       if repo_part.split("-dev-")[0] in c.get("image", "")
                       or c.get("name") in ("manager", "kommander-appmanagement")]
                if not any(i == expected for i in mgr):
                    offenders.append((r["metadata"]["name"], mgr))
            if not rss or offenders:
                ctx.remember("changeset_tainted", True)
                raise AssertionError(
                    f"no-old-code violated for {d['deployment']}: "
                    f"generation(s) with a non-change-set manager image: "
                    f"{offenders or 'no ReplicaSets found'}")
            ctx.log.info(
                f"STRICT proof: {len(rss)} generation(s), every one ran "
                f"{expected} - old code never ran on this cluster")


def _announce_kubeconfig(ctx, path, label: str) -> None:
    """Every step that produces a cluster prints this - the developer's other
    terminal needs the path, every time, without asking."""
    if not path:
        return
    ctx.log.banner(f"KUBECONFIG ({label})", level=2)
    ctx.log.info(f"  export KUBECONFIG={path}")
    ctx.log.info(f"  kubectl --kubeconfig {path} get nodes")


@step("override_component_image")
def override_component_image(ctx, *, component: str, image: str,
                             timeout: str = "10m") -> None:
    """Run YOUR pushed dev image in the cluster - the pipeline's proven vector.

    Drives instant-cluster/override_image.py: for kommander apps it writes the
    designed <app>-overrides ConfigMap and re-renders the HelmRelease, so flux
    keeps (not fights) the override. `component` accepts the @sub-image form,
    e.g. ``appmanagement@manager.image``.
    """
    import subprocess

    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would override {component} -> {image}")
        return
    ic_dir = _instant_cluster_dir()
    log_file = ctx.artifacts / "override-image.log"
    # HelmRelease names differ across install generations (appmanagement vs
    # kommander-appmanagement) - try the given name and its prefix variant.
    base, _, sub = component.partition("@")
    variants = [base]
    variants.append(base.replace("kommander-", "") if base.startswith("kommander-")
                    else f"kommander-{base}")
    rc = 1
    for cand in variants:
        target = cand + (f"@{sub}" if sub else "")
        ctx.log.info(f"overriding {target} -> {image}")
        with log_file.open("a") as sink:
            rc = subprocess.run(
                ["python3", str(ic_dir / "override_image.py"), str(ctx.kubeconfig),
                 "apply", f"{target}={image}"],
                cwd=ic_dir, stdout=sink, stderr=subprocess.STDOUT,
                timeout=parse_duration(timeout),
            ).returncode
        if rc == 0:
            break
        ctx.log.warn(f"override target {target} did not resolve; trying next variant")
    if rc != 0:
        tail = "\n".join(log_file.read_text().splitlines()[-6:])
        raise RuntimeError(f"image override failed (rc={rc}):\n{tail}")
    ctx.log.info(f"override applied; rollout begins (verify with assert_jsonpath)")


# ----------------------------------------------------- change-set smart create
#: how a repo's change reaches a running cluster: override target (component
#: syntax of override_image.py), image name in the dev registry, and where the
#: resulting workload runs so it can be asserted. Extend as repos join.
REPO_DELIVERY = {
    "kommander": [
        {"target": "kommander-appmanagement@manager.image",
         "image": "kommander2-appmanagement",
         "namespace": "kommander", "deployment": "kommander-appmanagement",
         # static chart values key + HR name variants: needed to PRE-SEED the
         # overrides ConfigMap before the first helm render, so the GA image
         # never starts (helm storage does not exist yet to discover these)
         "values_key": "controllerManager.containers.manager.image",
         "hr_names": ["kommander-appmanagement", "appmanagement"]},
    ],
}


def _ghcr_user() -> str:
    user = os.environ.get("GHCR_USER")
    if user:
        return user
    from pathlib import Path
    env_file = Path.home() / ".nkp-dev-registry.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "GHCR_USER" in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("GHCR_USER not set and ~/.nkp-dev-registry.env missing")


def _dev_image_ref(image_name: str, sha8: str) -> str:
    return f"ghcr.io/{_ghcr_user()}/nkp-dev:{image_name}-dev-{sha8}"


def _dev_image_exists(ref: str):
    """Is this dev image actually in the registry? True/False, or None = cannot tell.

    A sha with no pushed image does not fail at injection - it fails twenty
    minutes later as an ImagePullBackOff that reads like a cluster problem
    rather than "nobody built this commit". One HTTP call turns that into an
    instant, actionable error.

    Deliberately conservative: only a definitive 404 is a negative. No token,
    a refused token, or any transport error returns None, so a probe can
    never block a run that would otherwise have worked.
    """
    import base64
    import urllib.error
    import urllib.request

    host, _, rest = ref.partition("/")
    repo, _, tag = rest.rpartition(":")
    token = os.environ.get("GHCR_TOKEN", "")
    if host != "ghcr.io" or not tag or not token:
        return None
    req = urllib.request.Request(
        f"https://ghcr.io/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": f"Bearer {base64.b64encode(token.encode()).decode()}",
            "Accept": ("application/vnd.oci.image.index.v1+json,"
                       "application/vnd.docker.distribution.manifest.list.v2+json,"
                       "application/vnd.docker.distribution.manifest.v2+json"),
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:
        return None


def _preseed_entries(ctx, comps, build: bool = True):
    """Which pre-seed overrides a change-set implies, optionally building.

    ``build=False`` makes this a pure accessor. The post-create verification
    re-enumerates to check the ConfigMaps landed, and with build=True that
    re-enumeration could start a container build from inside a check - or
    surface a build error where the caller only asked a question.
    """
    entries = []
    for comp in comps or []:
        repo, _, sha8 = comp.partition("@")
        if repo in REPO_DELIVERY and build:
            # produce the image before pointing anything at it
            _build_and_push_image(ctx, repo, sha8)
        for d in REPO_DELIVERY.get(repo, []):
            if d.get("values_key"):
                entries.append((d, sha8))
    return entries


def _apply_preseed(ctx, kubeconfig, entries) -> None:
    import json as _json
    from .shell import run

    for d, sha8 in entries:
        ref = _dev_image_ref(d["image"], sha8)
        repo_part, _, tag = ref.rpartition(":")
        nested = {}
        cur = nested
        keys = d["values_key"].split(".")
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = {"repository": repo_part, "tag": tag}
        import yaml as _yaml
        vals = _yaml.safe_dump(nested, default_flow_style=False)
        ns = d["namespace"]
        run(["bash", "-c",
             f"kubectl --kubeconfig {kubeconfig} create namespace {ns} "
             f"--dry-run=client -o yaml | kubectl --kubeconfig {kubeconfig} apply -f -"],
            ctx.log, dry_run=False, quiet=True, check=False)
        for hr in d.get("hr_names", []):
            cm = {"apiVersion": "v1", "kind": "ConfigMap",
                  "metadata": {"name": f"{hr}-overrides", "namespace": ns},
                  "data": {"values.yaml": vals}}
            run(["bash", "-c", f"kubectl --kubeconfig {kubeconfig} apply -f -"],
                ctx.log, dry_run=False, quiet=True, stdin=_json.dumps(cm))
        ctx.log.info(
            f"pre-seeded overrides for {d['deployment']} -> {ref} "
            "(first render deploys YOUR image; GA never runs)")


def _start_preseed_watcher(ctx, comps, cluster_name):
    """Kommander core installs DURING `create cluster` on self-managed
    creates, so waiting for create to return is too late - the GA image
    would already be running. This watcher grabs the new cluster's
    kubeconfig from the BOOTSTRAP kind cluster's CAPI secret the moment the
    control plane initializes (~T+8 min, well before the kommander wave at
    ~T+20) and applies the override ConfigMaps immediately."""
    import base64
    import subprocess
    import threading

    entries = _preseed_entries(ctx, comps)
    if not entries or ctx.config.dry_run:
        return None
    state = {"done": False, "error": ""}

    def watch():
        deadline = time.monotonic() + 90 * 60
        kc_path = ctx.artifacts / f".preseed-{cluster_name}.conf"
        while time.monotonic() < deadline:
            kc = None
            if ctx.kubeconfig and pathlib_Path(ctx.kubeconfig).exists():
                kc = str(ctx.kubeconfig)
            else:
                r = subprocess.run(
                    ["kubectl", "--context", "kind-konvoy-capi-bootstrapper",
                     "get", "secret", f"{cluster_name}-kubeconfig",
                     "-n", "default", "-o", "jsonpath={.data.value}"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    kc_path.write_bytes(base64.b64decode(r.stdout.strip()))
                    kc = str(kc_path)
            if kc:
                probe = subprocess.run(
                    ["kubectl", "--kubeconfig", kc, "--request-timeout=10s",
                     "get", "--raw", "/healthz"],
                    capture_output=True, text=True, timeout=20)
                if probe.returncode == 0:
                    try:
                        _apply_preseed(ctx, kc, entries)
                        state["done"] = True
                        return
                    except Exception as exc:  # noqa: BLE001
                        state["error"] = str(exc)
            time.sleep(10)
        state["error"] = state["error"] or "timed out waiting for the new apiserver"

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    ctx.log.info("pre-seed watcher armed: overrides land the moment the new "
                 "apiserver answers, ahead of the kommander core wave")
    return state


from pathlib import Path as pathlib_Path


def _preseed_overrides(ctx, comps) -> None:
    """Write each changed component's overrides ConfigMap BEFORE the platform
    install, so the very first helm render already references YOUR image and
    the GA image never runs - not even once. The CM is the chart's designed
    optional valuesFrom hook; pre-existing content is simply consumed at
    revision 1."""
    import json as _json

    entries = _preseed_entries(ctx, comps)
    if not entries:
        return
    if ctx.config.dry_run:
        for d, sha8 in entries:
            ctx.log.info(f"[dry-run] would pre-seed overrides for {d['deployment']}")
        return
    _apply_preseed(ctx, str(ctx.kubeconfig), entries)
    return


def _cli_for_changes(ctx, comps):
    """konvoy2 in the change-set -> the cluster must be CREATED by a CLI
    built from that commit. Build it (content-addressed cache), wrap it in
    the standard shim (kommander verbs -> the GA nkp), remember it."""
    import subprocess
    from pathlib import Path

    sha8 = next((c.split("@", 1)[1] for c in comps or []
                 if c.startswith("konvoy2@")), "")
    if not sha8:
        return None
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would build the CLI from konvoy2@{sha8}")
        return None
    cache = Path.home() / ".cache/nkp-worktrees"
    wt = cache / f"konvoy2-{sha8}"
    if not (wt / "Makefile").exists():
        cache.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-C", str(Path.home() / "Documents/nkp/konvoy2"),
                            "worktree", "add", "--detach", str(wt), sha8],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"worktree for konvoy2@{sha8}: {r.stderr[:200]}")
    ctx.log.info(f"building CLI from konvoy2@{sha8} (cached when seen before)")
    build = subprocess.run(
        [str(Path.home() / "Documents/nkp/dev-e2e/build-nkp-fast.sh"), "-q"],
        cwd=wt, capture_output=True, text=True, timeout=900)
    if build.returncode != 0:
        raise RuntimeError(f"CLI build failed:\n{build.stderr[-400:]}")
    konvoy = build.stdout.strip().splitlines()[-1]
    shim_dir = ctx.artifacts / f"cli-{sha8}"
    shim_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    # The goreleaser snapshot self-reports <base>-dev-SNAPSHOT-<sha>, which
    # seeds kommander-core at a -dev version the GA installer then refuses.
    # Rebuild the binary with GA-version ldflags (the orchestrator's proven
    # slowpath recipe) - the snapshot pass above already ran the manifest
    # hooks, so this is a plain compile.
    base = os.environ.get("E2E_BASE", "v2.18.0")
    maj, mino = base.lstrip("v").split(".")[:2]
    ld = (f"-X github.com/mesosphere/dkp-cli-runtime/core/cmd/version.gitVersion={base} "
          f"-X github.com/mesosphere/dkp-cli-runtime/core/cmd/version.major={maj} "
          f"-X github.com/mesosphere/dkp-cli-runtime/core/cmd/version.minor={mino}")
    build_env = {k: v for k, v in os.environ.items() if k != "GOROOT"}
    build_env["GOCACHE"] = str(Path.home() / ".cache/nkp-ga-gocache")
    ga_build = subprocess.run(
        ["devbox", "run", "--", "go", "build", "-trimpath", "-ldflags", ld,
         "-o", str(shim_dir / "konvoy"), "./cmd/konvoy"],
        cwd=wt, env=build_env, capture_output=True, text=True, timeout=900)
    if ga_build.returncode != 0:
        ctx.log.warn(f"GA-ldflags rebuild failed ({ga_build.stderr[-200:]}); "
                     "using the snapshot binary")
        shutil.copy2(konvoy, shim_dir / "konvoy")
    # kommander-owned verbs go to the GA nkp; everything else runs YOUR build
    ga = str(ctx.config.nkp_bin)
    shim = shim_dir / "nkp"
    shim.write_text(SHIM_TEMPLATE.replace('"$D/kommander"', f'"{ga}"'))
    shim.chmod(0o755)
    # a snapshot build embeds a dev bootstrap-image tag that exists nowhere;
    # the CLI checks locally before pulling, so a retag of the GA image is
    # all it takes (best-effort - docker may not be running for claim paths)
    base = os.environ.get("E2E_BASE", "v2.18.0")
    short9 = subprocess.run(
        ["git", "-C", str(Path.home() / "Documents/nkp/konvoy2"),
         "rev-parse", f"--short=9", sha8],
        capture_output=True, text=True).stdout.strip()
    if short9:
        subprocess.run(["docker", "tag", f"mesosphere/konvoy-bootstrap:{base}",
                        f"mesosphere/konvoy-bootstrap:{base}-dev-SNAPSHOT-{short9}"],
                       capture_output=True, timeout=60)
    ctx.log.info(f"change-set CLI ready: {shim} (konvoy2@{sha8} + GA kommander)")
    ctx.remember("changeset_cli", str(shim))
    return str(shim)


def _diff_base(repo, sha: str, base: str) -> str:
    """The point YOUR branch diverged, not the release tag.

    Diffing a feature branch against a version TAG counts every commit the
    release branch has taken since that tag, so a one-app change reported five
    apps and would have synced four of them for nothing. The merge-base is the
    only honest answer to "what did this branch change".
    """
    import subprocess

    mb = subprocess.run(["git", "-C", str(repo), "merge-base", base, sha],
                        capture_output=True, text=True, timeout=60)
    if mb.returncode == 0 and mb.stdout.strip():
        return mb.stdout.strip()
    # An unresolvable base is NOT "no common ancestor": falling back to the
    # literal string makes the diff below fail with rc=128 and empty stdout,
    # which reads exactly like "this commit changes nothing" - and the delivery
    # guards then raise that, naming the wrong cause. Say which it is.
    check = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                            "--quiet", base + "^{commit}"],
                           capture_output=True, text=True, timeout=60)
    if check.returncode != 0:
        raise RuntimeError(
            f"cannot resolve the diff base {base!r} in {repo}. Change detection "
            "compares your commit against it, so an unresolvable base would look "
            "identical to 'nothing changed'. Fetch it, or set the matching "
            "E2E_*_BASE to a ref that exists.")
    return base


def _kapps_changed_paths(sha: str) -> set:
    """Top-level directories a kommander-applications commit touches vs base."""
    import subprocess
    from pathlib import Path

    src = _kapps_src()
    base = _diff_base(src.git_root, sha, src.default_base)
    r = subprocess.run(["git", "-C", str(src.git_root), "diff", "--name-only", base, sha,
                        "--", src.prefix or "."],
                       capture_output=True, text=True, timeout=60)
    return {src.strip(p).split("/")[0] for p in r.stdout.splitlines() if p.strip()}


def _kapps_changed_apps(sha: str) -> list:
    """Which applications a kommander-applications commit touches vs its base."""
    import subprocess
    from pathlib import Path

    src = _kapps_src()
    base = _diff_base(src.git_root, sha, src.default_base)
    r = subprocess.run(["git", "-C", str(src.git_root), "diff", "--name-only", base, sha,
                        "--", src.prefix or "."],
                       capture_output=True, text=True, timeout=60)
    paths = [src.strip(p) for p in r.stdout.splitlines()]
    apps = sorted({p.split("/")[1] for p in paths
                   if p.startswith("applications/") and len(p.split("/")) > 2})
    return apps



def _kommander_touched_dirs(sha8: str) -> set:
    """Top-level directories a kommander commit touches vs the k-apps diff base."""
    import subprocess
    src = _kapps_src()
    base = _diff_base(src.git_root, sha8, src.default_base)
    r = subprocess.run(["git", "-C", str(src.git_root), "diff", "--name-only", base, sha8],
                       capture_output=True, text=True, timeout=60)
    return {p.split("/")[0] for p in r.stdout.splitlines() if p.strip()}

def _deliver_changes(ctx, comps, on_upgrade: bool = False) -> None:
    """Make the change-set's images the ones RUNNING in the cluster.

    Runs on both paths: after a traditional create (first delivery) and after
    a claim (idempotent - a born-with template re-applies to the same values,
    a vanilla template gets the images now). Waits for each injection target's
    HelmRelease first: install/boot can complete before every HR is rendered.
    """
    if ctx.config.dry_run:
        for comp in comps or []:
            ctx.log.info(f"[dry-run] would deliver {comp}")
        return
    # a chart change and a k-apps change in the same set are coupled: k-apps
    # names the tag, charts must produce it. Resolve it up front so delivery
    # order in `changes:` cannot decide whether the run works.
    kapps_sha8 = _kapps_sha8(comps)
    src = _kapps_src()
    ctx.log.info(f"applications tree: {src}")
    for comp in comps or []:
        repo, _, sha8 = comp.partition("@")
        content_only = False
        if repo == "kommander" and src.line == "subtree" and src.sha8 == sha8:
            touched = _kommander_touched_dirs(sha8)
            content_only = bool(touched) and touched <= {"kommander-applications"}
            if content_only:
                ctx.log.info(f"kommander@{sha8} touches only kommander-applications/ - "
                             "application content only, no image is built or injected")
        if repo in REPO_DELIVERY and not content_only:
            # produce the image before pointing anything at it
            _build_and_push_image(ctx, repo, sha8)
        for d in ([] if content_only else REPO_DELIVERY.get(repo, [])):
            hr_base = d["target"].split("@")[0]

            def hr_exists() -> bool:
                for hr in (hr_base, f"kommander-{hr_base}", hr_base.replace("kommander-", "")):
                    if ctx.kube.json(["get", "hr", hr, "-n", d["namespace"]]):
                        return True
                return False

            wait_for(hr_exists, ctx.log,
                     what=f"HelmRelease for {hr_base} to exist",
                     timeout_s=1800, dry_run=ctx.config.dry_run)
            ref = _dev_image_ref(d["image"], sha8)
            if _dev_image_exists(ref) is False:
                raise RuntimeError(
                    f"dev image {ref} is not in the registry. The change-set "
                    f"resolved {repo} to {sha8}, but nothing was ever built and "
                    "pushed for that commit - injecting it would leave the "
                    "cluster in ImagePullBackOff. Build and push the image for "
                    "this commit, or point the scenario at a commit that has one.")
            override_component_image(ctx, component=d["target"], image=ref)
        if repo == "charts":
            _deliver_charts(ctx, sha8, kapps_sha8=kapps_sha8)
            continue
        if repo == "kommander-applications":
            _deliver_kapps(ctx, sha8, on_upgrade=on_upgrade)
        elif repo == "kommander" and src.line == "subtree" and src.sha8 == sha8 \
                and _kapps_changed_paths(sha8):
            # main line: the same commit carries application content
            _deliver_kapps(ctx, sha8, on_upgrade=on_upgrade)
        elif repo == "konvoy2":
            pass  # delivered as the CLI that created the cluster
        elif repo not in REPO_DELIVERY:
            # HARD FAIL, not a warning. A warning here produces the exact
            # failure this framework exists to catch: the change-set resolves,
            # the cluster builds, every assertion passes - and the cluster is
            # running the SHIPPED code, because nothing was ever injected. A
            # CAREN or CAPX developer would read that green run as "my change
            # is good". Refusing loudly is the only honest answer until a
            # delivery route exists. (Found by capability audit 2026-08-31.)
            raise RuntimeError(
                f"cannot deliver {repo}@{sha8}: no delivery route is implemented "
                f"for {repo!r}. Routes exist for: "
                f"{', '.join(sorted(set(REPO_DELIVERY) | {'konvoy2', 'kommander-applications'}))}. "
                f"Running anyway would build a cluster with the SHIPPED {repo} "
                "code and pass every assertion - a green run that tested "
                "nothing of yours. Add a route to REPO_DELIVERY (image override) "
                "or remove this repo from `changes:`.")


def _deliver_kapps(ctx, sha8: str, on_upgrade: bool = False) -> None:
    """Sync the changed kommander-applications apps into the cluster's own
    kommander.git via the proven devloop vector (~30s per app)."""
    import subprocess
    from pathlib import Path

    apps = _kapps_changed_apps(sha8)
    if not apps:
        # The git-sync vector carries applications/<app>/ only. Whether an
        # empty list is fatal depends on what else the commit touches and on
        # which path we are: an upgrade ALSO publishes the whole tree as the
        # version's OCI bundle, so a common/ or charts/ change still lands.
        touched = _kapps_changed_paths(sha8)
        base = _kapps_src().default_base
        if not touched and not on_upgrade:
            raise RuntimeError(
                f"kommander-applications@{sha8} changes nothing vs {base} - "
                "either it is not a change, or it has already landed on that "
                "branch, and on the create path the git-sync vector has nothing "
                "to carry. Point it at the commit under test, set E2E_KAPPS_BASE "
                "to the branch it forked from, or drop it from changes:.")
        if not touched:
            # on an upgrade the entry still has a job: it names the tree that
            # gets published as the target version's OCI bundle, and that path
            # never consults a diff
            ctx.log.info(
                f"kommander-applications@{sha8} changes nothing vs {base} "
                "(already landed, or the base is wrong) - nothing for the "
                "git-sync vector; it is still the source of this upgrade's bundle")
            return
        if not on_upgrade:
            raise RuntimeError(
                f"kommander-applications@{sha8} changes no application vs "
                f"{base} - it touches {', '.join(sorted(touched)[:4])} - and on "
                "the create path the devloop git-sync only carries "
                "applications/<app>/, so nothing would reach the cluster and "
                "the run would still pass.\n"
                "  Deliver it through upgrade_cluster instead, which publishes "
                "the whole tree as the target version's OCI bundle.")
        ctx.log.info(
            f"kommander-applications@{sha8} changes no application directory "
            f"(touches {', '.join(sorted(touched)[:4])}) - nothing for the "
            "git-sync vector; the OCI bundle published for this upgrade "
            "carries it")
        return
    src = _kapps_src()
    repo = src.git_root
    # Code lives with code: the devloop scripts ship inside instant-cluster/,
    # resolved the same way claim.py is. SPEEDSTART_DIR is for STATE and no
    # longer decides which script runs (2026-09-07: the standalone repo must
    # not depend on a sibling checkout for a delivery vector).
    script = _instant_cluster_dir() / "devloop" / "devloop-kapps.sh"
    # the sync copies from the WORKING TREE - pin it to the change-set commit
    cur = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if cur != sha8:
        raise RuntimeError(
            f"the applications checkout ({src.line}: {repo}) is at {cur}, the change-set "
            f"wants {sha8} - check out that commit (the working tree is the delivery source)")
    for app in apps:
        ctx.log.info(f"delivering kommander-applications/{app} @ {sha8} (devloop sync)")
        log_file = ctx.artifacts / f"kapps-{app}.log"
        with log_file.open("a") as sink:
            rc = subprocess.run(
                ["bash", str(script), str(ctx.kubeconfig), str(src.dir), app],
                stdout=sink, stderr=subprocess.STDOUT, timeout=600).returncode
        log_text = log_file.read_text()
        if rc != 0 and "could not locate app dir" in log_text:
            # the app is disabled in this profile: no dir in the cluster git,
            # no delivery target - and no old code of it runs either
            ctx.log.info(f"kommander-applications/{app}: disabled in this "
                         "profile - nothing to deliver, skipping")
            continue
        if rc != 0 and "no changes to commit" in log_text:
            # The content is already what the branch says - a previous
            # delivery of the same commit landed it. That IS the delivered
            # state; the sync's later HR poke failing must not undo the verdict
            # (2026-09-07: a re-run against a cluster that already carried the
            # change-set failed here for exactly this reason).
            ctx.log.info(f"kommander-applications/{app}: already in sync with "
                         f"{sha8} - nothing to push")
            continue
        if rc != 0 and "pushed applications/" not in log_text:
            tail = "\n".join(log_text.splitlines()[-6:])
            raise RuntimeError(f"kapps delivery of {app} failed (rc={rc}):\n{tail}")
        if rc != 0:
            # the sync's own HR verification fails when the app is not yet
            # ENABLED - but the content is in git, which is the delivery:
            # the app's FIRST render (at deploy_app) uses your charts.
            ctx.log.info(f"kommander-applications/{app}: content pushed "
                         "(app not yet enabled - first render will be yours)")
        else:
            ctx.log.info(f"kommander-applications/{app}: delivered")


def _resolve_ref(repo: str, ref: str):
    """Resolve a branch/ref of one of the nkp repos to a commit sha.

    Local checkout first (the developer's uncommitted world is a checkout
    away), then origin/<ref> in that checkout, then a network ls-remote as
    the last resort. Returns (sha, where).
    """
    import subprocess
    from pathlib import Path

    repo_dir = Path(os.environ.get("NKP_REPOS_DIR",
                    str(Path.home() / "Documents/nkp"))) / repo
    if repo_dir.is_dir():
        for candidate, where in ((ref, "local"), (f"origin/{ref}", "origin")):
            r = subprocess.run(["git", "-C", str(repo_dir), "rev-parse",
                                "--verify", "--quiet", candidate + "^{commit}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout.strip(), where
        r = subprocess.run(["git", "-C", str(repo_dir), "ls-remote", "origin", ref],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.split()[0], "ls-remote"
    hint = ""
    if repo == "kommander-applications" and (_repos_dir() / "kommander" / "kommander-applications" / "applications").is_dir():
        hint = (" On the main line the applications tree lives inside the kommander repository "
                "(kommander/kommander-applications/); name the change as kommander@<ref>.")
    raise RuntimeError(
        f"cannot resolve {repo}@{ref}: no checkout at {repo_dir} and no remote match.{hint}"
    )


#: repos a change-set can actually be DELIVERED to a running cluster.
#: konvoy2 arrives as the CLI that creates the cluster; kommander-applications
#: via a git push into the cluster's own kommander.git; anything in
#: REPO_DELIVERY via an image override. A repo NOT listed here cannot be
#: delivered at all, so accepting one would build a cluster running the
#: SHIPPED code and pass every assertion - a green run that tested nothing.
DELIVERABLE_REPOS = {"konvoy2", "kommander-cli", "kommander-applications", "charts"}


def _resolve_changes(ctx, changes) -> list[str]:
    """['kommander@my-branch', ...] -> sorted ['kommander@<sha8>', ...]."""
    comps = []
    for change in changes or []:
        # An OPTIONAL change-set is written `changes: ["${E2E_CHANGE}"]`, which
        # interpolates to [""] when the developer has nothing to deliver. That
        # is "no changes", not a malformed entry - and it lets a scenario show
        # the option in the file instead of hiding it behind an env var nobody
        # knows exists.
        if not str(change).strip():
            continue
        repo, _, ref = str(change).partition("@")
        if not ref:
            raise RuntimeError(f"changes entries are repo@branch, got {change!r}")
        # Reject BEFORE anything is built. The delivery step also refuses, but
        # by then a cluster has been created - so an unsupported repo would
        # cost ~90 minutes before saying so.
        if repo not in (DELIVERABLE_REPOS | set(REPO_DELIVERY)):
            raise RuntimeError(
                f"changes: {repo!r} has no delivery route, so its code could "
                "never reach the cluster - the run would build, assert, and "
                "pass while testing the SHIPPED code. Deliverable today: "
                f"{', '.join(sorted(DELIVERABLE_REPOS | set(REPO_DELIVERY)))}. "
                "Add a route to REPO_DELIVERY before testing this repo.")
        if ctx.config.dry_run:
            ctx.log.info(f"[dry-run] would resolve {repo}@{ref}")
            comps.append(f"{repo}@{ref}")
            continue
        sha, where = _resolve_ref(repo, ref)
        ctx.log.info(f"resolved {repo}@{ref} -> {sha[:8]} ({where})")
        comps.append(f"{repo}@{sha[:8]}")
    return sorted(comps)


# The developer's own E2E_BASE, captured before any scenario can overwrite
# it. A scenario asking for a GA base sets E2E_BASE process-wide (the hash,
# the GA ldflags and the applications diff all read it), so without this the
# NEXT scenario in the same run would inherit the previous one's base,
# compute a different hash, miss its template and rebuild for an hour.
_ENV_BASE_DEFAULT = os.environ.get("E2E_BASE", "v2.18.0")


def _changeset_hash(ctx, control_plane: int = 1, workers: int = 3,
                    comps: list | None = None) -> str:
    """Identity of the cluster content: base + component overrides + topology.

    E2E_BASE (default v2.18.0) + E2E_COMPONENTS ("repo@sha,repo@sha" - the
    orchestrator exports this for request-driven runs; empty for plain runs)
    + the node topology. Topology is part of the identity because a frozen
    template's node count is fixed: the claim machinery clones exactly the
    frozen VMs (a 3CP claim even re-forms etcd differently), so a 3cp
    scenario must never claim a 1cp template. Same inputs => same hash =>
    the same frozen template can serve the run.
    """
    import hashlib
    base = os.environ.get("E2E_BASE", "v2.18.0")
    if comps is None:
        comps = sorted(filter(None, os.environ.get("E2E_COMPONENTS", "").split(",")))
    key = base + "|" + "|".join(comps) + f"|{control_plane}cp{workers}w"
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _template_missing_vms(ctx, name: str) -> list[str]:
    """Manifest VM uuids that are no longer on Prism Central.

    2026-09-07: a lab-side sweep removed every powered-off VM on the shared
    PC - all seven frozen templates, 28 VMs. The registry still listed them,
    create_cluster said "template exists - claiming", and claim.py died four
    seconds later on the first missing uuid. A template is the VMs, not the
    manifest; check the PC before deciding to claim.
    """
    import json as _json
    from pathlib import Path
    sd = Path(os.environ.get("SPEEDSTART_DIR", str(Path.home() / "Documents/nkp/speedstart-state")))
    try:
        fm = _json.loads((sd / f"{name}-freeze-manifest.json").read_text())
    except (OSError, ValueError):
        return ["<no freeze manifest on disk>"]
    missing = []
    for v in fm.get("vms") or []:
        uuid = v.get("uuid") or ""
        try:
            ctx.pc._call("GET", f"/api/nutanix/v3/vms/{uuid}")
        except Exception:  # noqa: BLE001 - 404 or otherwise: not there
            missing.append(f"{uuid[:8]} ({v.get('machine', '?')})")
    return missing


def _forget_template(h: str) -> None:
    import json as _json
    from pathlib import Path
    path = _template_registry_path()
    try:
        reg = _json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return
    if h in reg:
        reg.pop(h)
        Path(path).write_text(_json.dumps(reg, indent=2, sort_keys=True))


def _template_registry_path():
    from pathlib import Path
    state = os.environ.get("SPEEDSTART_DIR")
    if state:
        return Path(state) / "templates.json"
    # No state home configured: keep the registry next to the claim machinery
    # so a single-machine setup still works without any env.
    return _instant_cluster_dir() / "templates.json"


def _template_registry() -> dict:
    import json
    p = _template_registry_path()
    try:
        return json.load(open(p))
    except Exception:
        return {}


def _freeze_fingerprint(ctx) -> dict:
    """What the cluster IS at the moment it still matches its change-set hash.

    A template's registry key promises its content, but freeze runs in
    cleanup - after a scenario may have upgraded the very cluster it is
    about to freeze. Registering that would make the NEXT run claim an
    already-upgraded cluster, whose upgrade then no-ops: a green run that
    tested nothing. ``finish_cluster`` compares this against the cluster at
    cleanup and refuses to freeze a drifted one.
    """
    if ctx.config.dry_run:
        return {}
    try:
        return {
            "platform": ctx.nkp.platform_version(ctx.kubeconfig),
            "kubelet": sorted({n["status"]["nodeInfo"]["kubeletVersion"]
                               for n in ctx.kube.nodes()}),
        }
    except Exception as exc:  # a probe must never lose a good create
        ctx.log.warn(f"could not fingerprint {ctx.cluster_name} for freeze: {exc}")
        return {}


def _settle(ctx, expected_nodes: int, timeout: str = "30m") -> None:
    """A cluster-producing step does not return until the cluster is usable.

    Scenarios used to write `wait_nodes_ready` after `create_cluster`, which
    reads as though creating were asynchronous. It is not: `nkp create cluster`
    blocks, and the claim path runs identity gates. What the explicit wait
    really added was the node COUNT - and the step already knows the topology
    it asked for, so it can check that itself. Forgetting it was the failure
    mode; nobody ever wanted a create that returned with a node missing.
    """
    if ctx.config.dry_run:
        return
    ctx.kube.wait_nodes_ready(expected_nodes, timeout_s=parse_duration(timeout))
    ctx.log.info(f"cluster is up: {expected_nodes} node(s) Ready")


@step("create_cluster")
def smart_cluster(ctx, *, control_plane: int = 1, workers: int = 3,
                  changes: list | None = None, version: str = "",
                  bin_path: str = "", machine_image: str = "",
                  kubernetes_version: str = "", worker_vcpus: int = 0,
                  worker_memory: int = 0, timeout: str = "90m") -> None:
    """Get a cluster: claim a frozen template if one matches, else create.

    The decision key hashes (base version | your changes | topology). Hit ->
    ~10-minute claim of a cluster frozen from exactly this content. Miss ->
    traditional create (+ platform + image delivery); pair with
    ``finish_cluster`` in cleanup, which freezes the result so the SECOND
    run of the same change-set claims.

    ``version:`` asks for a GA base (e.g. v2.17.0) - the GA binaries are
    fetched and used for create+install, and ``changes`` must be empty (a GA
    base is pristine by definition; your build acts on it via
    upgrade_cluster). ``machine_image``/``kubernetes_version`` belong to the
    scenario: set them here, not in the terminal (env vars remain the
    fallback).
    """
    if version and changes:
        raise RuntimeError("create_cluster: version (GA base) and changes are "
                           "mutually exclusive - a GA base is pristine")
    # bin_path names the CLI to CREATE with when there is no change-set to build
    # one from. With changes: the framework compiles the CLI itself, so the two
    # are mutually exclusive - otherwise it is ambiguous which binary wins.
    if bin_path and changes:
        raise RuntimeError(
            "create_cluster: bin_path and changes are mutually exclusive - a "
            "change-set builds its own CLI, so naming a binary too is ambiguous")
    if bin_path:
        from pathlib import Path as _P

        if not _P(bin_path).expanduser().exists():
            raise RuntimeError(f"create_cluster: bin_path {bin_path!r} does not exist")
        ctx.remember("bin_path_cli", str(_P(bin_path).expanduser()))
        ctx.log.info(f"creating with the CLI at {bin_path}")
    img = machine_image or ctx.config.machine_image
    if ctx.pc.image_exists(img) is False:
        raise RuntimeError(
            f"create_cluster: machine image {img!r} is not in Prism's image "
            "store, so every node would fail to provision. Check the name "
            "against the images list before starting a build.")
    # Set unconditionally, so each scenario's base is its own: a GA scenario
    # earlier in the same run must not leave E2E_BASE behind for this one.
    os.environ["E2E_BASE"] = version or _ENV_BASE_DEFAULT
    comps = _resolve_changes(ctx, changes) if changes else None
    if comps:
        ctx.remember("changeset_components", comps)
    h = _changeset_hash(ctx, control_plane, workers, comps)
    reg = _template_registry()
    entry = reg.get(h)
    if entry and not ctx.config.dry_run:
        gone = _template_missing_vms(ctx, entry["name"])
        if gone:
            ctx.log.warn(f"change-set {h} ({control_plane}cp{workers}w): template "
                         f"{entry['name']} is registered but {len(gone)} of its VM(s) "
                         f"no longer exist on the PC: {', '.join(gone)} - forgetting it "
                         "and creating traditionally (the create will re-freeze)")
            _forget_template(h)
            entry = None
    if entry:
        ctx.log.info(f"change-set {h} ({control_plane}cp{workers}w): "
                     f"template {entry['name']} exists - claiming")
        # the scenario's timeout must govern BOTH paths. It used to apply only
        # to traditional_create, so a claim silently kept claim_cluster's 20m
        # default - and a slow claim (measured 9-12 min normally, 15+ under
        # load) died at exactly 20:00 with the cluster half-built.
        claim_cluster(ctx, template=entry["name"], template_vip=entry["vip"],
                      template_lb=entry.get("lb", entry["vip"]), gates=True,
                      timeout=timeout)
        ctx.remember("smart_mode", "claimed")
        # idempotent: born-with templates re-verify, vanilla templates get
        # the change-set's images here - either way the cluster RUNS them
        _deliver_changes(ctx, comps)
        _settle(ctx, control_plane + workers)
        return
    topo = f"{control_plane}cp{workers}w"
    ctx.log.info(f"change-set {h} ({topo}): no template - creating traditionally "
                 f"(finish_cluster will freeze it as "
                 f"nkp-tmpl-{os.environ.get('E2E_BASE','v2.18.0')}-{h}-{topo})")
    # Mark the run "incomplete" BEFORE anything can create a VM. If a create
    # raises, finish_cluster must find a mode it recognises and KEEP the
    # cluster; without this, smart_mode is unset, finish_cluster falls through
    # to delete_cluster, and an hour of build is destroyed - the exact
    # opposite of this framework's stated rule that a half-built cluster is
    # worth more than a tidy teardown. Live 2026-08-30: a GA create timed out
    # waiting for KommanderCore while the cluster was 13/14 converged and
    # still settling; deleting that would have thrown away a usable baseline.
    ctx.remember("smart_mode", "created-incomplete")
    ctx.remember("freeze_topology", topo)
    if version:
        if bin_path:
            # an explicit binary replaces the GitHub-release fetch entirely
            ctx.remember("baseline_bin", ctx.recall("bin_path_cli"))
        else:
            resolve_baseline(ctx, version=version)
        traditional_create(ctx, control_plane=control_plane, workers=workers,
                           binary_from="baseline_bin",
                           machine_image=machine_image,
                           kubernetes_version=kubernetes_version,
                           worker_vcpus=worker_vcpus, worker_memory=worker_memory,
                           timeout=timeout)
        ctx.remember("smart_mode", "created-incomplete")
        ctx.remember("freeze_as", h)
        ctx.remember("freeze_topology", topo)
        install_platform(ctx, binary_from="baseline_bin", timeout="50m",
                         disable_apps=[
                             "rook-ceph", "rook-ceph-cluster", "grafana-logging",
                             "logging-operator", "velero", "grafana-loki-v3?",
                             "grafana-loki?", "ai-navigator-app?", "cloudnative-pg?"])
        ctx.remember("freeze_fingerprint", _freeze_fingerprint(ctx))
        ctx.remember("smart_mode", "created")
        _settle(ctx, control_plane + workers)
        return
    cli_shim = _cli_for_changes(ctx, comps) if comps else None
    if bin_path and not cli_shim:
        cli_shim = ctx.recall("bin_path_cli")
        ctx.remember("changeset_cli", cli_shim)
    # NO-OLD-CODE: kommander core installs DURING the create, so the override
    # hooks must land the moment the new apiserver answers - a watcher does
    # that from the bootstrap cluster's CAPI kubeconfig secret.
    preseed = _start_preseed_watcher(ctx, comps, ctx.cluster(""))
    traditional_create(ctx, control_plane=control_plane, workers=workers,
                       binary_from="changeset_cli" if cli_shim else "",
                       machine_image=machine_image,
                       kubernetes_version=kubernetes_version or "@default",
                       worker_vcpus=worker_vcpus, worker_memory=worker_memory,
                       timeout=timeout)
    if preseed is not None:
        # Verify against the CLUSTER, not the watcher thread - a rescue may
        # have applied the CMs by hand while the thread was dead.
        probe = ctx.kube.json(["get", "cm", "-n", "kommander"])
        names = {i["metadata"]["name"] for i in probe.get("items", [])}
        missing = [d["hr_names"][0] + "-overrides"
                   for d, _ in _preseed_entries(ctx, comps, build=False)
                   if not any(f"{hr}-overrides" in names for hr in d["hr_names"])]
        if missing:
            raise RuntimeError(
                f"no-old-code pre-seed missing on the cluster: {missing} "
                f"(watcher: {preseed.get('error') or 'no error recorded'}) "
                "- refusing to continue into a tainted install")
        ctx.log.info("no-old-code gate: override CMs verified on the cluster")
    # From here the cluster EXISTS: if anything below fails, cleanup must
    # keep it (an hour of create is worth more than a tidy teardown).
    ctx.remember("smart_mode", "created-incomplete")
    ctx.remember("freeze_as", h)
    ctx.remember("freeze_topology", topo)
    # NO-OLD-CODE GUARANTEE: seed the override hooks before the install so
    # revision 1 of every changed component already runs your image.
    _preseed_overrides(ctx, comps)
    # A claimable template must carry the platform - a bare CAPI cluster
    # would freeze fine but every claim of it would miss kommander. Lean
    # profile: heavy storage/logging apps stay off the 3-worker footprint.
    install_platform(ctx, timeout="50m", disable_apps=[
        "rook-ceph", "rook-ceph-cluster", "grafana-logging",
        "logging-operator", "velero", "grafana-loki-v3?", "grafana-loki?",
        "ai-navigator-app?", "cloudnative-pg?",
    ])
    # Deliver the change-set's images: the resolved sha8 IS the registry tag,
    # so nothing here is configured - branch in, running image out. The
    # override lives in at-rest cluster state, so the freeze below produces a
    # template genuinely BORN WITH these images.
    _deliver_changes(ctx, comps)
    ctx.remember("freeze_fingerprint", _freeze_fingerprint(ctx))
    ctx.remember("smart_mode", "created")  # fully delivered - freezable
    _settle(ctx, control_plane + workers)


@step("finish_cluster")
def finish_cluster(ctx, *, keep_on_freeze_failure: bool = True) -> None:
    """The smart counterpart of delete_cluster.

    Claimed cluster -> sweep the clone (template is the durable artifact).
    Created cluster -> FREEZE it under its change-set name and register it,
    so this create is the last one this change-set ever pays for. Freeze
    runs at cleanup: it needs quiesce + power-off, which cannot overlap
    with an actively-tested cluster - "async" here means async to the
    developer, whose results are already in.
    """
    import json
    import subprocess

    from .netpool import release as _release_leases

    _release_leases(ctx.cluster_name or "", ctx.log)
    mode = ctx.recall("smart_mode")
    if mode == "existing":
        ctx.log.info(f"cluster {ctx.cluster_name} was supplied, not created by "
                     "this run - leaving it alone")
        return
    if mode == "claimed":
        delete_claim(ctx)
        return
    if ctx.recall("changeset_tainted"):
        ctx.log.warn(
            f"cluster {ctx.cluster_name} is TAINTED (old code ran) - refusing "
            "to freeze it as a template; deleting instead")
        delete_cluster(ctx)
        return
    born = ctx.recall("freeze_fingerprint") or {}
    if born:
        now = _freeze_fingerprint(ctx)
        # Only a value that was ALREADY known can drift. A field that was
        # empty at create and has since populated is status settling, not an
        # upgrade - treating that as drift would refuse to freeze every
        # healthy template whose KommanderCore had not published its version
        # yet.
        drift = {k: f"{born[k]} -> {now.get(k)}"
                 for k in born
                 if born[k] and now.get(k) and now.get(k) != born[k]}
        if drift:
            ctx.log.warn(
                f"cluster {ctx.cluster_name} DRIFTED since it was created "
                f"({drift}): this scenario upgraded it, so it is no longer the "
                f"change-set that hash {ctx.recall('freeze_as')} names. "
                "Refusing to freeze - a template registered here would make "
                "the next run claim an already-upgraded cluster whose upgrade "
                "no-ops, i.e. a green run that tested nothing. Freeze a "
                "baseline with a scenario that does not upgrade it.")
            delete_cluster(ctx)
            return
    if mode == "created-incomplete":
        ctx.log.warn(
            f"cluster {ctx.cluster_name} KEPT: smart_cluster failed after the "
            "create - finish the delivery by hand or sweep it when done")
        return
    h = ctx.recall("freeze_as")
    if not h or ctx.config.dry_run:
        delete_cluster(ctx)
        return
    base = os.environ.get("E2E_BASE", "v2.18.0")
    topo = ctx.recall("freeze_topology", "")
    tmpl_name = f"nkp-tmpl-{base}-{h}" + (f"-{topo}" if topo else "")
    ic_dir = _instant_cluster_dir()
    env = {**os.environ,
           "NKP_PC_URL": ctx.config.pc_url,
           "NKP_NUTANIX_USER": ctx.config.pc_user,
           "NKP_NUTANIX_PASSWORD": ctx.config.pc_password}
    ctx.log.info(f"freezing {ctx.cluster_name} as change-set template {tmpl_name}")
    with (ctx.artifacts / f"freeze-{h}.log").open("w") as sink:
        rc = subprocess.run(
            ["python3", str(ic_dir / "freeze.py"), str(ctx.kubeconfig), ctx.cluster_name],
            cwd=os.environ.get("SPEEDSTART_DIR", str(ic_dir)),
            env=env, stdout=sink, stderr=subprocess.STDOUT, timeout=1800,
        ).returncode
    if rc != 0:
        ctx.log.warn(f"freeze failed (rc={rc}) - see freeze-{h}.log")
        if not keep_on_freeze_failure:
            delete_cluster(ctx)
        else:
            ctx.log.warn(f"cluster {ctx.cluster_name} KEPT for inspection")
        return
    reg = _template_registry()
    vip = os.environ.get("_SMART_VIP") or ""
    # the created cluster's VIP is the template's at-rest identity
    try:
        import yaml as _y
        kc = _y.safe_load(open(ctx.kubeconfig))
        server = kc["clusters"][0]["cluster"]["server"]
        vip = server.split("//")[1].split(":")[0]
    except Exception:
        pass
    # the created cluster's LB was allocated (and cached) at create time; a
    # future claim of this template needs it for the byte-for-byte rewrite
    lb = ctx.addresses()[1].split("-")[0]
    reg[h] = {"name": ctx.cluster_name, "vip": vip, "lb": lb, "base": base,
              "topology": topo, "template_alias": tmpl_name}
    json.dump(reg, open(_template_registry_path(), "w"), indent=1)
    ctx.log.info(f"registered change-set template: {h} -> {ctx.cluster_name} (vip {vip})")
    ctx.remember("frozen_template", tmpl_name)


# --------------------------------------------------- workload cluster (day-2)
@step("create_workload_cluster")
def create_workload_cluster(ctx, *, control_plane: int = 1, workers: int = 1,
                            name_suffix: str = "wl", timeout: str = "45m",
                            control_plane_vcpus: int = 0, control_plane_memory: int = 0,
                            wait_attached: bool = True, attach_timeout: str = "20m",
                            namespace: str = "kommander-default-workspace") -> None:
    """Create a workload cluster FROM the management cluster - the day-2 op.

    Attachment is deliberately a scenario action, never part of a frozen
    template: templates stay minimal, and attaching is exactly the behavior a
    developer wants to watch happen. The workload cluster gets its own VIP/LB
    (the management cluster's pair stays untouched).
    """
    from .netpool import allocate

    mgmt_kc = ctx.kubeconfig
    if mgmt_kc is None:
        raise RuntimeError("create_workload_cluster needs a management cluster first")
    ctx.remember("mgmt_kubeconfig", str(mgmt_kc))
    ctx.remember("mgmt_cluster_name", ctx.cluster_name)

    wl_name = f"{ctx.cluster_name}-{name_suffix}"[:40] if ctx.cluster_name else ctx.cluster(name_suffix)
    vip, lb = allocate(ctx.config.vip_pool, 2, ctx.log, dry_run=ctx.config.dry_run,
                       owner=wl_name)
    ctx.log.info(f"creating workload cluster {wl_name} from {ctx.cluster_name} (vip {vip})")
    ctx.pc.sweep(wl_name)
    ctx.remember("workload_namespace", namespace)

    # Inherit kubernetes version + node image FROM the management cluster:
    # the CLI's preflight requires the image name to match the k8s version,
    # and the one pairing guaranteed to exist on this PC is whatever the
    # management cluster itself was built from. Nothing for the developer
    # to configure.
    wl_k8s, wl_image = None, None
    try:
        nodes = ctx.kube.nodes()
        if nodes:
            wl_k8s = nodes[0]["status"]["nodeInfo"]["kubeletVersion"].lstrip("v")
        nms = ctx.kube.json(["get", "nutanixmachines", "-A"]).get("items", [])
        for nm in nms:
            img = (nm.get("spec", {}).get("image") or {}).get("name")
            if img:
                wl_image = img
                break
        if wl_k8s and wl_image:
            ctx.log.info(f"workload inherits from management: k8s {wl_k8s}, image {wl_image}")
    except Exception as exc:  # noqa: BLE001 - fall back to the configured pair
        ctx.log.warn(f"could not inherit versions from management ({exc}); using config")

    try:
        ctx.nkp.create_cluster(
            wl_name,
            control_plane_replicas=control_plane,
            worker_replicas=workers,
            kubeconfig_out=ctx.artifacts / f"{wl_name}.conf",
            control_plane_ip=vip,
            load_balancer_range=f"{lb}-{lb}",
            self_managed=False,
            management_kubeconfig=mgmt_kc,
            machine_image=wl_image,
            kubernetes_version=wl_k8s,
            control_plane_vcpus=control_plane_vcpus,
            control_plane_memory=control_plane_memory,
            # Clusters born in a workspace namespace are what kommander
            # auto-attaches; "default" would create an unattached orphan.
            extra=["--namespace", namespace],
            timeout_minutes=max(1, parse_duration(timeout) // 60),
        )
    except RuntimeError as exc:
        # The CLI provisions the cluster but does not write a kubeconfig file
        # for managed (non-self-managed) creates. The CAPI secret on the
        # management cluster is the source of truth - and NKP places each
        # managed cluster in its OWN generated namespace (<name>-<rand>)
        # under the workspace, so discover it rather than assume.
        if "kubeconfig" not in str(exc).lower():
            raise
        ctx.log.warn(f"CLI did not write the kubeconfig ({exc}); reading the CAPI secret")
        import base64

        def fetch() -> bool:
            for c in ctx.kube.json(["get", "clusters.cluster.x-k8s.io", "-A"]).get("items", []):
                if c["metadata"]["name"] == wl_name:
                    ns = c["metadata"]["namespace"]
                    ctx.remember("workload_capi_namespace", ns)
                    sec = ctx.kube.json(["get", "secret", f"{wl_name}-kubeconfig", "-n", ns])
                    if sec.get("data", {}).get("value"):
                        (ctx.artifacts / f"{wl_name}.conf").write_bytes(
                            base64.b64decode(sec["data"]["value"]))
                        return True
            return False

        wait_for(fetch, ctx.log, what=f"kubeconfig secret for {wl_name}",
                 timeout_s=600, dry_run=False)
    ctx.remember("workload_name", wl_name)
    if not ctx.config.dry_run:
        # v2.18 gives attached clusters their own generated workspace
        # namespace regardless of -n; discover where the cluster REALLY is
        found = [i for i in ctx.kube.json(["get", "clusters", "-A"]).get("items", [])
                 if i["metadata"]["name"] == wl_name]
        if found:
            namespace = found[0]["metadata"]["namespace"]
            ctx.remember("workload_namespace", namespace)
            ctx.log.info(f"workload cluster lives in namespace {namespace}")
    wl_kc = ctx.artifacts / f"{wl_name}.conf"
    if workers == 0 and not ctx.config.dry_run:
        # A single-node cluster must carry workloads on its control plane, or
        # nothing - including the attachment machinery - can ever schedule.
        from .shell import run as _run
        for attempt in range(1, 6):
            r = _run(["kubectl", "--kubeconfig", str(wl_kc), "taint", "nodes",
                      "--all", "node-role.kubernetes.io/control-plane-"],
                     ctx.log, dry_run=False, check=False, quiet=True, timeout=60)
            if r.ok or "not found" in (r.stdout or ""):
                ctx.log.info("single-node workload: control-plane taint removed")
                break
            time.sleep(15)
        else:
            ctx.log.warn("could not remove control-plane taint - attachment may stall")
    _announce_kubeconfig(ctx, wl_kc, "workload")
    # Creating a workload cluster FROM a management cluster is a day-2 attach:
    # a cluster that exists but never joined is a failed attach, not a passed
    # create with a follow-up step the scenario might forget. Waiting is part
    # of this verb's contract, the same way create_cluster settles its nodes.
    if wait_attached:
        wait_cluster_attached(ctx, name=wl_name, timeout=attach_timeout)


@step("wait_cluster_attached")
def wait_cluster_attached(ctx, *, name: str = "", timeout: str = "20m") -> None:
    """Wait until the management cluster reports the workload cluster joined."""
    target = name or ctx.recall("workload_name")
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would wait for KommanderCluster {target} joined")
        return

    def joined() -> bool:
        items = ctx.kube.json(["get", "kommandercluster", "-A"]).get("items", [])
        for i in items:
            if target in i["metadata"]["name"]:
                st = i.get("status", {})
                conds = {c.get("type"): c.get("status")
                         for c in st.get("conditions", [])}
                return (st.get("phase") == "Joined"
                        or conds.get("KubefedClusterJoined") == "True"
                        or conds.get("Joined") == "True"
                        or conds.get("Ready") == "True")
        return False

    wait_for(joined, ctx.log, what=f"KommanderCluster {target} joined",
             timeout_s=parse_duration(timeout), dry_run=False)

    # "Joined" is necessary but NOT sufficient: live 2026-08-30 it flipped in
    # THREE SECONDS, long before the attach payload (cert-manager, gatekeeper,
    # flux, traefik...) existed. Returning there hands the next step a cluster
    # that cannot yet run anything, and the real wait resurfaces as a
    # confusing timeout somewhere downstream. The payload lands as
    # AppDeploymentInstances in the cluster's workspace namespace, so wait for
    # the first one to appear and settle.
    ns = ctx.recall("workload_namespace") or ""
    if not ns:
        ctx.log.warn("no workload namespace recorded - cannot confirm the "
                     "attach payload; proceeding on 'Joined' alone")
        return

    def payload_landed() -> bool:
        items = ctx.kube.json(
            ["get", "appdeploymentinstance", "-n", ns]).get("items", [])
        return len(items) > 0

    wait_for(payload_landed, ctx.log,
             what=f"attach payload to appear in {ns}",
             timeout_s=parse_duration(timeout), dry_run=False)
    n = len(ctx.kube.json(["get", "appdeploymentinstance", "-n", ns]).get("items", []))
    ctx.log.info(f"workload cluster {target} is attached "
                 f"({n} platform app instance(s) federated)")


@step("switch_cluster")
def switch_cluster(ctx, *, to: str) -> None:
    """Point the following steps at 'workload' or 'management'."""
    if to == "management":
        path = ctx.recall("mgmt_kubeconfig")
    elif to == "workload":
        wl = ctx.recall("workload_name")
        path = str(ctx.artifacts / f"{wl}.conf") if wl else None
    else:
        raise RuntimeError(f"switch_cluster: to must be workload or management, got {to!r}")
    if not path:
        # In cleanup this step runs even when the scenario died before any
        # cluster existed; a hard failure here would abort the remaining
        # cleanup steps, which must stay best-effort.
        if ctx.kubeconfig is not None:
            ctx.log.warn(f"switch_cluster: no {to} kubeconfig recorded; keeping current")
            return
        ctx.log.warn(f"switch_cluster: nothing to switch to for {to!r}; skipping")
        return
    if ctx.config.dry_run:
        ctx.log.info(f"[dry-run] would switch to the {to} cluster")
        return
    ctx.switch_kubeconfig(path)
    ctx.log.info(f"now operating on the {to} cluster ({path})")


@step("delete_workload_cluster")
def delete_workload_cluster(ctx, *, timeout: str = "30m") -> None:
    """Tear the workload cluster down via the management cluster."""
    wl = ctx.recall("workload_name")
    mgmt = ctx.recall("mgmt_kubeconfig")
    if not wl or ctx.config.dry_run:
        return
    import subprocess
    ctx.log.info(f"deleting workload cluster {wl}")
    with (ctx.artifacts / f"delete-{wl}.log").open("w") as sink:
        subprocess.run(
            [str(ctx.config.nkp_bin), "delete", "cluster", "--cluster-name", wl,
             "--namespace", ctx.recall("workload_namespace", "kommander-default-workspace"),
             "--kubeconfig", mgmt],
            stdout=sink, stderr=subprocess.STDOUT,
            timeout=parse_duration(timeout),
            env={**os.environ, "NUTANIX_USER": ctx.config.pc_user,
                 "NUTANIX_PASSWORD": ctx.config.pc_password},
        )
    swept = ctx.pc.sweep(wl)
    if swept:
        ctx.log.warn(f"swept {swept} leftover VM(s) of {wl}")
