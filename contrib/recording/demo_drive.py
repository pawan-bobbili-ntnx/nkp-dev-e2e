#!/usr/bin/env python3
"""Drive the live demo by hand, against a cluster that is already running.

Why this exists rather than `./run_e2e.py demo-live`:

  * The cluster has to be REAL and has to STAY UP between bits, because the
    demo shows the NKP dashboard at every pause. A scenario run owns its
    cluster for the length of that run; this drives one that outlives it, so
    the recording can be taken in bits with the dashboard live throughout.
  * A recording wants a clean terminal. The framework streams every
    subprocess line for diagnostics - 194k of the last full run's 202k lines.
    Here that stream is tee'd to the log file and kept off the screen.

Nothing here is simulated. Every command is the real one, run against the real
cluster, and the delivery path is the framework's own proven machinery
(`STEPS[...]`) - a recording is a bad reason to re-implement it.

The cluster comes from `./run_e2e.py demo-live-prep --keep`, which is the
unrecorded half: build the cluster, get istio-helm running with an override.

  ./demo_drive.py                  # all three bits, pausing between them
  ./demo_drive.py --bit deliver    # just one bit
  ./demo_drive.py --list
  ./demo_drive.py --dashboard      # print the URL and credentials, nothing else
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]          # repo root: framework/ and e2e-results/ live there
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))    # demo_console sits beside this file

from demo_console import Console  # noqa: E402
from framework.config import Config, ConfigError, load_env_file  # noqa: E402
from framework.core import Context  # noqa: E402
from framework.logging_ import Log  # noqa: E402
from framework.nkp import Nkp  # noqa: E402
from framework.pc import PrismCentral  # noqa: E402
from framework.steps import STEPS  # noqa: E402

BRANCH = "pawan/e2e-demo"
GATEWAY_NS = "istio-helm-gateway-ns"

#: Where narration goes. Set once in main().
SINK: "Sink | None" = None


class Sink:
    """One narration format, two destinations.

    The driver only ever emits demo_console directives. Either a local Console
    renders them here (running the demo straight), or they are appended to the
    control file that `demo_console.py` is following in the terminal being
    recorded. The rendering code exists once, in demo_console.Console, so the
    screen looks identical either way.
    """

    def __init__(self, control: Path | None, scenario: str, pace: float):
        self.control = control
        self.console = None if control else Console(scenario, pace)
        self.pace = pace
        self.resume = control.with_suffix(".resume") if control else None

    def _resumed(self) -> int:
        try:
            return int(self.resume.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def wait_for_resume(self) -> None:
        """Block until the console's Enter is pressed, in console mode.

        The pause happens on someone else's terminal, so it is not this
        process's stdin that releases it. Without this the driver would run the
        next bit while the screen still read AUTOMATION STOPPED.
        """
        if self.resume is None:
            return                          # local mode: Console.handle blocked
        before = self._resumed()
        while self._resumed() <= before:
            time.sleep(0.25)

    def emit(self, line: str = "") -> None:
        if self.control is not None:
            with self.control.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        if not line:
            self.console.plain("")
            return
        self.console.handle(line)
        time.sleep(self.pace)


def emit(line: str = "") -> None:
    SINK.emit(line)


def heading(text: str) -> None:
    emit()
    emit("@step " + text)


class DemoLog(Log):
    """The framework's logger, speaking demo_console directives.

    Two jobs. First, `shell.py:140` echoes every line a command prints as
    "  | ..." - right for a diagnostic artifact, wrong for a recording - so
    those go to the log file only. Second, everything the framework does choose
    to say is re-emitted as a directive, so it lands on the same screen, in the
    same style, as the driver's own narration.
    """

    def _tee(self, line: str) -> None:
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _relay(self, directive: str, line: str) -> None:
        self._tee(line)
        emit(f"{directive} {line}".strip())

    def banner(self, line: str, level: int = 1) -> None:
        self._relay("@banner" if level == 1 else "@step", line)

    def info(self, line: str) -> None:
        if line.lstrip().startswith("| "):
            self._tee(line)
            return
        self._relay("", line)

    def warn(self, line: str) -> None:
        self._relay("@warn", line)

    def error(self, line: str) -> None:
        self._relay("@error", line)

    def debug(self, line: str) -> None:
        self._tee(line)

    def child(self, prefix: str, path=None) -> "DemoLog":
        return DemoLog(prefix=prefix, path=path, verbose=self.verbose)


def find_kubeconfig(explicit: str = "") -> Path:
    """The cluster demo-live-prep left behind, newest first."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            sys.exit(f"no such kubeconfig: {p}")
        return p
    found = sorted(
        (ROOT / "e2e-results").glob("*/demo-live-prep/*.conf"),
        key=lambda p: p.stat().st_mtime,
    )
    if not found:
        sys.exit(
            "no cluster found. Build one first:\n"
            "    ./run_e2e.py demo-live-prep --keep"
        )
    return found[-1]


