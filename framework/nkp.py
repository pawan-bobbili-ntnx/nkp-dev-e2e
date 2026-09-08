# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around the ``nkp`` CLI.

Only the flags the scenarios need are modelled. Anything exotic can be passed
through ``extra`` rather than growing this surface.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .logging_ import Log
from .shell import Result, run


class Nkp:
    def __init__(self, config: Config, log: Log):
        self.config = config
        self.log = log

    # ------------------------------------------------------------------ core
    def _run(self, args: list[str], *, timeout: int = 3600, check: bool = True,
             log_file: Path | None = None, cwd: Path | None = None,
             quiet: bool = False) -> Result:
        return run(
            [str(self.config.nkp_bin), *args],
            self.log,
            dry_run=self.config.dry_run,
            check=check,
            timeout=timeout,
            log_file=log_file,
            cwd=cwd,
            quiet=quiet,
            env={
                "NUTANIX_USER": self.config.pc_user,
                "NUTANIX_PASSWORD": self.config.pc_password,
            },
        )

    def with_binary(self, path: str) -> "Nkp":
        """A second CLI (an older release) sharing this config - used by the
        upgrade scenarios to install a baseline before upgrading."""
        import copy

        cfg = copy.copy(self.config)
        cfg.nkp_bin = Path(path).expanduser()
        return Nkp(cfg, self.log)

    def version(self) -> str:
        return self._run(["version"], timeout=120, check=False).stdout.strip()

    # -------------------------------------------------------------- clusters
    def create_cluster(
        self,
        name: str,
        *,
        control_plane_replicas: int = 1,
        worker_replicas: int = 1,
        kubeconfig_out: Path,
        control_plane_ip: str,
        load_balancer_range: str,
        machine_image: str | None = None,
        kubernetes_version: str | None = None,
        worker_vcpus: int = 0,
        worker_memory: int = 0,
        control_plane_vcpus: int = 0,
        control_plane_memory: int = 0,
        self_managed: bool = True,
        management_kubeconfig: Path | None = None,
        extra: list[str] | None = None,
        timeout_minutes: int | None = None,
    ) -> None:
        """`nkp create cluster nutanix`.

        Flag names verified against nkp v2.18: subnets and the Prism Element
        cluster are set per machine role, the storage container flag is
        --csi-storage-container, and the kubeconfig is written to
        <cluster-name>.conf in the working directory rather than to a flag.
        """
        minutes = timeout_minutes or self.config.timeout_minutes
        args = [
            "create", "cluster", "nutanix",
            "--cluster-name", name,
            "--control-plane-replicas", str(control_plane_replicas),
            "--worker-replicas", str(worker_replicas),
            "--endpoint", self.config.pc_url,
            "--insecure",
            "--control-plane-subnets", self.config.subnet_name,
            "--worker-subnets", self.config.subnet_name,
            "--control-plane-prism-element-cluster", self.config.pe_cluster_name,
            "--worker-prism-element-cluster", self.config.pe_cluster_name,
            "--csi-storage-container", self.config.storage_container,
            "--control-plane-endpoint-ip", control_plane_ip,
            "--kubernetes-service-load-balancer-ip-range", load_balancer_range,
            "--timeout", f"{minutes}m",
        ]
        # A baseline CLI must create with ITS supported kubernetes, not ours;
        # empty string means "let that CLI use its own default".
        k8s = self.config.k8s_version if kubernetes_version is None else kubernetes_version
        if k8s:
            args += ["--kubernetes-version", k8s.lstrip("v")]
        if self_managed:
            # Without a management cluster to pivot from, the cluster has to
            # manage itself; otherwise the CLI looks for one and fails.
            args.append("--self-managed")
        elif management_kubeconfig is not None:
            # A workload cluster is created BY the management cluster - without
            # this the CLI targets whatever kubeconfig context the shell has,
            # which is never what the scenario means.
            args += ["--kubeconfig", str(management_kubeconfig)]
        # The CLI requires an image and rejects the bare --vm-image group in
        # favour of per-role flags:
        #   "at least one of the flags in the group
        #    [vm-image control-plane-vm-image worker-vm-image] is required"
        image = self.config.machine_image if machine_image is None else machine_image
        if image:
            args += [
                "--control-plane-vm-image", image,
                "--worker-vm-image", image,
            ]
        cp_mem = control_plane_memory or self.config.control_plane_memory
        if cp_mem:
            args += ["--control-plane-memory", str(cp_mem)]
        if control_plane_vcpus:
            args += ["--control-plane-vcpus", str(control_plane_vcpus)]
        if worker_vcpus:
            args += ["--worker-vcpus", str(worker_vcpus)]
        if worker_memory:
            args += ["--worker-memory", str(worker_memory)]
        if self.config.registry_mirror_url:
            # A mirror without credentials wedges CAREN part-way through
            # topology reconciliation, so the three travel together.
            args += ["--registry-mirror-url", self.config.registry_mirror_url]
            if self.config.registry_mirror_username:
                args += [
                    "--registry-mirror-username", self.config.registry_mirror_username,
                    "--registry-mirror-password", self.config.registry_mirror_password,
                ]
        if self.config.ssh_public_key:
            # Omitting this creates a cluster with no way in, which makes any
            # later diagnosis impossible.
            args += ["--ssh-public-key-file", str(self.config.ssh_public_key)]
        if self.config.ssh_username:
            args += ["--ssh-username", self.config.ssh_username]
        args += extra or []

        self.log.info(f"creating cluster {name} ({control_plane_replicas}cp/{worker_replicas}w)")
        # The CLI writes <name>.conf into the working directory; run from the
        # artifacts directory so it lands with the rest of the run's output.
        kubeconfig_out.parent.mkdir(parents=True, exist_ok=True)
        self._run(args, timeout=(minutes + 15) * 60, cwd=kubeconfig_out.parent)

        produced = kubeconfig_out.parent / f"{name}.conf"
        if produced.exists() and produced != kubeconfig_out:
            produced.replace(kubeconfig_out)
        elif not kubeconfig_out.exists() and not self.config.dry_run:
            raise RuntimeError(
                f"cluster created but no kubeconfig at {produced} - "
                "check the create output"
            )

    def delete_cluster(self, name: str, *, timeout_minutes: int = 30) -> None:
        self.log.info(f"deleting cluster {name}")
        self._run(
            [
                "delete", "cluster",
                "--cluster-name", name,
                "--self-managed",
            ],
            timeout=timeout_minutes * 60,
            check=False,  # cleanup is best effort; the PC sweep is the backstop
        )

    def diagnose(self, kubeconfig: Path, out_dir: Path) -> None:
        """Support bundle, if this build of the CLI has the subcommand."""
        out_dir.mkdir(parents=True, exist_ok=True)
        # The CLI writes support-bundle-<ts>.tar.gz into its working directory,
        # so run it FROM the artifacts dir - a bundle that lands next to the
        # repo instead of the failure it belongs to helps nobody (hit live
        # 2026-08-27: a 15 MB bundle stranded in hack/dev-e2e/).
        self._run(
            ["diagnose", "--kubeconfig", str(kubeconfig)],
            timeout=900,
            check=False,
            log_file=out_dir / "nkp-diagnose.log",
            cwd=out_dir,
        )

    # ------------------------------------------------------- platform / apps
    # Used by the kommander scenarios. Kept here so a scenario never shells out
    # directly and the dry-run contract holds everywhere.
    def installer_config(self, out: Path, *, disable: list[str] | None = None) -> Path:
        """Write an installation config, optionally with some apps switched off.

        The default set is whatever the CLI itself generates, so this tracks the
        release rather than a copy that silently goes stale.
        """
        out.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(["install", "kommander", "--init", "-o", "yaml"],
                           timeout=300, quiet=True)
        if self.config.dry_run:
            self.log.info(f"[dry-run] would write an installer config to {out}")
            return out

        import yaml as _yaml

        doc = _yaml.safe_load(result.stdout) or {}
        apps = doc.get("apps") or {}
        # "name?" means disable-if-present: app catalogs vary by version
        # (v2.17 ships grafana-loki, v2.18 grafana-loki-v3 - found live
        # 2026-08-27), so cross-version scenarios mark those entries optional.
        # Bare names keep the fail-loud check that catches typos.
        required = [a for a in (disable or []) if not a.endswith("?")]
        optional = [a[:-1] for a in (disable or []) if a.endswith("?")]
        unknown = [a for a in required if a not in apps]
        if unknown:
            raise RuntimeError(
                f"cannot disable unknown app(s): {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(apps))}"
            )
        skipped = [a for a in optional if a not in apps]
        if skipped:
            self.log.info(f"optional disable(s) not in this catalog, skipped: {', '.join(skipped)}")
        for app in required + [a for a in optional if a in apps]:
            apps[app]["enabled"] = False
        doc["apps"] = apps
        out.write_text(_yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
        off = [a for a, v in sorted(apps.items()) if not v.get("enabled")]
        self.log.info(f"installer config: {len(apps) - len(off)} apps on, {len(off)} off")
        return out

    def install_kommander(self, kubeconfig: Path, *, installer_config: Path | None = None,
                          timeout_minutes: int = 45) -> None:
        """`nkp install kommander`.

        Verified against nkp v2.18: there is no chart-version flag here - the
        version installed is the one the CLI ships. To install an older
        platform, use an older CLI (NKP_BIN) or an --installer-config that
        pins it.

        REGISTRY CREDENTIALS (added 2026-08-30). `--registry-url/-username/
        -password` exist but are MarkHidden, so `--help` does not list them -
        do not conclude from `--help` that they are absent, as I did. They
        drive the installer's OCICredentials step, which writes the
        `flux-oci-repository-secret` pull secret into the kommander namespace
        (kommander-cli/pkg/installer/oci.go:19,44-67) - the SUPPORTED way to
        authenticate flux's chart pulls. That matters because the kommander
        and kommander-appmanagement charts come from docker.io on both the
        2.17 and 2.18 lines, so a shared egress IP hits Docker Hub's
        anonymous limit and the install dies on those two charts alone.
        Hand-patching each OCIRepository's secretRef does NOT hold: the
        operator (manager=KommanderCoreInstaller) re-applies the object and
        strips it within ~10s.
        """
        args = ["install", "kommander", "--kubeconfig", str(kubeconfig)]
        if installer_config:
            args += ["--installer-config", str(installer_config)]
        if self.config.registry_mirror_url and self.config.registry_mirror_username:
            args += [
                "--registry-url", self.config.registry_mirror_url,
                "--registry-username", self.config.registry_mirror_username,
                "--registry-password", self.config.registry_mirror_password,
            ]
            self.log.info(
                f"authenticating OCI chart pulls as "
                f"{self.config.registry_mirror_username} via {self.config.registry_mirror_url}")
        self.log.info("installing the platform")
        self._run(args, timeout=timeout_minutes * 60)

    def upgrade_platform(self, kubeconfig: Path, *, applications_repository: str = "",
                         timeout_minutes: int = 60) -> None:
        """`nkp upgrade kommander`. The target is selected by the CLI version;
        --kommander-applications-repository picks the app definitions."""
        args = ["upgrade", "kommander", "--kubeconfig", str(kubeconfig)]
        if applications_repository:
            args += ["--kommander-applications-repository", applications_repository]
        self.log.info("upgrading the platform")
        self._run(args, timeout=timeout_minutes * 60)

    def upgrade_cluster_nodes(self, kubeconfig: Path, cluster_name: str, *,
                              namespace: str = "kommander", vm_image: str = "",
                              skip_preflight: list | None = None,
                              timeout_minutes: int = 60) -> None:
        """`nkp upgrade cluster nutanix` - the kubernetes/node-roll upgrade.

        On a MANAGEMENT cluster this also upgrades the CAPI stack and
        re-applies the ClusterClass (verified: cluster/upgrade.go:226,416-463
        runs the same code as `upgrade capi-components`, gated on
        IsManagementCluster). On a managed cluster it rolls nodes only.
        """
        args = ["upgrade", "cluster", "nutanix",
                "--cluster-name", cluster_name,
                "--namespace", namespace,
                "--kubeconfig", str(kubeconfig),
                "--timeout", f"{timeout_minutes}m"]
        if vm_image:
            args += ["--vm-image", vm_image]
        if skip_preflight:
            args += ["--skip-preflight-checks", ",".join(skip_preflight)]
        self.log.info(f"upgrading cluster {cluster_name} (nodes + k8s)")
        self._run(args, timeout=(timeout_minutes + 20) * 60)

    def upgrade_catalog_app(self, kubeconfig: Path, app: str, *, to_version: str,
                            workspace: str = "") -> None:
        """`nkp upgrade catalogapp NAME --to-version X`.

        Note there is no generic `upgrade app`: platform applications move with
        `upgrade kommander` / `upgrade workspace`, and catalog applications are
        upgraded individually here.
        """
        args = [
            "upgrade", "catalogapp", app,
            "--to-version", to_version,
            "--kubeconfig", str(kubeconfig),
        ]
        if workspace:
            args += ["--workspace", workspace]
        self._run(args, timeout=45 * 60)

    def upgrade_workspace(self, kubeconfig: Path, workspace: str,
                          *, timeout_minutes: int = 60) -> None:
        """`nkp upgrade workspace` - moves platform apps in a workspace up to
        the management cluster's versions."""
        self._run(
            ["upgrade", "workspace", workspace, "--kubeconfig", str(kubeconfig)],
            timeout=timeout_minutes * 60,
        )

    def _kubectl_json(self, kubeconfig: Path, args: list[str]) -> dict:
        import json as _json

        res = run(
            ["kubectl", "--kubeconfig", str(kubeconfig), *args, "-o", "json"],
            self.log,
            dry_run=self.config.dry_run,
            check=False,
            quiet=True,
        )
        try:
            return _json.loads(res.stdout) if res.stdout.strip() else {}
        except ValueError:
            return {}

    def platform_version(self, kubeconfig: Path) -> str:
        """Installed platform version, read from the KommanderCore resource."""
        if self.config.dry_run:
            return "v0.0.0-dry-run"
        data = self._kubectl_json(kubeconfig, ["get", "kommandercore", "-A"])
        for item in data.get("items", []):
            status = item.get("status", {})
            return status.get("version") or item.get("spec", {}).get("version", "")
        return ""

    def app_version(self, kubeconfig: Path, app: str) -> str:
        """Version of a single AppDeployment."""
        if self.config.dry_run:
            return "v0.0.0-dry-run"
        data = self._kubectl_json(kubeconfig, ["get", "appdeployment", "-A"])
        for item in data.get("items", []):
            if item.get("metadata", {}).get("name") == app:
                spec = item.get("spec", {})
                ref = spec.get("appRef") or {}
                return ref.get("name", "") or spec.get("version", "")
        return ""

    def unready_helmreleases(self, kubeconfig: Path) -> list[str]:
        """HelmReleases whose Ready condition is not True."""
        if self.config.dry_run:
            return []
        data = self._kubectl_json(kubeconfig, ["get", "helmreleases", "-A"])
        bad = []
        for item in data.get("items", []):
            conds = {c.get("type"): c.get("status") for c in item.get("status", {}).get("conditions", [])}
            if conds.get("Ready") != "True":
                meta = item.get("metadata", {})
                bad.append(f"{meta.get('namespace')}/{meta.get('name')}")
        return bad
