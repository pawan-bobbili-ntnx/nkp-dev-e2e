# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scenario framework: steps -> collect (on failure) -> cleanup (always).

Adding a scenario is one YAML file in ``scenarios/``. The runner owns
ordering, timing, artifacts and reporting so a scenario only describes
*what* it does, never the plumbing.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .kube import Kube
from .logging_ import Log
from .netpool import allocate
from .nkp import Nkp
from .pc import PrismCentral


@dataclass
class Context:
    """Everything a scenario is handed. One instance per scenario run."""

    config: Config
    log: Log
    artifacts: Path
    nkp: Nkp
    pc: PrismCentral
    # Populated by scenarios that create a cluster; the runner uses it for
    # best-effort cleanup if a scenario dies before its own cleanup runs.
    cluster_name: str | None = None
    kubeconfig: Path | None = None
    _kube: Kube | None = field(default=None, repr=False)
    _addresses: tuple[str, str] | None = field(default=None, repr=False)
    _notes: dict = field(default_factory=dict, repr=False)

    @property
    def kube(self) -> Kube:
        if self._kube is None:
            if self.kubeconfig is None:
                raise RuntimeError("kubeconfig not set yet - create a cluster first")
            self._kube = Kube(self.kubeconfig, self.log, self.config)
        return self._kube

    def switch_kubeconfig(self, path) -> None:
        """Point ctx at a different cluster; the cached kubectl wrapper resets."""
        from pathlib import Path
        self.kubeconfig = Path(path)
        self._kube = None

    def remember(self, key: str, value) -> None:
        """Stash a value for a later step (which node was drained, etc.)."""
        self._notes[key] = value

    def recall(self, key: str, default=None):
        return self._notes.get(key, default)

    def addresses(self) -> tuple[str, str]:
        """(control-plane VIP, service load-balancer range) for this scenario.

        Allocated once and cached, so prepare/execute see the same pair.
        """
        if self._addresses is None:
            vip, lb = allocate(
                self.config.vip_pool, 2, self.log, dry_run=self.config.dry_run,
                reserved=_template_addresses(), owner=self.cluster(),
            )
            self._addresses = (vip, f"{lb}-{lb}")
        return self._addresses

    def cluster(self, suffix: str = "") -> str:
        """A unique, traceable cluster name for this scenario."""
        base = f"{self.config.cluster_prefix}-{self.scenario_slug}"
        return f"{base}{suffix}"[:40].rstrip("-")

    scenario_slug: str = ""


class Scenario:
    """Base class.

    A scenario is one ordered list of steps, plus two phases the runner
    guarantees around it: ``collect`` runs when something fails, and ``cleanup``
    always runs. Work and assertions live together in ``steps`` because that is
    how tests actually read - do a thing, check it, do the next thing.
    """

    #: short, stable identifier used on the command line
    name: str = ""
    #: one line shown by --list
    description: str = ""
    #: informational: what the scenario needs, e.g. "1 control plane + 1 worker"
    topology: str = ""
    #: rough expectation so the runner can warn before a long run
    expected_minutes: int = 30
    #: what the runner executes before collect/cleanup
    main_phases: tuple[str, ...] = ("steps",)

    def steps(self, ctx: Context) -> None:
        """The test: preconditions, actions and assertions, in order.

        Raise to fail the scenario.
        """

    def collect(self, ctx: Context) -> None:
        """Diagnostics. The runner calls this on failure, and on success only
        when --always-collect is passed."""

    def cleanup(self, ctx: Context) -> None:
        """Tear down whatever the steps created. Must be idempotent: it runs
        even when the steps failed, and may run twice."""


#: what the runner executes before collect/cleanup
MAIN_PHASES = ("steps",)


@dataclass
class PhaseResult:
    name: str
    ok: bool
    seconds: float
    error: str = ""


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    seconds: float
    phases: list[PhaseResult] = field(default_factory=list)
    error: str = ""
    skipped_reason: str = ""

    @property
    def status(self) -> str:
        if self.skipped_reason:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


