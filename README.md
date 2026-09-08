# Dev E2E scenarios

Run NKP end-to-end scenarios against the dev Prism Central, from your laptop or
from a dev VM. Each scenario creates real clusters, asserts against them,
collects diagnostics when something goes wrong, and cleans up after itself.

```bash
export NUTANIX_USER=<pc-username>          # ./tam login pc-dev prints both
export NUTANIX_PASSWORD=<pc-password>

./run_e2e.py --list           # what is available
./run_e2e.py day1-install     # run one
./run_e2e.py --all            # run everything
./run_e2e.py --all --dry-run  # print the plan, touch nothing
```

The `nkp` binary is taken from the current directory (`./nkp`); override with
`NKP_BIN=/path/to/nkp`.

## Scenarios

All six scenarios from the NKP Dev Testing Infrastructure doc, as config-driven
YAML in `scenarios/`:

| Scenario | Validates | Topology |
|---|---|---|
| `day1-install` | minimal install succeeds, core healthy | 1 CP + 2 workers |
| `day2-operations` | app deploy (as the UI does), cordon/drain, restart, recovery | 1 CP + 3 workers, platform |
| `cluster-lifecycle` | create, reconcile, scale out/in, delete | 1 CP + 2 workers |
| `platform-upgrade` | platform upgrade + post-upgrade health | 1 CP + 3 workers (needs `E2E_BASELINE_NKP_BIN`) |
| `app-upgrade` | app upgrade + rollback restores it | 1 CP + 3 workers (needs `E2E_APP`, `E2E_APP_TO_VERSION`) |
| `single-node` | single-node feasibility (experimental) | 1 node |

`create_cluster` takes `type: management` (self-managed, default) or
`type: workload` (created against the cluster you are attached to - how test
clusters hang off a fast-claimed management cluster).


## How a scenario runs

A scenario is **one ordered list of steps** — setup, actions and assertions in
the order they happen. Two phases sit around that list, and the runner
guarantees both:

```text
steps  ->  collect (on failure)  ->  cleanup (always)
```

- **collect** runs automatically when a step fails — node and pod state, events,
  describes, pod logs and the Prism Central VM list, without asking.
- **cleanup** runs *even when the scenario failed*. Clusters live on a shared
  Prism Element that is usually above 90% full, so a harness that strands VMs on
  failure would be worse than no harness. `--keep` opts out when you want to
  inspect a live failure, and the runner prints how to reach it.
## Layout

```text
dev-e2e/
  run_e2e.py        the runner
  scenarios/        what you write - one YAML file per scenario
  framework/        the engine: steps, phases, CLI/kubectl/Prism wrappers
```

You almost never need to open `framework/`. Adding a scenario means adding a
file to `scenarios/`.

## Writing a scenario

Drop a `.yaml` file in `scenarios/`. It is discovered automatically - there is
no registry to edit.

```yaml
name: day2-drain-reconcile
description: Drain a worker, wait, then verify the cluster reconciles
topology: 1 control plane + 1 worker
expected_minutes: 50

steps:
  - group: build the cluster
  - require_prism_central
  - create_cluster: {control_plane: 1, workers: 1}
  - wait_nodes_ready: {count: 2, timeout: 25m}

  - group: drain the worker, then let it settle
  - drain_node: {role: worker}
  - wait: {duration: 2m, reason: "allow rescheduling to settle"}
  - uncordon_node: {role: worker}

  - group: did it actually reconcile?
  - wait_reconciled: {timeout: 20m}
  - assert_node_count: {count: 2}
  - wait_pods_healthy: {namespace: kube-system}

cleanup:
  - delete_cluster
  - assert_no_leftover_vms
```

Copy `scenarios/example-pass.yaml` to start. Then:

```bash
./run_e2e.py --steps              # every step you can use
./run_e2e.py my-scenario --dry-run   # prints the plan, touches nothing
```

