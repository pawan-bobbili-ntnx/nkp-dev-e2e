# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prism Central v3 API: capacity preflight and orphan cleanup.

The dev PC is shared and frequently near capacity, so scenarios check before
they create and sweep after they finish - a failed `nkp delete` otherwise
leaves VMs holding memory for days.
"""

from __future__ import annotations

import base64
import json
import time
import os
import ssl
import urllib.error
import urllib.request

from .config import Config
from .logging_ import Log


class PrismCentral:
    def __init__(self, config: Config, log: Log):
        self.config = config
        self.log = log
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.config.dry_run:
            self.log.info(f"[dry-run] PC {method} {path}")
            return {}
        token = base64.b64encode(
            f"{self.config.pc_user}:{self.config.pc_password}".encode()
        ).decode()
        req = urllib.request.Request(
            self.config.pc_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Authorization", "Basic " + token)
        req.add_header("Content-Type", "application/json")
        # TRANSPORT retries only. A TLS handshake timeout or dropped connection
        # to Prism Central is a network blip, not an answer - but without a
        # retry it strands whatever the call was doing. Live 2026-08-30: one
        # `_ssl.c:1063 handshake operation timed out` during teardown left a VM
        # behind, which then failed the whole run's cleanup 15 minutes later
        # (PC itself was fine seconds afterwards: 200 in 4s). HTTP responses are
        # NOT retried - a 4xx/5xx is the server's answer and must surface.
        last = None
        for attempt in (1, 2, 3):
            try:
                with urllib.request.urlopen(req, context=self._ctx, timeout=60) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode()[:300]
                if exc.code == 401:
                    raise RuntimeError(
                        "Prism Central rejected the credentials (401). They rotate every "
                        "few days - refresh with: cd <tam-cli> && ./tam login pc-dev"
                    ) from exc
                raise RuntimeError(f"PC {method} {path} -> {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < 3:
                    self.log.warn(
                        f"PC {method} {path}: transport error ({str(exc)[:80]}) - "
                        f"retry {attempt}/2")
                    time.sleep(5 * attempt)
        raise RuntimeError(
            f"PC {method} {path} failed after 3 transport attempts: {last}") from last

    # ---------------------------------------------------------------- checks
    def reachable(self) -> bool:
        try:
            self._call("POST", "/api/nutanix/v3/clusters/list", {"kind": "cluster", "length": 1})
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warn(f"Prism Central not reachable: {exc}")
            return False

    def vms_named(self, prefix: str) -> list[dict]:
        body = {"kind": "vm", "filter": f"vm_name=={prefix}.*", "length": 200}
        data = self._call("POST", "/api/nutanix/v3/vms/list", body)
        return data.get("entities", [])

    def image_exists(self, name: str) -> bool | None:
        """Is this machine image in Prism's image store? None = cannot tell.

        Asks the server for THIS name rather than listing and searching. The
        first version listed 500 images and looked for the name in the result;
        Prism holds 1052, so it saw under half the store and reported a present
        image as missing - blocking a legitimate run. It also compared against
        a de-duplicated set, so the "did I see a full page?" guard never fired.

        Conservative by design: only an explicit empty result for an exact-name
        query is a negative. Any transport or shape surprise returns None so a
        preflight can never block a run that would have worked.
        """
        if not name or self.config.dry_run:
            return None
        try:
            data = self._call("POST", "/api/nutanix/v3/images/list",
                              {"kind": "image", "length": 20,
                               "filter": f"name=={name}"})
        except Exception:  # noqa: BLE001
            return None
        ents = data.get("entities")
        if ents is None:
            return None
        for e in ents:
            if e.get("status", {}).get("name") == name:
                return True
        # the server understood the query and matched nothing
        return False if isinstance(ents, list) else None

    def free_memory_gib(self) -> float | None:
        """Rough free memory across hosts of the target PE, for a preflight."""
        try:
            data = self._call("POST", "/api/nutanix/v3/hosts/list", {"kind": "host", "length": 200})
        except Exception:  # noqa: BLE001 - preflight must never be fatal
            return None
        total = 0.0
        for host in data.get("entities", []):
            res = host.get("status", {}).get("resources", {})
            cluster = host.get("status", {}).get("cluster_reference", {}).get("name")
            if cluster and cluster != self.config.pe_cluster_name:
                continue
            capacity = res.get("memory_capacity_mib")
            if capacity:
                total += capacity / 1024.0
        return total or None

    # --------------------------------------------------------------- cleanup
    def _wait_task(self, task_uuid: str, *, timeout_s: int = 120) -> tuple[bool, str]:
        """Poll a v3 task to its terminal state. A 202 is a promise, not a result."""
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            task = self._call("GET", f"/api/nutanix/v3/tasks/{task_uuid}")
            status = task.get("status", "")
            if status in ("SUCCEEDED",):
                return True, ""
            if status in ("FAILED", "ABORTED"):
                detail = task.get("error_detail") or task.get("error_code") or status
                return False, str(detail)
            time.sleep(3)
        return False, f"task {task_uuid} still running after {timeout_s}s"

    def _task_of(self, response: dict) -> str | None:
        return (response.get("status", {}).get("execution_context", {})
                .get("task_uuid"))

    def power_off_vm(self, uuid: str) -> None:
        """Request OFF via a spec update (v3 has no dedicated power endpoint)."""
        vm = self._call("GET", f"/api/nutanix/v3/vms/{uuid}")
        vm.pop("status", None)
        vm["spec"]["resources"]["power_state"] = "OFF"
        resp = self._call("PUT", f"/api/nutanix/v3/vms/{uuid}", vm)
        task = self._task_of(resp)
        if task:
            ok, err = self._wait_task(task)
            if not ok:
                raise RuntimeError(f"power-off failed: {err}")

    def _vg_call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Volumes v4 call; unlike v3 it demands a unique NTNX-Request-Id."""
        import uuid as uuidlib

        token = base64.b64encode(
            f"{self.config.pc_user}:{self.config.pc_password}".encode()
        ).decode()
        req = urllib.request.Request(
            self.config.pc_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Authorization", "Basic " + token)
        req.add_header("Content-Type", "application/json")
        req.add_header("NTNX-Request-Id", str(uuidlib.uuid4()))
        with urllib.request.urlopen(req, context=self._ctx, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}

    def _detach_volume_groups(self, uuid: str) -> list[str]:
        """Detach every VG in the VM's disk spec (volumes v4); returns VG ids.

        CSI-provisioned volume groups survive `nkp delete cluster` failures and
        block VM deletion with "Volume group at scsi.N must be detached". The
        VM's own disk list is the authoritative attachment record.
        """
        vm = self._call("GET", f"/api/nutanix/v3/vms/{uuid}")
        vgs: set[str] = set()
        for view in (vm.get("spec", {}), vm.get("status", {})):
            for disk in view.get("resources", {}).get("disk_list", []):
                ref = disk.get("volume_group_reference")
                if ref and ref.get("uuid"):
                    vgs.add(ref["uuid"])
        for vg_id in vgs:
            self._vg_call(
                "POST",
                f"/api/volumes/v4.0/config/volume-groups/{vg_id}/$actions/detach-vm",
                {"extId": uuid},
            )
        return sorted(vgs)

    def delete_vm(self, uuid: str) -> None:
        resp = self._call("DELETE", f"/api/nutanix/v3/vms/{uuid}")
        task = self._task_of(resp)
        if not task:
            return
        ok, err = self._wait_task(task)
        if ok:
            return
        if "must be detached" in err or "olume group" in err:
            import time

            vgs = self._detach_volume_groups(uuid)
            self.log.warn(f"detached {len(vgs)} volume group(s) from {uuid}; retrying delete")
            time.sleep(5)  # detach tasks are async too; give them a beat
            resp = self._call("DELETE", f"/api/nutanix/v3/vms/{uuid}")
            task = self._task_of(resp)
            ok, err = self._wait_task(task) if task else (True, "")
            # The detached VGs are CSI leftovers of a deleted cluster: without
            # this they linger and fight future claims over the same volumes.
            for vg_id in vgs if ok else []:
                try:
                    self._vg_call(
                        "DELETE", f"/api/volumes/v4.0/config/volume-groups/{vg_id}")
                except Exception as exc:  # noqa: BLE001
                    self.log.warn(f"orphan VG {vg_id} not deleted: {exc}")
        if not ok:
            raise RuntimeError(err)

    def sweep(self, prefix: str) -> int:
        """Delete leftover VMs whose name starts with ``prefix``.

        `nkp delete cluster` is the primary path; this catches the case where it
        failed half way and left VMs behind. Running VMs are powered off first -
        a DELETE on a running VM is accepted (202) and then fails asynchronously,
        which without task polling looks exactly like success.
        """
        if self.config.dry_run:
            self.log.info(f"[dry-run] would sweep VMs named {prefix}*")
            return 0
        # NEVER delete a frozen template's VMs: templates are frozen under
        # the cluster's own name, so a later create of the same scenario
        # shares their prefix (live-caught: a sweep ate a fresh template).
        protected: set = set()
        import json as _json
        from pathlib import Path as _P
        state = _P(os.environ.get("SPEEDSTART_DIR", ""))
        if state.is_dir():
            for mf in state.glob("*-freeze-manifest.json"):
                try:
                    protected |= {v["uuid"] for v in _json.load(mf.open())["vms"]}
                except Exception:  # noqa: BLE001
                    continue
        leftovers = self.vms_named(prefix)
        swept = 0
        for vm in leftovers:
            name = vm.get("status", {}).get("name", "?")
            uuid = vm.get("metadata", {}).get("uuid")
            power = vm.get("status", {}).get("resources", {}).get("power_state")
            if uuid in protected:
                self.log.info(f"NOT sweeping {name}: frozen template VM")
                continue
            self.log.warn(f"sweeping leftover VM {name} ({uuid}, {power})")
            try:
                if power == "ON":
                    self.power_off_vm(uuid)
                self.delete_vm(uuid)
                swept += 1
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"could not delete {name}: {exc}")
        return swept