def make_ctx(kubeconfig: Path, verbose: bool) -> Context:
    load_env_file()
    try:
        config = Config.from_env(keep=True)
    except ConfigError as exc:
        sys.exit(f"ERROR: {exc}")
    artifacts = kubeconfig.parent
    log = DemoLog(prefix="demo", path=artifacts / "demo_drive.log", verbose=verbose)
    ctx = Context(
        config=config,
        log=log,
        artifacts=artifacts,
        nkp=Nkp(config, log),
        pc=PrismCentral(config, log),
        scenario_slug="demo-live",
    )
    ctx.kubeconfig = kubeconfig
    ctx.remember("smart_mode", "existing")
    # The delivery path reads the cluster name from CAPI when it needs it; the
    # claim path names the kubeconfig after the cluster, so this is free.
    ctx.cluster_name = kubeconfig.stem.replace("-claim", "")
    load_state(ctx)
    return ctx


def state_path(ctx) -> Path:
    return ctx.artifacts / ".demo-state.json"


def load_state(ctx) -> None:
    """Carry what a step remembered into the NEXT invocation of this script.

    Each bit is meant to be re-runnable on its own during a recording, and each
    run is a fresh process. Two assertions in `verify` read things `deliver`
    remembered - the change-set's resolved commits, and the chart references
    from before it landed - so without this, `--bit verify` on its own fails on
    state that a single scenario run would have held in memory.
    """
    try:
        ctx._notes.update(json.loads(state_path(ctx).read_text()))
    except (OSError, ValueError):
        pass


def save_state(ctx) -> None:
    keep = {}
    for k, v in ctx._notes.items():
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue            # kubeconfigs, Paths - rebuilt on load anyway
        keep[k] = v
    try:
        state_path(ctx).write_text(json.dumps(keep, indent=2, sort_keys=True))
    except OSError:
        pass


def kubectl(ctx, *args: str) -> str:
    """A read-only kubectl whose output this script places itself.

    `quiet=True` keeps shell.py from echoing the result as it streams; these
    calls exist to be printed deliberately, under a heading, not twice.
    """
    return ctx.kube._run(list(args), check=False, quiet=True).stdout.strip()


def dashboard(ctx) -> tuple[str, str, str]:
    """The URL and the admin credentials, read from the cluster."""
    ip = kubectl(
        ctx, "get", "svc", "kommander-traefik", "-n", "kommander",
        "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}",
    )
    user = kubectl(
        ctx, "get", "secret", "dkp-credentials", "-n", "kommander",
        "-o", "go-template={{.data.username|base64decode}}",
    )
    pw = kubectl(
        ctx, "get", "secret", "dkp-credentials", "-n", "kommander",
        "-o", "go-template={{.data.password|base64decode}}",
    )
    url = f"https://{ip}/dkp/kommander/dashboard" if ip else ""
    return url, user, pw


def emit_block(text: str) -> None:
    """A command's own output, one console line per line."""
    for line in (text or "").splitlines():
        emit("@raw   " + line)


def show_dashboard(ctx) -> None:
    url, user, pw = dashboard(ctx)
    if not url:
        emit("@warn dashboard service has no load-balancer address yet")
        return
    emit(f"  dashboard: {url}")
    if user:
        emit(f"  username:  {user}")
    if pw:
        emit(f"  password:  {pw}")


def pause(message: str, enabled: bool) -> None:
    if not enabled:
        return
    emit("@pause " + message)
    SINK.wait_for_resume()


# ─────────────────────────────────────────────────────────────── the three bits


def bit_show(ctx, args) -> None:
    """Bit 1 - this is a real 2.18 cluster, and istio-helm is running on it."""
    heading("A real NKP 2.18 cluster")
    emit("@cmd kubectl get nodes")
    emit_block(kubectl(
        ctx, "get", "nodes",
        "-o", "custom-columns=NAME:.metadata.name,"
              "STATUS:.status.conditions[-1].type,"
              "ROLE:.metadata.labels['node-role\\.kubernetes\\.io/control-plane'],"
              "VERSION:.status.nodeInfo.kubeletVersion"))

    heading("istio-helm, enabled with a config override")
    emit("@cmd kubectl get appdeployment istio-helm -n kommander-workspace")
    emit_block(kubectl(
        ctx, "get", "appdeployment", "istio-helm", "-n", "kommander-workspace",
        "-o", "custom-columns=NAME:.metadata.name,"
              "APPREF:.spec.appRef.name,"
              "OVERRIDES:.spec.configOverrides[*].name"))
    emit()
    emit(f"@cmd kubectl get hpa istio-helm-ingressgateway -n {GATEWAY_NS}")
    emit_block(kubectl(
        ctx, "get", "hpa", "istio-helm-ingressgateway", "-n", GATEWAY_NS,
        "-o", "custom-columns=NAME:.metadata.name,"
              "MIN:.spec.minReplicas,MAX:.spec.maxReplicas,"
              "REPLICAS:.status.currentReplicas"))
    emit("@dim   minReplicas is 1 because the override said so; the chart default is 2")

    heading("The dashboard")
    show_dashboard(ctx)
    # The baseline assertion 3 measures against. Taken here so it reflects the
    # cluster BEFORE anything is delivered; `bit_deliver` re-takes it only if a
    # bit was skipped. `upgrade_kommander` does this for itself, but
    # `deliver_changes` does not.
    STEPS["record_chart_refs"](ctx)


