# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven scenarios: a YAML file becomes a Scenario.

    name: day2-drain
    description: Drain a worker and verify the cluster reconciles
    topology: 1 control plane + 1 worker
    expected_minutes: 45

    prepare:
      - create_cluster: {control_plane: 1, workers: 1}
      - wait_nodes_ready: {count: 2}

    execute:
      - drain_node: {role: worker}
      - wait: {duration: 2m, reason: let the scheduler settle}
      - uncordon_node: {role: worker}

    check:
      - wait_reconciled: {timeout: 20m}
      - assert_node_count: {count: 2}
      - assert_pods_healthy: {namespace: kube-system}

    cleanup:
      - delete_cluster

Steps come from ``steps.py``; ``run_e2e.py --steps`` lists them. Python
scenarios still work for anything that needs real logic.
"""

from __future__ import annotations

import difflib
import os
import re

from pathlib import Path
from typing import Any

import yaml

from .core import Context, Scenario
from .steps import STEPS

#: the test itself: one ordered list where work and assertions interleave
FLAT_PHASE = "steps"

#: phases a scenario file may declare. The runner guarantees the last two:
#: collect on failure, cleanup always.
PHASES = (FLAT_PHASE, "collect", "cleanup")

#: the old three-phase form, kept only to give a useful error
LEGACY_PHASES = ("prepare", "execute", "check")

#: structural markers, not steps - they label a run rather than doing anything
MARKERS = ("group",)

#: ${VAR} or ${VAR:-fallback} anywhere in the YAML
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SpecError(RuntimeError):
    pass


#: Steps that were renamed. A scenario using the old name gets told why rather
#: than a bare "unknown step", because the rename fixed a MEANING, not a typo.
RENAMED = {
    "upgrade_cluster": (
        "upgrade_kommander",
        "It runs `nkp upgrade kommander`, which moves the platform only. The "
        "node/Kubernetes roll is `upgrade_nodes` (`nkp upgrade cluster "
        "nutanix`), so the old name named the wrong verb.",
    ),
}


def _expand(value: Any, scenario_env: dict | None = None) -> Any:
    """Substitute ${ENV_VAR} references so a scenario can be parameterised.

    Resolution order: the developer's shell > the scenario's own ``env:``
    defaults > the inline ``:-fallback``. Unset variables become empty rather
    than an error, because --list must work without a configured environment;
    a step that needs a value fails loudly by name when it is missing.
    """
    env = scenario_env or {}

    def sub(m):
        name = m.group(1)
        return os.environ.get(name) or str(env.get(name, "")) or (m.group(2) or "")

    if isinstance(value, str):
        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def _normalise(entry: Any, where: str) -> tuple[str, dict]:
    """Accept `- step_name` or `- step_name: {args}` or `- {step: name, ...}`."""
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict):
        if "step" in entry:
            params = {k: v for k, v in entry.items() if k != "step"}
            return entry["step"], params
        if len(entry) != 1:
            raise SpecError(
                f"{where}: expected one step per list item, got keys {list(entry)}"
            )
        name, params = next(iter(entry.items()))
        if params is None:
            params = {}
        if not isinstance(params, dict):
            # `- wait: 2m` is a convenient shorthand for a single-argument step
            params = {_default_arg(name): params}
        return name, params
    raise SpecError(f"{where}: cannot read step {entry!r}")


#: shorthand argument for steps commonly written as `- step: value`
_SHORTHAND = {"wait": "duration", "wait_nodes_ready": "count", "group": "label",
              "assert_node_count": "count", "scale_workers": "replicas"}


def _default_arg(step_name: str) -> str:
    if step_name not in _SHORTHAND:
        raise SpecError(
            f"step '{step_name}' needs named arguments, e.g. "
            f"- {step_name}: {{key: value}}"
        )
    return _SHORTHAND[step_name]


def load(path: Path) -> type[Scenario]:
    """Build a Scenario subclass from a YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")

    # the scenario's own env: defaults take part in ${...} interpolation,
    # scoped to THIS file - they never leak into other scenarios' loads
    raw_env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    raw = _expand(raw, raw_env)
    name = raw.get("name") or path.stem
    legacy = [p for p in LEGACY_PHASES if p in raw]
    if legacy:
        raise SpecError(
            f"{path}: '{', '.join(legacy)}' is no longer a phase. Put everything "
            f"under '{FLAT_PHASE}:' as one ordered list - setup, actions and "
            "assertions together, in the order they happen. 'collect:' and "
            "'cleanup:' are unchanged."
        )

    retired = [k for k in ("topology", "requires_env") if k in raw]
    if retired:
        raise SpecError(
            f"{path}: '{', '.join(retired)}' is retired. Steps declare their own "
            "needs (a step that reads an env var fails loudly without it); use "
            "'env:' for scenario-scoped defaults."
        )
    unknown_top = set(raw) - {"name", "description", "expected_minutes", "env", *PHASES}
    if unknown_top:
        raise SpecError(f"{path}: unknown key(s): {', '.join(sorted(unknown_top))}")

    plans: dict[str, list[tuple[str, dict]]] = {}
    for phase in PHASES:
        entries = raw.get(phase) or []
        if not isinstance(entries, list):
            raise SpecError(f"{path}: '{phase}' must be a list of steps")
        plan = []
        for index, entry in enumerate(entries):
            step_name, params = _normalise(entry, f"{path}:{phase}[{index}]")
            if step_name in MARKERS:
                plan.append((step_name, params))
                continue
            if step_name in RENAMED:
                new, why = RENAMED[step_name]
                raise SpecError(
                    f"{path}: '{phase}' uses '{step_name}', which is now "
                    f"'{new}'. {why}"
                )
            if step_name not in STEPS:
                # Substring matching misses a typo in the middle of a word
                # ('assert_node_kount'), which is the common case.
                close = difflib.get_close_matches(step_name, STEPS, n=3, cutoff=0.6)
                close += [s for s in STEPS if step_name in s and s not in close]
                hint = f" Did you mean: {', '.join(close[:3])}?" if close else ""
                raise SpecError(
                    f"{path}: '{phase}' uses unknown step '{step_name}'.{hint} "
                    "Run run_e2e.py --steps for the catalogue."
                )
            plan.append((step_name, params))
        plans[phase] = plan

    scenario_env = raw.get("env") or {}
    if not isinstance(scenario_env, dict):
        raise SpecError(f"{path}: 'env' must be a mapping of NAME: value")

    def apply_env(ctx: Context) -> None:
        # Scenario-scoped defaults; a developer's shell export always wins.
        for key, value in scenario_env.items():
            if os.environ.setdefault(key, str(value)) == str(value):
                ctx.log.debug(f"env default: {key}={value}")

    def make_phase(phase_name: str):
        def run_phase(self, ctx: Context) -> None:
            if phase_name == FLAT_PHASE:
                apply_env(ctx)
                # Guard rail, not a step: every scenario talks to Prism
                # Central sooner or later, so unreachable PC fails in second
                # one - scenario authors never write this check themselves.
                STEPS["require_prism_central"](ctx)
            plan = plans[phase_name]
            total = sum(1 for n, _ in plan if n not in MARKERS)
            position = 0
            group = ""
            for step_name, params in plan:
                if step_name == "group":
                    group = params.get("label", "")
                    ctx.log.banner(group, level=2)
                    continue
                position += 1
                where = f"step {position}/{total} '{step_name}'"
                if group:
                    where += f" [{group}]"
                ctx.log.info(f"step: {step_name}" + (f" {params}" if params else ""))
                try:
                    STEPS[step_name](ctx, **params)
                except SpecError:
                    raise
                except TypeError as exc:
                    # A bad argument in YAML should read like a config error,
                    # not a Python stack trace.
                    prose = [k for k in params if " " in k]
                    hint = (
                        f" Quote the value - an unquoted comma in {{...}} turns the "
                        f"rest of the text into another key ({prose[0]!r})."
                        if prose else ""
                    )
                    raise SpecError(
                        f"{where} rejected {params}: {exc}.{hint}"
                    ) from exc
                except Exception as exc:
                    # Say which step failed, not just which phase. Re-raised as
                    # the original type so the report still reads
                    # "AssertionError: step 6/11 ...".
                    enriched = f"{where}: {exc}"
                    try:
                        raise type(exc)(enriched) from exc
                    except TypeError:
                        raise RuntimeError(enriched) from exc

        return run_phase

    attrs: dict[str, Any] = {
        "name": name,
        "description": raw.get("description", ""),
        "expected_minutes": int(raw.get("expected_minutes", 30)),
        "source": str(path),
    }
    if not plans[FLAT_PHASE]:
        raise SpecError(f"{path}: needs a '{FLAT_PHASE}:' list - that is the test")
    for phase in PHASES:
        if plans[phase]:
            attrs[phase] = make_phase(phase)
    # A config scenario with no explicit collect still gets diagnostics on
    # failure, because that is the behaviour people expect.
    if not plans["collect"]:
        # collect_diagnostics, NOT collect_cluster: the former adds the real
        # `nkp diagnose` support bundle on top of the state dumps. Scenarios
        # used to list it explicitly; when those lines were removed as
        # boilerplate the default silently downgraded failure forensics to
        # kubectl dumps only. The default must be the stronger one.
        from . import steps as _steps

        attrs["collect"] = lambda self, ctx: _steps.STEPS["collect_diagnostics"](ctx)

    # Nor should anyone have to remember to tear down what they asked for. A
    # scenario that produced a cluster gets finish_cluster unless it says
    # otherwise - and finish_cluster already owns the decision (sweep a claim,
    # freeze a create, keep a half-built one, leave a SUPPLIED one alone), so
    # there was never a choice for the author to make here.
    if not plans["cleanup"]:
        made_cluster = any(
            n in {"create_cluster", "claim_cluster", "traditional_create",
                  "use_existing_cluster"}
            for n, _ in plans[FLAT_PHASE])
        if made_cluster:
            from . import steps as _steps

            attrs["cleanup"] = lambda self, ctx: _steps.STEPS["finish_cluster"](ctx)

    return type(f"Spec_{name.replace('-', '_')}", (Scenario,), attrs)


def discover_specs(directory: Path) -> dict[str, type[Scenario]]:
    found = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        if path.name.startswith("."):
            # macOS tarballs ship AppleDouble ("._foo.yaml") sidecars; they are
            # binary, not scenarios.
            continue
        cls = load(path)
        found[cls.name] = cls
    return found