class Runner:
    """Runs scenarios and guarantees the phase contract."""

    def __init__(self, config: Config, log: Log):
        self.config = config
        self.log = log

    def run(self, scenario: Scenario) -> ScenarioResult:
        slug = scenario.name
        artifacts = self.config.artifacts_dir / slug
        artifacts.mkdir(parents=True, exist_ok=True)
        log = self.log.child(slug, artifacts / "scenario.log")

        ctx = Context(
            config=self.config,
            log=log,
            artifacts=artifacts,
            nkp=Nkp(self.config, log),
            pc=PrismCentral(self.config, log),
            scenario_slug=slug,
        )

        log.banner(f"{slug}: {scenario.description}")
        if scenario.topology:
            log.info(f"topology: {scenario.topology}")
        if self.config.dry_run:
            log.info("DRY RUN - external commands are printed, not executed")

        started = time.monotonic()
        result = ScenarioResult(name=slug, ok=True, seconds=0.0)
        failed = False

        for phase in scenario.main_phases:
            pr = self._phase(scenario, ctx, phase)
            result.phases.append(pr)
            if not pr.ok:
                result.ok = False
                result.error = f"{phase}: {pr.error}"
                failed = True
                break

        # Diagnostics: always on failure, optional on success. Never allowed to
        # mask the original error.
        if failed or self.config.always_collect:
            result.phases.append(self._phase(scenario, ctx, "collect", tolerant=True))

        # Cleanup always runs unless the operator asked to keep the environment.
        if self.config.keep:
            log.info(f"--keep set: leaving {ctx.cluster_name or 'environment'} in place")
        else:
            result.phases.append(self._phase(scenario, ctx, "cleanup", tolerant=True))

        result.seconds = time.monotonic() - started
        log.banner(f"{slug}: {result.status} in {result.seconds:.0f}s")
        return result

    def _phase(
        self, scenario: Scenario, ctx: Context, name: str, tolerant: bool = False
    ) -> PhaseResult:
        fn: Callable[[Context], None] = getattr(scenario, name)
        ctx.log.banner(f"[{name}]", level=2)
        started = time.monotonic()
        try:
            fn(ctx)
            return PhaseResult(name, True, time.monotonic() - started)
        except KeyboardInterrupt:
            # Ctrl-C is not an Exception, so without this it would skip the
            # cleanup phase entirely and strand VMs - the one outcome the
            # contract exists to prevent.
            elapsed = time.monotonic() - started
            ctx.log.warn(f"{name} interrupted - cleanup will still run")
            return PhaseResult(name, False, elapsed, "interrupted")
        except Exception as exc:  # noqa: BLE001 - a scenario may raise anything
            elapsed = time.monotonic() - started
            detail = f"{type(exc).__name__}: {exc}"
            ctx.log.error(f"{name} failed: {detail}")
            ctx.log.debug(traceback.format_exc())
            if tolerant:
                # A failure to collect diagnostics or clean up is reported but
                # does not change the scenario verdict.
                ctx.log.warn(f"{name} failed but is not fatal to the verdict")
                return PhaseResult(name, False, elapsed, detail)
            return PhaseResult(name, False, elapsed, detail)


def _template_addresses() -> set[str]:
    """VIP/LB of every registered template - never hand these to a claim.

    A frozen template is powered OFF, so netpool's ping probe sees its VIP as
    free. Handing that address to a claim is actively harmful: the claim
    byte-replaces the template VIP with the new VIP across at-rest state, so it
    would rewrite the very address it had just been allocated.
    """
    import json
    import os
    from pathlib import Path

    state = os.environ.get("SPEEDSTART_DIR")
    path = (Path(state) / "templates.json") if state else None
    if not path or not path.exists():
        return set()
    try:
        reg = json.load(open(path))
    except Exception:
        return set()
    out: set[str] = set()
    for entry in reg.values():
        for key in ("vip", "lb"):
            val = (entry or {}).get(key) or ""
            for part in str(val).split("-"):
                part = part.strip()
                if part:
                    out.add(part)
    return out