The step catalogue covers cluster lifecycle (`create_cluster`, `delete_cluster`,
`install_platform`, `use_existing_cluster`), waiting (`wait`, `wait_nodes_ready`,
`wait_pods_healthy`, `wait_reconciled`), day-2 operations (`cordon_node`,
`drain_node`, `uncordon_node`, `scale_workers`, `restart_workload`), apps and
Helm (`deploy_app`, `wait_app_deployed`, `assert_helmrelease_ready`,
`assert_all_helmreleases_ready`, `assert_appdeployments_ready`, `delete_app`),
upgrades (`upgrade_platform`, `upgrade_catalog_app`, `record_*`), assertions
(`assert_node_count`, `assert_pods_healthy`, `assert_api_reachable`,
`assert_no_leftover_vms`), `collect_diagnostics`, and `run` as an escape hatch
for a raw `kubectl`/`nkp` command.

### Checking that an app is really up

"Deployed" has three separate meanings and they fail differently, so the steps
keep them apart:

| Step | What it proves | What it would miss on its own |
|---|---|---|
| `wait_app_deployed` | app-management reconciled the AppDeployment **and** its HelmRelease is Ready | — |
| `assert_helmrelease_ready` | that release is `Ready` **and** `Released` | Helm reports `Ready` on a release it never installed |
| `assert_app_workload_running` | the pods it installed are actually running | a Ready release whose workload is crash-looping |

`assert_appdeployments_ready` applies the same check to every AppDeployment on
the cluster - the platform-wide "did anything land broken?" gate. When a release
is not up, the failure carries the Helm reason and message, not just a name:

```text
2 of 14 AppDeployment(s) not up:
  kommander-default-workspace/nginx: Ready=False (InstallFailed) - Helm install failed: timed out
  kommander/traefik: Released=False (UpgradeFailed) - retries exhausted
```

`use_existing_cluster` (with `E2E_KUBECONFIG`) points a scenario at a cluster
that already exists, so an app-level check runs in about a minute instead of
paying a 40-minute cluster build. Nothing it did not create is ever deleted.

Durations accept `30s`, `5m`, `1h`. A mistyped step or argument produces a
config error naming the file and suggesting the right name, not a stack trace.

### Order is the test

Work and assertions live in the same list, because that is how a test reads: do
a thing, check it, do the next thing, check that.

```yaml
steps:
  - group: create
  - create_cluster: {control_plane: 1, workers: 1}
  - wait_nodes_ready: {count: 2}
  - assert_node_count: {count: 1, role: worker}

  - group: scale out 1 -> 2
  - scale_workers: 2
  - wait_nodes_ready: {count: 3}
  - assert_node_count: {count: 2, role: worker}

  - group: scale in 2 -> 1
  - scale_workers: 1
  - assert_node_count: {count: 1, role: worker}

cleanup:
  - delete_cluster
```

`group:` is only a label: it groups the log output and names the failing step in
the report.

```text
AssertionError: step 8/11 'assert_node_count' [scale out 1 -> 2]: expected 2 workers, found 1
```

### Config overrides, the way the UI does them

```yaml
- deploy_app:
    app: logging-operator
    version: 6.4.0
    namespace: kommander
    config_values: |
      resources:
        requests:
          memory: 192Mi
- assert_app_config_applied            # override CM wired into the HelmRelease
- assert_jsonpath:                     # ...and it reached the workload
    kind: deployment
    name: logging-operator
    namespace: kommander
    path: "{.spec.template.spec.containers[0].resources.requests.memory}"
    equals: "192Mi"
```

`config_values` becomes an override ConfigMap referenced by
`spec.configOverrides` - byte-for-byte the UI flow. The two assertions prove
different things: the platform wired the override into helm, and the value
actually landed on the running workload. `assert_jsonpath` is generic - use it
whenever "did my config take effect" is the question.

### Upgrades: your change arrives as the new NKP

```bash
E2E_BASE_NKP_VERSION=v2.17.0 ./run_e2e.py platform-upgrade   # baseline fetched for you
# or: E2E_BASELINE_NKP_BIN=/path/to/old/nkp
```

`resolve_baseline` downloads the baseline version's CLIs from GitHub releases
and shims them; the baseline platform is installed with that, and the upgrade
runs with **your** `NKP_BIN` - so a change that only triggers during upgrade is
exactly what gets exercised.

### When it fails, when it waits, when you want to look

Three behaviours every scenario gets for free:

