# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Configuration, resolved from the environment with dev-PC defaults.

Credentials come from the same NUTANIX_USER / NUTANIX_PASSWORD pair the dev-VM
gate uses, which is what `tam login pc-dev` prints.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _sanitize(value: str, limit: int = 20) -> str:
    """Reduce to an RFC-1123 label fragment usable in cluster and VM names."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return re.sub(r"-+", "-", cleaned)[:limit].strip("-") or "e2e"


#: Settings that are per-DEVELOPER, not per-scenario: which Prism Central,
#: which credentials, which address pool. They live in a file so nobody has to
#: keep a wall of exports in their shell, and so a second developer can be
#: given one file rather than a paragraph of instructions.
#:
#: Search order - first hit wins, and a real environment variable ALWAYS beats
#: the file, so a one-off override is still just `VAR=... ./run_e2e.py`.
ENV_FILES = ("./nkp-e2e.env", "~/.nkp-e2e.env")


def load_env_file(path: str | None = None) -> str | None:
    """Read KEY=VALUE lines into the environment without clobbering real vars."""
    from pathlib import Path as _P

    candidates = [path] if path else [os.environ.get("E2E_ENV_FILE"), *ENV_FILES]
    for cand in candidates:
        if not cand:
            continue
        f = _P(cand).expanduser()
        if not f.is_file():
            continue
        for raw in f.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # a real environment variable always wins over the file
            os.environ.setdefault(key, val)
        return str(f)
    return None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Config:
    # --- Prism Central -------------------------------------------------------
    pc_url: str
    pc_user: str
    pc_password: str = field(repr=False)
    pe_cluster_name: str
    subnet_name: str
    storage_container: str

    # --- cluster inputs ------------------------------------------------------
    nkp_bin: Path
    k8s_version: str
    machine_image: str
    ssh_public_key: Path | None
    vip_pool: str

    # --- run control ---------------------------------------------------------
    cluster_prefix: str
    artifacts_dir: Path
    #: an existing cluster to run against instead of building one (E2E_KUBECONFIG)
    kubeconfig_env: str
    ssh_username: str
    control_plane_memory: str
    registry_mirror_url: str
    registry_mirror_username: str
    registry_mirror_password: str
    dry_run: bool = False
    keep: bool = False
    always_collect: bool = False
    timeout_minutes: int = 60

    @classmethod
    def from_env(
        cls,
        *,
        artifacts_dir: Path | None = None,
        dry_run: bool = False,
        keep: bool = False,
        always_collect: bool = False,
        timeout_minutes: int | None = None,
    ) -> "Config":
        missing = [k for k in ("NUTANIX_USER", "NUTANIX_PASSWORD") if not _env(k)]
        if missing and not dry_run:
            raise ConfigError(
                f"missing {' and '.join(missing)}. Get them with: "
                "cd <tam-cli> && ./tam login pc-dev"
            )

        nkp_bin = Path(_env("NKP_BIN", "./nkp")).expanduser()
        if not dry_run:
            if not nkp_bin.exists():
                found = shutil.which("nkp")
                if not found:
                    raise ConfigError(
                        f"nkp binary not found at {nkp_bin} and not on PATH. "
                        "Build it or set NKP_BIN."
                    )
                nkp_bin = Path(found)
            if not os.access(nkp_bin, os.X_OK):
                raise ConfigError(f"{nkp_bin} is not executable")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        default_artifacts = Path("e2e-results") / stamp

        ssh_key = _env("E2E_SSH_PUBLIC_KEY")
        ssh_path = Path(ssh_key).expanduser() if ssh_key else None
        if ssh_path is None:
            candidate = Path.home() / ".ssh" / "id_ed25519.pub"
            ssh_path = candidate if candidate.exists() else None

        missing = [k for k, v in (("PC_URL", _env("PC_URL", "")),
                                  ("NUTANIX_PRISM_ELEMENT_CLUSTER_NAME", _env("NUTANIX_PRISM_ELEMENT_CLUSTER_NAME", "")),
                                  ("NUTANIX_SUBNET_NAME", _env("NUTANIX_SUBNET_NAME", "")),
                                  ("E2E_VIP_POOL", _env("E2E_VIP_POOL", ""))) if not v]
        if missing and not dry_run:
            raise ConfigError(
                "these describe YOUR Prism Central and carry no default: "
                + ", ".join(missing)
                + ". Copy nkp-e2e.env.example to nkp-e2e.env and fill them in "
                "(the values for the shared dev PC are in the team's internal notes, not in this repository).")
        return cls(
            pc_url=_env("PC_URL", ""),
            pc_user=_env("NUTANIX_USER", "dry-run"),
            pc_password=_env("NUTANIX_PASSWORD", "dry-run"),
            pe_cluster_name=_env("NUTANIX_PRISM_ELEMENT_CLUSTER_NAME", ""),
            subnet_name=_env("NUTANIX_SUBNET_NAME", ""),
            storage_container=_env("NUTANIX_STORAGE_CONTAINER_NAME", "SelfServiceContainer"),
            nkp_bin=nkp_bin.resolve() if nkp_bin.exists() else nkp_bin,
            k8s_version=_env("E2E_KUBERNETES_VERSION", "v1.33.2"),
            kubeconfig_env=_env("E2E_KUBECONFIG", ""),
            ssh_username=_env("E2E_SSH_USERNAME", "konvoy"),
            control_plane_memory=_env("E2E_CONTROL_PLANE_MEMORY", ""),
            registry_mirror_url=_env("E2E_REGISTRY_MIRROR_URL", ""),
            registry_mirror_username=_env("E2E_REGISTRY_MIRROR_USERNAME", ""),
            registry_mirror_password=_env("E2E_REGISTRY_MIRROR_PASSWORD", ""),
            machine_image=_env("E2E_MACHINE_IMAGE", ""),
            # Node IPs come from DHCP, but the control-plane VIP and the service
            # load-balancer range are static and must be unique per cluster.
            vip_pool=_env("E2E_VIP_POOL", ""),
            ssh_public_key=ssh_path,
            # Cluster names become Kubernetes object names and VM names, so keep
            # them to an RFC-1123 label: lowercase alphanumerics and dashes.
            cluster_prefix=_sanitize(
                _env("E2E_CLUSTER_PREFIX", f"e2e-{os.environ.get('USER', 'dev')}")
            ),
            artifacts_dir=(artifacts_dir or default_artifacts).resolve(),
            dry_run=dry_run,
            keep=keep,
            always_collect=always_collect,
            timeout_minutes=timeout_minutes or int(_env("E2E_TIMEOUT_MINUTES", "60")),
        )

    def describe(self) -> list[tuple[str, str]]:
        """Redacted summary for the run header and RESULTS.md."""
        return [
            ("Prism Central", self.pc_url),
            ("PE cluster", self.pe_cluster_name),
            ("subnet", self.subnet_name),
            ("nkp binary", str(self.nkp_bin)),
            ("kubernetes", self.k8s_version),
            ("cluster prefix", self.cluster_prefix),
            ("VIP pool", self.vip_pool),
        ]
