# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""kubectl helpers used by the assertions and day-2 operations."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .logging_ import Log
from .shell import Result, run, wait_for


class Kube:
    def __init__(self, kubeconfig: Path, log: Log, config: Config):
        self.kubeconfig = kubeconfig
        self.log = log
        self.config = config

    def _run(self, args: list[str], *, check: bool = True, timeout: int = 300,
             quiet: bool = False) -> Result:
        return run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), *args],
            self.log,
            dry_run=self.config.dry_run,
            check=check,
            timeout=timeout,
            quiet=quiet,
        )

    def json(self, args: list[str]) -> dict:
        """`kubectl ... -o json`, parsed. Never raises on a transient blip.

        Empty output has always meant "nothing there". Non-empty output that
        is not JSON did NOT: it raised a bare
        `Expecting value: line 1 column 1 (char 0)`, which tells the reader
        nothing about which command failed or why. That is not hypothetical -
        it killed a node-upgrade run at the post-roll identity check
        (2026-08-30), the one moment the API is guaranteed to be unstable
        because the control-plane node has just been replaced.

        One retry, because the failure is a moving apiserver rather than a
        malformed request; then give up quietly and let the caller's own
        wait/assert decide, with the offending output logged so it is
        diagnosable.
        """
        for attempt in (1, 2):
            out = self._run([*args, "-o", "json"], quiet=True).stdout
            if not out.strip():
                return {}
            try:
                return json.loads(out)
            except ValueError:
                pass
            # kubectl prefixes the document with warnings - deterministically,
            # not as a blip. Live 2026-08-30, right after a node roll:
            #   Warning: cluster.x-k8s.io/v1beta1 Machine is deprecated; ...
            #   {"apiVersion": "v1", ...}
            # The roll moved the cluster to a kubernetes where that API is
            # deprecated, so EVERY `get machines` returns it. Parse from the
            # first document character instead of guessing at transience.
            start = min((i for i in (out.find("{"), out.find("[")) if i >= 0),
                        default=-1)
            if start >= 0:
                try:
                    parsed = json.loads(out[start:])
                    dropped = out[:start].strip().splitlines()
                    if dropped:
                        self.log.warn(
                            f"kubectl {' '.join(args[:3])}: ignored "
                            f"{len(dropped)} warning line(s) before the JSON "
                            f"({dropped[0][:90]!r})")
                    return parsed
                except ValueError:
                    pass
            if attempt == 1:
                time.sleep(3)
                continue
            self.log.warn(
                f"kubectl {' '.join(args[:3])} returned non-JSON "
                f"({out.strip()[:120]!r}) - treating as empty")
        return {}

    # ----------------------------------------------------------------- nodes
    def nodes(self) -> list[dict]:
        return self.json(["get", "nodes"]).get("items", [])

    def node_names(self, *, role: str | None = None) -> list[str]:
        out = []
        for node in self.nodes():
            labels = node.get("metadata", {}).get("labels", {})
            if role == "control-plane" and "node-role.kubernetes.io/control-plane" not in labels:
                continue
            if role == "worker" and "node-role.kubernetes.io/control-plane" in labels:
                continue
            out.append(node["metadata"]["name"])
        return out

    def node_ready(self, name: str) -> bool:
        node = self.json(["get", "node", name])
        for cond in node.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready":
                return cond.get("status") == "True"
        return False

    def all_nodes_ready(self, expected: int | None = None) -> bool:
        nodes = self.nodes()
        if expected is not None and len(nodes) != expected:
            return False
        if not nodes:
            return False
        for node in nodes:
            conds = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
            if conds.get("Ready") != "True":
                return False
        return True

    def wait_nodes_ready(self, expected: int, *, timeout_s: int = 1800) -> None:
        wait_for(
            lambda: self.all_nodes_ready(expected),
            self.log,
            what=f"{expected} node(s) Ready",
            timeout_s=timeout_s,
            dry_run=self.config.dry_run,
        )

    def cordon(self, node: str) -> None:
        self._run(["cordon", node])

    def uncordon(self, node: str) -> None:
        self._run(["uncordon", node])

    def drain(self, node: str, *, timeout_s: int = 600) -> None:
        self._run(
            [
                "drain", node,
                "--ignore-daemonsets",
                "--delete-emptydir-data",
                "--force",
                f"--timeout={timeout_s}s",
            ],
            timeout=timeout_s + 120,
        )

    def is_cordoned(self, node: str) -> bool:
        return bool(self.json(["get", "node", node]).get("spec", {}).get("unschedulable"))

    # ------------------------------------------------------------------ pods
    def unhealthy_pods(self, namespace: str | None = None) -> list[str]:
        args = ["get", "pods", "-A"] if namespace is None else ["get", "pods", "-n", namespace]
        bad = []
        for pod in self.json(args).get("items", []):
            meta, status = pod["metadata"], pod.get("status", {})
            phase = status.get("phase")
            if phase in ("Succeeded",):
                continue
            ready = all(
                cs.get("ready") for cs in status.get("containerStatuses", [])
            ) if status.get("containerStatuses") else False
            if phase != "Running" or not ready:
                bad.append(f"{meta.get('namespace')}/{meta['name']} ({phase})")
        return bad

    def wait_pods_healthy(self, *, namespace: str | None = None, timeout_s: int = 1800,
                          tolerate: int = 0) -> None:
        wait_for(
            lambda: len(self.unhealthy_pods(namespace)) <= tolerate,
            self.log,
            what=f"pods healthy in {namespace or 'all namespaces'}",
            timeout_s=timeout_s,
            dry_run=self.config.dry_run,
        )

    # -------------------------------------------------------------- workloads
    def scale(self, kind_name: str, replicas: int, *, namespace: str) -> None:
        self._run(["scale", kind_name, f"--replicas={replicas}", "-n", namespace])

    def rollout_restart(self, kind_name: str, *, namespace: str) -> None:
        self._run(["rollout", "restart", kind_name, "-n", namespace])

    def rollout_status(self, kind_name: str, *, namespace: str, timeout_s: int = 600) -> None:
        self._run(
            ["rollout", "status", kind_name, "-n", namespace, f"--timeout={timeout_s}s"],
            timeout=timeout_s + 60,
        )

    # -------------------------------------------------------- flux / apps
    def apply(self, manifest: dict) -> None:
        """Apply a manifest given as a dict."""
        body = json.dumps(manifest)
        if self.config.dry_run:
            meta = manifest.get("metadata", {})
            self.log.info(
                f"[dry-run] would apply {manifest.get('kind')} "
                f"{meta.get('namespace')}/{meta.get('name')}"
            )
            return
        run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), "apply", "-f", "-"],
            self.log,
            dry_run=False,
            check=True,
            timeout=120,
            stdin=body,
        )

    def delete(self, kind: str, name: str, *, namespace: str,
               ignore_missing: bool = True) -> None:
        """Delete a resource. Missing is fine, so cleanup stays idempotent."""
        args = ["delete", kind, name, "-n", namespace, "--wait=false"]
        if ignore_missing:
            args.append("--ignore-not-found")
        self._run(args, check=not ignore_missing)

    def patch_json(self, kind: str, name: str, patch: list, *, namespace: str) -> None:
        """Apply a JSON patch to one object."""
        self._run([
            "patch", kind, name, "-n", namespace,
            "--type=json", "-p", json.dumps(patch),
        ])

    def conditions(self, kind: str, name: str, *, namespace: str) -> dict[str, dict]:
        """Conditions of one object, keyed by type. Empty if it does not exist."""
        obj = self.json(["get", kind, name, "-n", namespace])
        if not obj:
            return {}
        return {
            c.get("type"): c
            for c in obj.get("status", {}).get("conditions", []) or []
        }

    def helmreleases(self, *, namespace: str | None = None,
                     selector: str | None = None) -> list[dict]:
        args = ["get", "helmreleases"]
        args += ["-n", namespace] if namespace else ["-A"]
        if selector:
            args += ["-l", selector]
        return self.json(args).get("items", [])

    @staticmethod
    def helmrelease_problem(item: dict) -> str | None:
        """Why this HelmRelease is not up, or None if it is.

        A HelmRelease is only actually serving a chart when it is both Ready and
        Released; Ready alone goes True on a release that was never installed.
        The condition message is carried through because that is the line that
        says what broke.
        """
        meta = item.get("metadata", {})
        where = f"{meta.get('namespace')}/{meta.get('name')}"
        conds = {c.get("type"): c for c in item.get("status", {}).get("conditions", []) or []}
        if not conds:
            return f"{where}: no status yet"
        for wanted in ("Ready", "Released"):
            cond = conds.get(wanted)
            if cond is None:
                if wanted == "Released":
                    continue  # only reported once a release has been attempted
                return f"{where}: no {wanted} condition"
            if cond.get("status") != "True":
                reason = cond.get("reason", "")
                message = (cond.get("message") or "").strip().splitlines()
                detail = f" - {message[0]}" if message else ""
                return f"{where}: {wanted}={cond.get('status')} ({reason}){detail}"
        return None

    def unready_helmreleases(self, *, namespace: str | None = None,
                             selector: str | None = None) -> list[str]:
        return [
            problem
            for item in self.helmreleases(namespace=namespace, selector=selector)
            if (problem := self.helmrelease_problem(item))
        ]

    def server_reachable(self) -> bool:
        return self._run(["version", "--request-timeout=10s"], check=False, quiet=True).ok
