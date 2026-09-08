# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Diagnostics collected when a scenario fails.

Cheap, always-safe commands only: this runs against a cluster that is by
definition misbehaving, so every step tolerates failure.
"""

from __future__ import annotations

from pathlib import Path


def collect_cluster(ctx, *, label: str = "failure") -> None:
    """Dump the usual suspects into <artifacts>/diagnostics-<label>/."""
    out = ctx.artifacts / f"diagnostics-{label}"
    out.mkdir(parents=True, exist_ok=True)
    log = ctx.log

    if ctx.kubeconfig is None or not Path(ctx.kubeconfig).exists():
        log.warn("no kubeconfig - collecting Prism Central state only")
    else:
        kube = ctx.kube
        dumps = {
            "nodes.txt": ["get", "nodes", "-o", "wide"],
            "pods-all.txt": ["get", "pods", "-A", "-o", "wide"],
            "events.txt": ["get", "events", "-A", "--sort-by=.lastTimestamp"],
            "nodes-describe.txt": ["describe", "nodes"],
            "cluster-api.txt": ["get", "cluster,machine,machinedeployment", "-A", "-o", "wide"],
            "helmreleases.txt": ["get", "helmreleases", "-A"],
        }
        for filename, args in dumps.items():
            try:
                res = kube._run(args, check=False, quiet=True)  # noqa: SLF001
                (out / filename).write_text(res.stdout or "", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log.debug(f"diagnostic {filename} failed: {exc}")

        # Logs of anything not Running - usually the whole story.
        try:
            for entry in kube.unhealthy_pods()[:15]:
                ns_name = entry.split(" ")[0]
                ns, _, pod = ns_name.partition("/")
                res = kube._run(  # noqa: SLF001
                    ["logs", "-n", ns, pod, "--all-containers", "--tail=200", "--previous"],
                    check=False,
                    quiet=True,
                )
                if not (res.stdout or "").strip():
                    res = kube._run(  # noqa: SLF001
                        ["logs", "-n", ns, pod, "--all-containers", "--tail=200"],
                        check=False,
                        quiet=True,
                    )
                (out / f"logs-{ns}-{pod}.txt").write_text(res.stdout or "", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug(f"pod log collection failed: {exc}")

    # What the PC thinks exists, which is how orphans get spotted.
    try:
        vms = ctx.pc.vms_named(ctx.config.cluster_prefix)
        lines = [
            f"{v.get('status', {}).get('name')}\t{v.get('metadata', {}).get('uuid')}\t"
            f"{v.get('status', {}).get('resources', {}).get('power_state')}"
            for v in vms
        ]
        (out / "prism-central-vms.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"PC VM listing failed: {exc}")

    log.info(f"diagnostics written to {out}")