- **Support bundle on failure.** `collect_diagnostics` writes the state dumps
  (nodes, pods, events, CAPI, HelmReleases, Prism Central VMs) *and* runs
  `nkp diagnose` - the same support bundle support would ask a customer for -
  under `diagnostics-failure/`, before cleanup deletes the cluster.
- **Every assert waits.** All `assert_*` steps take `timeout:` (default 2m for
  point-in-time checks) and retry until it - a condition that is about to
  become true should not fail the run because the check arrived early.
- **`pause` hands you the cluster.**

  ```yaml
  - pause: {message: "inspect the drain result", timeout: 2h}
  ```

  Execution stops, the kubeconfig and an inspect command are printed, and
  nothing touches the cluster until you resume: **Enter** in an interactive
  terminal, or `touch <artifacts>/resume` for background runs - the exact
  path is printed. An unresumed pause fails at its timeout so cleanup still
  runs.

### Operate, wait, then check reconciliation

That pattern is ordinary here: the operation, then `wait`, then
`wait_reconciled`. Reconciliation is judged on the
CAPI Cluster's `Available` / `TopologyReconciled` conditions, because pods can
look healthy while the topology controller has quietly stopped reconciling.

### Parameters

Scenarios take values from the environment, so one file covers many cases:

```yaml
requires_env:
  - E2E_APP                              # fails before anything is built
steps:
  - upgrade_catalog_app:
      app: "${E2E_APP}"
      to_version: "${E2E_APP_TO_VERSION:-latest}"
```

## Testing the framework itself

```bash
./selftest.py     # about a second, no cluster, no credentials
```

Most of the framework is glue around `kubectl` and the `nkp` CLI, which only a
real run exercises. The pure decision logic is different: a harness that reports
"all good" incorrectly is worse than no harness, so the HelmRelease readiness
rules, duration parsing and every scenario file are pinned by the self-test.

### When YAML is not enough

A `.py` file in `scenarios/` subclassing `Scenario` also works, for the rare
case that needs real logic. Prefer YAML - it is what the rest of the team reads.
Better still, add a step to `framework/steps.py`: one decorated function makes
it available to every scenario, and it shows up in `--steps`.

## Adding a scenario — Python

For anything needing real logic, a Python scenario still works and is discovered the same way:

```python
from ..framework import Context, Scenario

class MyScenario(Scenario):
    name = "my-scenario"                       # how you select it on the CLI
    description = "What this proves"
    topology = "1 control plane + 1 worker"
    expected_minutes = 30

    def steps(self, ctx): ...                  # the test, in order
    def collect(self, ctx): ...                # extra diagnostics (optional)
    def cleanup(self, ctx): ...                # tear down; must be idempotent
```

`ctx` gives you everything: `ctx.nkp` (CLI), `ctx.kube` (kubectl), `ctx.pc`
(Prism Central API), `ctx.log`, `ctx.artifacts`, and `ctx.cluster()` for a
unique cluster name. Use them rather than shelling out directly, so `--dry-run`
keeps working and secrets stay out of the logs.

## Results

```text
e2e-results/<timestamp>/
├── RESULTS.md                  # pass/fail table — attach to a PR
├── junit-e2e.xml               # for CI consumption
├── run.log
└── <scenario>/
    ├── scenario.log
    ├── <cluster>.conf          # kubeconfig
    └── diagnostics-failure/    # nodes, pods, events, describes, pod logs, PC VMs
```

## Building the NKP binary fast

```bash
NKP_BIN=$(./build-nkp-fast.sh -q) ./run_e2e.py day2-operations
```

Three cache layers: a **binary cache** keyed on HEAD *plus a hash of your
uncommitted diff* (identical code never builds twice - a repeat is ~1s); the
persistent **Go build cache** (an incremental change recompiles only what it
touched); and goreleaser **single-target** (this machine's platform only, not
the 6-target release matrix). The orchestrator's `cli-binary` vector uses the
same flags and its own sha-keyed cache.

## Sizing: how many workers

Node counts come from what the components actually require, checked in code
rather than assumed:

