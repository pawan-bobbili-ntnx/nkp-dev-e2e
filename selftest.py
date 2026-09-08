#!/usr/bin/env python3
# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Self-test for the framework's own logic.

Run it with ``./hack/dev-e2e/selftest.py``. It needs no cluster and no
credentials, and takes about a second.

Most of the framework is glue around kubectl and the nkp CLI, which only a real
run can exercise. The exceptions are the pure functions that decide whether
something is healthy - and a harness that reports "all good" incorrectly is
worse than no harness, so those are pinned here.

The HelmRelease fixtures mirror the shapes kommander's own kuttl tests assert
on (tests/kuttl-multi-cluster/test-catalog).
"""

from __future__ import annotations

import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework.kube import Kube  # noqa: E402
from framework.spec import SpecError, load  # noqa: E402
from framework.steps import STEPS, parse_duration  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  pass  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def check_true(label: str, got: bool) -> None:
    check(label, bool(got), True)


def hr(namespace: str, name: str, conditions: list[tuple]) -> dict:
    return {
        "metadata": {"namespace": namespace, "name": name},
        "status": {
            "conditions": [
                {"type": t, "status": s, "reason": r, "message": m}
                for t, s, r, m in conditions
            ]
        },
    }


def test_helmrelease_readiness() -> None:
    print("\nHelmRelease readiness")
    problem = Kube.helmrelease_problem

    check(
        "Ready + Released is healthy",
        problem(hr("catalog-project", "nginx", [
            ("Ready", "True", "InstallSucceeded", "Helm install succeeded"),
            ("Released", "True", "InstallSucceeded", "Helm install succeeded"),
        ])),
        None,
    )

    # Released is only reported once a release has been attempted, so a Ready
    # release without it is not a failure.
    check(
        "Ready with no Released condition is healthy",
        problem(hr("ws", "app", [("Ready", "True", "ReconciliationSucceeded", "ok")])),
        None,
    )

    failed = problem(hr("ws", "nginx", [
        ("Ready", "False", "InstallFailed", "Helm install failed: timed out\nsecond line"),
    ]))
    check_true("a failed install is caught", failed is not None)
    check_true("the reason is reported", "InstallFailed" in (failed or ""))
    check_true("the message is reported", "timed out" in (failed or ""))
    check_true("only the first message line is used", "second line" not in (failed or ""))

    # The subtle one: Helm can report Ready on a release it never installed.
    check_true(
        "Ready=True but Released=False is caught",
        problem(hr("ws", "app", [
            ("Ready", "True", "", ""),
            ("Released", "False", "UpgradeFailed", "retries exhausted"),
        ])) is not None,
    )

    check_true(
        "a HelmRelease with no status is caught",
        problem({"metadata": {"namespace": "ws", "name": "fresh"}, "status": {}}) is not None,
    )


def test_durations() -> None:
    print("\nDurations")
    check("90s", parse_duration("90s"), 90)
    check("5m", parse_duration("5m"), 300)
    check("1h", parse_duration("1h"), 3600)
    check("bare number is seconds", parse_duration(45), 45)
    try:
        parse_duration("soon")
        FAILURES.append("nonsense duration should raise")
        print("  FAIL  nonsense duration should raise")
    except ValueError:
        print("  pass  nonsense duration is rejected")


def test_scenarios_load(directory: Path) -> None:
    print(f"\nScenarios in {directory.name}/")
    files = sorted(directory.glob("*.yaml"))
    check_true("there are scenarios to load", bool(files))
    for path in files:
        try:
            cls = load(path)
        except SpecError as exc:
            FAILURES.append(path.name)
            print(f"  FAIL  {path.name}: {exc}")
            continue
        problems = []
        if not cls.description:
            problems.append("no description")
        if not cls.name:
            problems.append("no name")
        if problems:
            FAILURES.append(path.name)
            print(f"  FAIL  {path.name}: {', '.join(problems)}")
        else:
            print(f"  pass  {path.name} -> {cls.name}")


def test_bad_specs(tmp: Path) -> None:
    print("\nBad config is reported as config, not as a stack trace")

    unknown = tmp / "unknown-step.yaml"
    unknown.write_text("name: x\nsteps:\n  - assert_node_kount: {count: 1}\n")
    try:
        load(unknown)
        FAILURES.append("unknown step should raise SpecError")
        print("  FAIL  unknown step should raise SpecError")
    except SpecError as exc:
        check_true("an unknown step names the offender", "assert_node_kount" in str(exc))
        check_true("and suggests a real step", "assert_node_count" in str(exc))

    unknown_key = tmp / "unknown-key.yaml"
    unknown_key.write_text("name: x\nstpes:\n  - log: {message: hi}\n")
    try:
        load(unknown_key)
        FAILURES.append("unknown top-level key should raise SpecError")
        print("  FAIL  unknown top-level key should raise SpecError")
    except SpecError as exc:
        check_true("a mistyped phase is reported", "stpes" in str(exc))

    no_steps = tmp / "no-steps.yaml"
    no_steps.write_text("name: x\ncleanup:\n  - log: {message: bye}\n")
    try:
        load(no_steps)
        FAILURES.append("a scenario with no steps should be rejected")
        print("  FAIL  a scenario with no steps should be rejected")
    except SpecError as exc:
        check_true("a scenario with no steps is rejected", "steps" in str(exc))


def test_steps_phase(tmp: Path) -> None:
    """A scenario is one ordered list; collect and cleanup sit around it."""
    print("\nThe steps list")

    flat = tmp / "flat.yaml"
    flat.write_text(
        "name: flat\n"
        "steps:\n"
        "  - group: first\n"
        "  - log: {message: do}\n"
        "  - log: {message: check}\n"
        "  - group: second\n"
        "  - fail: {message: boom}\n"
        "cleanup:\n"
        "  - log: {message: bye}\n"
    )
    cls = load(flat)
    check("steps is the only main phase", cls.main_phases, ("steps",))
    check_true("cleanup sits alongside it", hasattr(cls, "cleanup"))

    class FakeLog:
        def info(self, *a): pass
        def banner(self, *a, **k): pass
        warn = error = debug = info

    class FakeCtx:
        log = FakeLog()

        class config:
            dry_run = True

    try:
        cls().steps(FakeCtx())
        FAILURES.append("a failing step should raise")
        print("  FAIL  a failing step should raise")
    except AssertionError as exc:
        message = str(exc)
        check_true("the failure names the step", "'fail'" in message)
        check_true("and its position", "3/3" in message)
        check_true("and the group it was in", "[second]" in message)
        check_true("and keeps the original reason", "boom" in message)

    # The old three-phase form should fail with directions, not confusion.
    legacy = tmp / "legacy.yaml"
    legacy.write_text(
        "name: m\nprepare:\n  - log: {message: a}\ncheck:\n  - log: {message: b}\n"
    )
    try:
        load(legacy)
        FAILURES.append("the old prepare/check form should be rejected")
        print("  FAIL  the old prepare/check form should be rejected")
    except SpecError as exc:
        check_true("the old form is rejected", "no longer a phase" in str(exc))
        check_true("and says what to do instead", "steps" in str(exc))


def test_step_catalogue() -> None:
    print("\nStep catalogue")
    check_true("steps are registered", len(STEPS) > 20)
    undocumented = [n for n, fn in STEPS.items() if not (fn.__doc__ or "").strip()]
    check("every step is documented for --steps", undocumented, [])


def test_freeze_drift_guard() -> None:
    """A drifted cluster must never be registered as a template.

    This is the one piece of framework logic whose failure is SILENT: a
    template frozen after an upgrade looks fine in the registry, and the
    damage only shows up as a later run that passes without testing
    anything. Pinning the decision here is cheaper than discovering that.
    """
    from framework import steps as S

    print("\nFreeze drift guard")

    class _Log:
        def __init__(self):
            self.warns = []

        def info(self, m):
            pass

        def warn(self, m):
            self.warns.append(m)

    class _Ctx:
        """The minimum finish_cluster touches on the refuse-to-freeze path."""

        def __init__(self, born, now):
            self._born, self._now = born, now
            self.log, self.cluster_name = _Log(), "c1"
            self.config = type("C", (), {"dry_run": False})()
            self.deleted = False

        def recall(self, key, default=None):
            return {"smart_mode": "created", "freeze_as": "abcd1234",
                    "freeze_fingerprint": self._born}.get(key, default)

        def remember(self, *_):
            pass

    def decide(born, now):
        ctx = _Ctx(born, now)
        orig_fp, orig_del = S._freeze_fingerprint, S.delete_cluster
        S._freeze_fingerprint = lambda _c: now
        S.delete_cluster = lambda _c: setattr(ctx, "deleted", True)
        try:
            S.finish_cluster.__wrapped__(ctx) if hasattr(
                S.finish_cluster, "__wrapped__") else S.finish_cluster(ctx)
        except Exception:
            pass  # anything past the guard needs a real cluster; irrelevant here
        finally:
            S._freeze_fingerprint, S.delete_cluster = orig_fp, orig_del
        return ctx.deleted

    ga = {"platform": "v2.17.0", "kubelet": ["v1.34.1"]}
    check_true("platform upgrade is refused a freeze",
               decide(ga, {"platform": "v2.18.0", "kubelet": ["v1.34.1"]}))
    check_true("node roll is refused a freeze",
               decide(ga, {"platform": "v2.17.0", "kubelet": ["v1.35.2"]}))
    check("an unchanged cluster still freezes", decide(ga, dict(ga)), False)
    check("status settling from empty is not drift",
          decide({"platform": "", "kubelet": []},
                 {"platform": "v2.18.0", "kubelet": ["v1.34.1"]}), False)
    check("an unreadable probe is not drift",
          decide(ga, {}), False)


def test_registry_pull_diagnosis() -> None:
    """Name a registry failure instead of blaming the cluster.

    Fixtures are the REAL conditions captured from the ga-baseline install
    that failed on 2026-08-29 - the kommander charts come from docker.io and
    hit Docker Hub's anonymous pull limit, while the ghcr.io ones succeeded.
    """
    from framework import steps as S

    print("\nRegistry pull diagnosis")

    class _Kube:
        def __init__(self, items):
            self._items = items

        def json(self, _args):
            return {"items": self._items}

    def ocirepo(name, ready, msg, fetch_failed=False):
        conds = [{"type": "Ready", "status": ready, "message": msg}]
        if fetch_failed:
            conds.append({"type": "FetchFailed", "status": "True", "message": msg})
        return {"metadata": {"name": name}, "status": {"conditions": conds}}

    RATE = ("failed to determine artifact digest: GET "
            "https://index.docker.io/v2/mesosphere/kommander-appmanagement-chart/"
            "manifests/v2.17.0: TOOMANYREQUESTS: You have reached your "
            "unauthenticated pull rate limit.")
    OK = "stored artifact for digest '2.14.0@sha256:18db1c40'"

    def diagnose(items):
        ctx = type("C", (), {})()
        ctx.config = type("K", (), {"dry_run": False})()
        ctx.kube = _Kube(items)
        return S._registry_pull_failures(ctx)

    hits = diagnose([
        ocirepo("dex-2.14.4-chart", "True", OK),
        ocirepo("kommander-appmanagement-0.17.0-chart", "False", RATE, True),
        ocirepo("kommander-0.17.0-chart", "False", RATE, True),
    ])
    check("both docker.io charts are named", len(hits), 2)
    # Compare the SOURCE NAME, not the whole entry: the quoted URL is
    # "index.docker.io", which contains "dex" and makes a naive substring
    # check pass for the wrong reason.
    check("the healthy ghcr chart is not blamed",
          sorted(h.split(":")[0] for h in hits),
          ["kommander-0.17.0-chart", "kommander-appmanagement-0.17.0-chart"])
    check_true("the registry's own reason is quoted",
               all("TOOMANYREQUESTS" in h for h in hits))
    check("one entry per source, not per condition",
          len(hits), len(set(hits)))

    check("an all-healthy cluster reports nothing",
          diagnose([ocirepo("dex-2.14.4-chart", "True", OK)]), [])
    # A release stuck for a NON-registry reason must not be misreported as a
    # registry problem - that would send the developer chasing Docker Hub.
    check("a non-registry failure is not a registry failure",
          diagnose([ocirepo("kommander", "False",
                            "dependency 'kommander/kommander' is not ready")]), [])


def test_stuck_storage_diagnosis() -> None:
    """A stalled install must name the storage that never provisioned.

    Fixture is the REAL ProvisioningFailed event from 2026-08-30, where an
    expired Prism Central credential surfaced as a 100-minute "Waiting for
    all enabled applications to be ready" timeout with no mention of
    credentials or storage anywhere in the failure text.
    """
    from framework import steps as S

    print("\nStuck-storage diagnosis")

    REAL = ('failed to provision volume with StorageClass "nutanix-volume": rpc '
            'error: code = Internal desc = NutanixVolumes: failed to create '
            'volume: pvc-331e2ab8, err: failed to get storage container by '
            'name - name: SelfServiceContainer')

    class _Kube:
        def __init__(self, pvcs, events):
            self._p, self._e = pvcs, events

        def json(self, args):
            return {"items": self._e if args[1] == "events" else self._p}

    def ctx(pvcs, events):
        c = type("C", (), {})()
        c.config = type("X", (), {"dry_run": False})()
        c.kube = _Kube(pvcs, events)
        return c

    pending = {"metadata": {"name": "db-prometheus-0", "namespace": "kommander"},
               "status": {"phase": "Pending"}}
    bound = {"metadata": {"name": "git-volume", "namespace": "git-operator-system"},
             "status": {"phase": "Bound"}}
    events = [{"reason": "ProvisioningFailed", "message": REAL}]

    hits = S._stuck_volume_claims(ctx([pending, bound], events))
    check("only the Pending PVC is reported", len(hits), 1)
    check_true("the provisioner's own words are quoted",
               "storage container by name" in hits[0])
    check_true("the PVC is identified by namespace/name",
               hits[0].startswith("kommander/db-prometheus-0"))
    check("a healthy cluster reports nothing",
          S._stuck_volume_claims(ctx([bound], [])), [])
    # A Pending PVC with no event is still worth reporting - silence there is
    # itself the symptom (no provisioner responded at all).
    quiet = S._stuck_volume_claims(ctx([pending], []))
    check_true("a Pending PVC with no event is still reported",
               len(quiet) == 1 and "no provisioner event" in quiet[0])


def test_kubectl_warning_prefix() -> None:
    """kubectl prefixes JSON with warnings - deterministically, not as a blip.

    Real output captured 2026-08-30 immediately after a node roll: the cluster
    had moved to a kubernetes where cluster.x-k8s.io/v1beta1 Machine is
    deprecated, so EVERY `get machines -o json` came back with a Warning line
    ahead of the document. Parsing that raised a bare
    `Expecting value: line 1 column 1` and killed the run at the post-roll
    identity check.
    """
    from framework.kube import Kube

    print("\nkubectl warning prefix")

    REAL = ("Warning: cluster.x-k8s.io/v1beta1 Machine is deprecated; "
            "use cluster.x-k8s.io/v1 Machine\n"
            '{"apiVersion": "v1", "items": [{"spec": {"providerID": "nutanix://abc"}}]}\n')

    class _Fake(Kube):
        def __init__(self, out):
            self._out = out
            self.log = type("L", (), {"warn": lambda s, m: None})()

        def _run(self, args, **kw):
            return type("R", (), {"stdout": self._out})()

    check("a warning line before the JSON is skipped",
          len(_Fake(REAL).json(["get", "machines", "-A"]).get("items", [])), 1)
    check("clean JSON is unaffected",
          len(_Fake('{"items":[{},{}]}').json(["get", "x"]).get("items", [])), 2)
    check("empty output is still empty", _Fake("").json(["get", "x"]), {})
    check("genuinely broken output degrades to empty, not a crash",
          _Fake("The connection to the server was refused").json(["get", "x"]), {})
    # A JSON array response must survive too - some kubectl paths return one.
    check("a top-level array is parsed",
          _Fake("Warning: x\n[1,2,3]").json(["get", "x"]), [1, 2, 3])


def test_no_undefined_ctx() -> None:
    """A module-level helper must RECEIVE ctx, never read it as a global.

    _preseed_entries(comps) called _build_and_push_image(ctx, ...) with no ctx
    parameter and no module-level ctx, so `create_cluster: {changes: [kommander@x]}`
    died with NameError before a single VM existed - the whole create-path
    image route was dead and no scenario exercised it. Live-caught 2026-09-01
    by an audit, not by a run.

    Scope-aware on purpose: the many nested poll helpers legitimately close
    over their parent's ctx, which symtable reports as free, not global.
    """
    import symtable

    def scan(src: str, filename: str) -> list[str]:
        top = symtable.symtable(src, filename, "exec")
        if "ctx" in [s.get_name() for s in top.get_symbols()]:
            return []
        out, stack = [], [top]
        while stack:
            sc = stack.pop()
            stack.extend(sc.get_children())
            if sc.get_type() != "function":
                continue
            for sym in sc.get_symbols():
                if sym.get_name() == "ctx" and sym.is_global() and sym.is_referenced():
                    out.append(f"{filename}:{sc.get_lineno()} {sc.get_name()}()")
        return out

    root = Path(__file__).resolve().parent / "framework"
    offenders: list[str] = []
    for f in sorted(root.glob("*.py")):
        offenders += scan(f.read_text(encoding="utf-8"), f.name)
    check("no module-level helper reads ctx as a global", offenders, [])
    # the check must actually be able to fail
    reintroduced = scan("def helper(comps):\n    return build(ctx, comps)\n", "x.py")
    check("and the check catches it if reintroduced", len(reintroduced), 1)


def main() -> int:
    import tempfile

    root = Path(__file__).resolve().parent
    test_helmrelease_readiness()
    test_durations()
    test_step_catalogue()
    test_freeze_drift_guard()
    test_registry_pull_diagnosis()
    test_stuck_storage_diagnosis()
    test_kubectl_warning_prefix()
    test_no_undefined_ctx()
    test_scenarios_load(root / "scenarios")
    with tempfile.TemporaryDirectory() as tmp:
        test_bad_specs(Path(tmp))
        test_steps_phase(Path(tmp))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    check_scenario_hygiene()
    print("All framework self-tests passed.")
    return 0


def check_scenario_hygiene():
    """Scenario files must carry no line that repeats a step's own default.

    `kubernetes_version: ""` and `timeout: 90m` on create_cluster read as
    decisions; they are the defaults. Noise like that is what made a reviewer
    ask why the version was blank - the answer being that it was never set at
    all. Enforced rather than advised, because it only ever grows.
    """
    import inspect
    import yaml
    from pathlib import Path
    from framework import steps as _steps
    from framework.spec import _normalise

    # Topology stays explicit even when it equals the default: the change-set
    # hash covers it, so leaning on a default would let a change to that
    # default silently invalidate every frozen template.
    KEEP_EXPLICIT = {"create_cluster": {"control_plane", "workers"},
                     "create_workload_cluster": {"control_plane", "workers"}}
    noise = []
    for f in sorted(Path("scenarios").glob("*.yaml")):
        doc = yaml.safe_load(f.read_text())
        for phase in ("steps", "collect", "cleanup"):
            for entry in doc.get(phase) or []:
                if isinstance(entry, dict) and set(entry) == {"group"}:
                    continue
                name, params = _normalise(entry, phase)
                fn = _steps.STEPS.get(name)
                if not fn or not params:
                    continue
                sig = inspect.signature(fn)
                for k, v in params.items():
                    if k in KEEP_EXPLICIT.get(name, ()):
                        continue
                    par = sig.parameters.get(k)
                    if par is not None and par.default is not inspect.Parameter.empty \
                            and par.default == v:
                        noise.append(f"{f.name}: {name}.{k}={v!r} is the default")
    if noise:
        raise AssertionError(
            "scenario options that repeat the step default:\n  " + "\n  ".join(noise))
    print("  pass  no scenario repeats a step default")


if __name__ == "__main__":
    sys.exit(main())