def bit_deliver(ctx, args) -> None:
    """Bit 2 - one change-set, three repos, onto the cluster that is running."""
    heading("One change-set, from three repositories")
    for repo in ("kommander", "kommander-applications", "charts"):
        emit(f"  {repo:<24} @ {args.branch}")
    emit("@dim   each is resolved to a commit, built, and pushed to the dev registry")
    emit("@dim   under a tag that IS that commit - then the cluster is pointed at it")
    emit()
    if not ctx.recall("chart_refs_before"):
        STEPS["record_chart_refs"](ctx)

    started = time.monotonic()
    STEPS["deliver_changes"](ctx, changes=[
        f"kommander@{args.branch}",
        f"kommander-applications@{args.branch}",
        f"charts@{args.branch}",
    ])
    emit()
    emit(f"  change-set delivered in {(time.monotonic() - started) / 60:.1f} min")


def bit_verify(ctx, args) -> None:
    """Bit 3 - every one of the three changes is visible on the cluster."""
    if not ctx.recall("changeset_components"):
        sys.exit("nothing has been delivered to this cluster yet - run "
                 "`./demo_drive.py --bit deliver` first")
    heading("1/4  the kommander image running is the one we built")
    STEPS["assert_change_running"](ctx, repo="kommander")

    heading("2/4  that image's own log line is in the controller")
    STEPS["assert_controller_log"](
        ctx, deployment="kommander-appmanagement", contains="e2e-demo marker")

    heading("3/4  the k-apps change repointed the chart references")
    STEPS["assert_chart_refs_changed"](ctx, app="istio-helm")

    heading("4/4  the chart change is on the running gateway workload")
    STEPS["assert_jsonpath"](
        ctx, kind="deployment", name="istio-helm-ingressgateway",
        namespace=GATEWAY_NS,
        path="{.metadata.annotations.e2e-demo-chart-source}",
        equals=args.branch,
    )
    emit()
    emit("@dim   three repositories, one delivery, every change provably live on a")
    emit("@dim   real cluster - and not one line of it has merged")


BITS = {
    "show": ("the cluster is real, istio-helm is running with an override", bit_show),
    "deliver": ("deliver a three-repo change-set onto it", bit_deliver),
    "verify": ("prove all three changes are live", bit_verify),
}
PAUSE_AFTER = {
    "show": "open the NKP dashboard - show istio-helm enabled",
    "deliver": "back to the dashboard - the apps are reconciling the new charts",
    "verify": "dashboard once more - this is the change, live, pre-merge",
}


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bit", action="append", choices=list(BITS),
                   help="run only this bit (repeatable; default: all three)")
    p.add_argument("--list", action="store_true", help="list the bits")
    p.add_argument("--dashboard", action="store_true",
                   help="print the dashboard URL and credentials, then exit")
    p.add_argument("--kubeconfig", default="",
                   help="cluster to drive (default: newest demo-live-prep cluster)")
    p.add_argument("--branch", default=BRANCH, help=f"change-set branch ({BRANCH})")
    p.add_argument("--no-pause", action="store_true", help="do not wait between bits")
    p.add_argument("--console", nargs="?", const=str(HERE / ".demo-console"),
                   default=None, metavar="CONTROL",
                   help="write the narration to a demo_console.py control file "
                        "instead of this terminal (default: ./.demo-console)")
    p.add_argument("--scenario", default="demo-full-loop",
                   help="the [tag] each line carries (default: demo-full-loop)")
    p.add_argument("--pace", type=float, default=0.28,
                   help="seconds between lines when rendering locally")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="tee the raw subprocess output to the log file")
    args = p.parse_args()

    global SINK
    control = Path(args.console) if args.console else None
    SINK = Sink(control, args.scenario, args.pace)

    if args.list:
        for name, (desc, _) in BITS.items():
            emit(f"@raw   {name:<9} {desc}")
        return 0

    kubeconfig = find_kubeconfig(args.kubeconfig)
    ctx = make_ctx(kubeconfig, args.verbose)

    if args.dashboard:
        show_dashboard(ctx)
        return 0

    if control:
        print(f"narrating into {control}\n"
              f"  in the terminal you are recording:  ./demo_console.py"
              f"{'' if control == HERE / '.demo-console' else f' --control {control}'}")

    emit()
    emit(f"@dim   cluster:    {ctx.cluster_name}")
    emit(f"@dim   kubeconfig: {kubeconfig}")

    chosen = args.bit or list(BITS)
    for name in chosen:
        BITS[name][1](ctx, args)
        save_state(ctx)
        pause(PAUSE_AFTER[name], not args.no_pause)
    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