| Component | Constraint | Implies |
|---|---|---|
| `cilium-operator` | `replicas: 2`, required anti-affinity on `kubernetes.io/hostname` — but it **tolerates the control-plane taint**. Verified 2026-09-02: this is the UPSTREAM cilium chart default, not an NKP setting — CAREN's `values-template.yaml` never sets `operator.replicas`, and `CNI.AddonConfig.Values.SourceRef` takes per-cluster helm values, so it can be set to 1 | 2 *nodes* by default, so 1 CP + 1 worker satisfies it without any override |
| `rook-ceph` (any platform install) | `mon.count: 3`, `mgr.count: 2`, both `allowMultiplePerNode: false`; mons do not run on the control plane | **3 workers** |

So scenarios that install the platform use 3 workers; plain cluster scenarios
use 2, because draining the only worker is not a meaningful drain. `single-node`
stays at 0 workers by design, and its second `cilium-operator` replica is
expected to stay Pending — `report_unschedulable_pods` records that rather than
failing.

## Configuration

```bash
export NUTANIX_USER=... NUTANIX_PASSWORD=...        # tam login pc-dev
export NKP_BIN=./nkp
export E2E_MACHINE_IMAGE=nkp-rocky-9.6-release-cis-1.33.2-20251110200434.qcow2
export E2E_SSH_PUBLIC_KEY=~/Documents/nkp/nkp_cluster.pub
```


| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NUTANIX_USER` / `NUTANIX_PASSWORD` | **yes** | — | Prism Central credentials (`tam login pc-dev`) |
| `NKP_BIN` | no | `./nkp` | Path to the CLI under test |
| `PC_URL` | no | `https://<your-prism-central>:9440` | Prism Central endpoint |
| `NUTANIX_PRISM_ELEMENT_CLUSTER_NAME` | no | `<prism-element-cluster>` | PE to place VMs on |
| `NUTANIX_SUBNET_NAME` | no | `<subnet>` | Subnet |
| `NUTANIX_STORAGE_CONTAINER_NAME` | no | `SelfServiceContainer` | Storage container |
| `E2E_KUBERNETES_VERSION` | no | `v1.33.2` | Cluster Kubernetes version |
| `E2E_MACHINE_IMAGE` | **yes** | — | VM image on this PC; must match `E2E_KUBERNETES_VERSION`. Images are rotated off regularly, so last month's may be gone |
| `E2E_SSH_PUBLIC_KEY` | no | `~/.ssh/id_ed25519.pub` | Key placed on nodes — without one you cannot debug a broken cluster |
| `E2E_SSH_USERNAME` | no | `konvoy` | Login user created on the nodes |
| `E2E_CONTROL_PLANE_MEMORY` | no | CLI default | Control-plane memory in GiB |
| `E2E_REGISTRY_MIRROR_URL` | no | — | Registry mirror, to avoid Docker Hub rate limits |
| `E2E_REGISTRY_MIRROR_USERNAME` / `_PASSWORD` | with a mirror | — | A mirror without credentials wedges CAREN mid-reconcile. The password is redacted wherever commands are echoed |
| `E2E_KUBECONFIG` | `platform-apps-healthy` | — | Run against an existing cluster; nothing is created or deleted |
| `E2E_CLUSTER_PREFIX` | no | `e2e-<user>` | Cluster/VM name prefix, also what cleanup sweeps |
| `E2E_TIMEOUT_MINUTES` | no | `60` | Per-cluster-operation timeout |
| `E2E_BASELINE_VERSION` / `E2E_TARGET_VERSION` | upgrade scenarios | — | Platform versions to upgrade between |
| `E2E_APP`, `E2E_APP_VERSION`, `E2E_APP_TO_VERSION` | app scenarios | `kubernetes-dashboard` | App under test |

## Notes on the shared dev PC

- Scenarios create real VMs on a **shared** Prism Element that regularly runs
  above 90% memory. A create can fail with `RESOURCE_SHORTAGE` when someone
  else is mid-run; that is capacity, not your change.
- Cleanup deletes the cluster through `nkp delete cluster` and then sweeps any
  VM matching the cluster prefix, because a half-failed delete otherwise leaves
  VMs holding memory for days.
- Credentials rotate every few days; a `401` means re-run `tam login pc-dev`.

