# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Static IP allocation for cluster VIPs and service load balancers.

`nkp create cluster nutanix` requires --control-plane-endpoint-ip and
--kubernetes-service-load-balancer-ip-range. Node addresses come from DHCP, but
these two are static and must not collide - including with another developer
running the same scenario at the same time. Addresses are picked at random from
a configured pool and probed before use, so two concurrent runs are unlikely to
choose the same one and a stale cluster's address is never reused.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import random
import subprocess
import time
from pathlib import Path

from .logging_ import Log

#: Addresses handed out but not yet visible to a ping probe live here, so a
#: second run starting at the same moment cannot pick them too. A probe alone
#: is a race: both runs ping, both see silence, both take the address, and the
#: loser fails minutes later inside `nkp create cluster`.
LEASE_TTL_S = 6 * 3600          # longer than the longest scenario; a dead run
                                # must not hold addresses for ever


def _lease_path() -> Path:
    home = os.environ.get("SPEEDSTART_DIR") or os.environ.get("E2E_STATE_DIR")
    base = Path(home) if home else Path.home() / ".cache/nkp-dev-e2e"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ip-leases.json"


class _FileLock:
    """Cross-process lock. flock is advisory but every allocator here uses it."""

    def __init__(self, path: Path):
        self.path = path.with_suffix(".lock")
        self.fh = None

    def __enter__(self):
        import fcntl

        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl

        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        self.fh.close()


def _load_leases(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - a corrupt or absent file means no leases
        return {}
    now = time.time()
    return {ip: v for ip, v in data.items()
            if isinstance(v, dict) and now - v.get("at", 0) < LEASE_TTL_S}


def release(owner: str, log: Log | None = None) -> int:
    """Drop every address leased by ``owner``. Safe to call more than once."""
    path = _lease_path()
    try:
        with _FileLock(path):
            leases = _load_leases(path)
            keep = {ip: v for ip, v in leases.items() if v.get("owner") != owner}
            freed = len(leases) - len(keep)
            path.write_text(json.dumps(keep, indent=1))
    except Exception as exc:  # noqa: BLE001 - releasing must never fail a run
        if log:
            log.warn(f"could not release address leases: {exc}")
        return 0
    if log and freed:
        log.info(f"released {freed} address lease(s)")
    return freed


class PoolExhausted(RuntimeError):
    pass


def _reachable(ip: str, timeout_s: int = 1) -> bool:
    """True if something answers on this address, i.e. it is NOT free."""
    if platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-t", str(timeout_s), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), ip]
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=timeout_s + 3
        ).returncode == 0
    except Exception:  # noqa: BLE001 - treat a broken probe as "in use"
        return True


def hold(addresses: list[str], owner: str, log: Log, *, dry_run: bool = False) -> None:
    """Lease SPECIFIC addresses, or refuse because someone else holds them.

    The pool allocator picks free addresses; this claims named ones. It exists
    for the address-preserving claim, which reuses a frozen template's own VIP
    and LB and therefore cannot go through the pool at all. Without it that
    path took no lease of any kind, so two people claiming the same template at
    once would both proceed and their clusters would fight over the same two
    IPs on the wire - kube-vip and MetalLB on both announcing the same address.
    """
    if dry_run:
        return
    path = _lease_path()
    with _FileLock(path):
        leases = _load_leases(path)
        taken = {ip: leases[ip] for ip in addresses if ip in leases and leases[ip].get("owner") != owner}
        if taken:
            who = ", ".join(f"{ip} (held by {v.get('owner')})" for ip, v in sorted(taken.items()))
            raise PoolExhausted(
                f"cannot preserve template addresses - {who}. This template already "
                "has a live clone: preserving addresses means at most ONE clone at a "
                "time. Wait for it to finish, or claim without preserve_addresses.")
        now = time.time()
        leases.update({ip: {"owner": owner, "at": now} for ip in addresses})
        path.write_text(json.dumps(leases, indent=1))
    log.info(f"holding {', '.join(addresses)} for {owner}")


def allocate(pool: str, count: int, log: Log, *, dry_run: bool = False,
             reserved: set[str] | None = None, owner: str = "") -> list[str]:
    """Return ``count`` addresses from ``pool`` ("start-end") that do not answer.

    A ping probe is a heuristic: a host that ignores ICMP looks free. It is the
    same check the manual runbooks use, and cluster creation fails loudly if the
    address turns out to be taken, so a wrong guess is noisy rather than silent.
    """
    # RESERVED addresses are excluded before probing. A ping probe cannot see
    # them: a frozen template is POWERED OFF, so its VIP never answers and looks
    # free - then a claim takes it, and the claim's byte-replace of
    # "template VIP -> new VIP" also rewrites the value it just allocated.
    # Live 2026-08-31: a GA template frozen at .134 sat inside the claim pool
    # and a claim was handed .134 as its load-balancer address.
    reserved = set(reserved or ())
    start_s, _, end_s = pool.partition("-")
    start = ipaddress.IPv4Address(start_s.strip())
    end = ipaddress.IPv4Address(end_s.strip() or start_s.strip())
    candidates = [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
    if reserved:
        blocked = [c for c in candidates if c in reserved]
        candidates = [c for c in candidates if c not in reserved]
        if blocked:
            log.info(f"pool {pool}: skipping {len(blocked)} address(es) held by "
                     f"frozen templates ({', '.join(sorted(blocked))})")
    if len(candidates) < count:
        raise PoolExhausted(f"pool {pool} holds {len(candidates)} address(es), need {count}")

    if dry_run:
        log.info(f"[dry-run] would allocate {count} address(es) from {pool}")
        return candidates[:count]

    # Probe AND record the lease under one lock, so two runs starting together
    # cannot both conclude the same address is free. The lock is held only for
    # the probe (a few seconds), which is exactly the window that used to race.
    path = _lease_path()
    owner = owner or f"{os.getpid()}@{platform.node()}"
    with _FileLock(path):
        leases = _load_leases(path)
        held = set(leases)
        if held:
            before = len(candidates)
            candidates = [c for c in candidates if c not in held]
            if len(candidates) < before:
                log.info(f"pool {pool}: skipping {before - len(candidates)} address(es) "
                         f"leased by another run")
        if len(candidates) < count:
            raise PoolExhausted(
                f"pool {pool} has {len(candidates)} address(es) free of leases, need {count}")
        random.shuffle(candidates)
        free: list[str] = []
        probed = 0
        for ip in candidates:
            if len(free) == count:
                break
            probed += 1
            if not _reachable(ip):
                free.append(ip)
        if len(free) < count:
            raise PoolExhausted(
                f"only found {len(free)} free address(es) in {pool} after probing {probed}; "
                "someone else may be mid-run, or the pool needs widening (E2E_VIP_POOL)"
            )
        now = time.time()
        leases.update({ip: {"owner": owner, "at": now} for ip in free})
        path.write_text(json.dumps(leases, indent=1))
    log.info(f"allocated {', '.join(free)} from {pool} (leased to {owner})")
    return free
