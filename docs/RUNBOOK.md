# dev-e2e runbook

`dev-e2e` runs NKP scenarios on real clusters in a Nutanix Prism Central lab.
A scenario is a YAML list of steps: get a cluster, put a change on it, assert
what must be true, tear it down.

Commands run from the repository root. Timings are from the shared dev Prism
Central.

1. [Setup](#1-setup)
2. [First run](#2-first-run)
3. [How a run works](#3-how-a-run-works)
4. [Writing a scenario](#4-writing-a-scenario)
5. [Delivering your change](#5-delivering-your-change)
6. [Clusters](#6-clusters)
7. [Upgrades](#7-upgrades)
8. [The scenarios](#8-the-scenarios)
9. [Steps](#9-steps)
10. [Configuration](#10-configuration)
11. [When it fails](#11-when-it-fails)
12. [Rules, and where to read more](#12-rules-and-where-to-read-more)

---

## 1. Setup

```bash
git clone https://github.com/pawan-bobbili-ntnx/nkp-dev-e2e.git && cd nkp-dev-e2e
make bootstrap                       # PyYAML; the pinned etcd binaries the claim path copies to nodes
cp nkp-e2e.env.example nkp-e2e.env   # then fill it in; chmod 600
make creds                           # 5 s: Prism Central accepts the credentials
make selftest                        # 1 s: the framework is intact
```

You need: Python 3.11+, `kubectl`, `gh` (logged in and SAML-authorised for
`mesosphere`, `nutanix-cloud-native`, `nutanix-core`), the `nkp` CLI to test,
and network access to the lab (VPN or office LAN).

`nkp-e2e.env` holds everything environment-specific (section 10 lists it).
The values for the shared dev Prism Central are in the team's internal notes,
not in this repository; the file is gitignored and secrets are redacted from
every logged command. Prism Central credentials come from `tam login pc-dev`
and expire after a few days.

---

## 2. First run

```bash
./run_e2e.py --list                    # every scenario, with measured duration
./run_e2e.py example-pass --dry-run    # the plan, every variable resolved; touches nothing
./run_e2e.py example-pass              # build a cluster, check it, delete it
```

The result is `e2e-results/<timestamp>/RESULTS.md`: one row per scenario,
verdict, duration, and the failure detail if any, with `scenario.log` beside
it. Those two files are what you attach to a pull request.

The first run of a shape creates a cluster with `nkp create cluster` (~36 min)
and, when the run ends, freezes it as a template. Later runs of the same shape
claim a clone of that template (~8 min). The log says which happened.

### Terms

- **Prism Central (PC)**: the Nutanix control plane the lab VMs live in; **Prism Element (PE)** is one cluster of hosts inside it.
- **konvoy2**: the repository behind the `nkp` CLI, which creates and upgrades clusters through **CAPI** (cluster-api) with the Nutanix provider **CAPX** and the runtime-extension **CAREN**.
- **kommander**: the platform installed on a management cluster: dashboard, app catalog, federation to attached **workload clusters**. Its controller is built as the image `kommander2-appmanagement` and runs as the deployment `kommander-appmanagement` in namespace `kommander`.
- **applications tree / k-apps**: the catalog definitions (`applications/<app>/<version>/`), shipped to a cluster as an **OCI bundle**. On kommander `main` it is the `kommander-applications/` directory of the kommander repo; on `release-2.18` a separate repository.
- **flux**: reconciles that tree into **HelmReleases**; a HelmRelease pulls its chart from an **OCIRepository**.
- **ClusterApp**: an app version the catalog offers; **AppDeployment**: a request to enable one in a **workspace** (a kommander grouping that maps to one namespace; `kommander-workspace` is the management cluster's own); **AppDeploymentInstance**: its status per attached cluster.
- **pin**: the app version an operator expects for a platform version; **carrier**: an object whose field states the platform version.
- **template / claim**: a frozen cluster the framework clones instead of creating; the cluster's **shape** (base version, change-set, topology) is the lookup key.
- **change-set**: `repo@ref` entries naming the branches under test.

---

## 3. How a run works

```
steps  →  collect (only on failure)  →  cleanup (always, unless --keep)
```

Steps stop at the first failure. Collect dumps nodes, pods, events,
HelmReleases, CAPI objects, the logs of everything unhealthy, and an
`nkp diagnose` bundle. Cleanup deletes what the scenario created and checks
Prism Central has no VM left; a supplied cluster (`use_existing_cluster`) is
never touched. Two things happen before every scenario without being written:
a Prism Central probe, and the scenario's `env:` defaults.

```
e2e-results/<timestamp>/
  RESULTS.md  junit-e2e.xml  run.log
  <scenario>/scenario.log  <cluster>.conf  diagnostics-failure/
```

| Flag | Effect |
|---|---|
| `--dry-run` | print the plan; run nothing external |
| `--keep` | skip cleanup; the log prints `export KUBECONFIG=…` for a second terminal |
| `--always-collect` | diagnostics on success too |
| `--timeout-minutes N` | default budget for cluster operations (60) |

A `pause:` step hands you the cluster: Enter resumes in a terminal; in the
background, `touch` the file the log names. An unresumed pause fails after 2 h
so cleanup still runs.

---

## 4. Writing a scenario

```bash
cp scenarios/example-pass.yaml scenarios/my-feature.yaml
./check_scenario.py my-feature         # self-test + dry run; non-zero on the first problem
```

A file in `scenarios/` is discovered by name.

```yaml
name: my-feature
description: one line; shown in --list and RESULTS.md
expected_minutes: 45                   # measured; for the reader, not a timeout

env:                                   # non-secret defaults; a shell export wins
  E2E_KPS_VERSION: "82.13.6"

steps:
  - group: 1 - a cluster running my kommander branch      # a heading in the log
  - create_cluster:
      control_plane: 1
      workers: 1
      worker_vcpus: 16
      worker_memory: 48
      changes: ["kommander@me/fix-123"]
  - group: 2 - the change is live
  - assert_change_running:             # the running image is the one built from the branch
      repo: kommander
  - assert_controller_log:             # and that code executed
      deployment: kommander-appmanagement
      contains: "fix-123 marker"
  - group: 3 - an app on top of it
  - deploy_app:
      app: kube-prometheus-stack
      version: "${E2E_KPS_VERSION}"
      workspace: kommander-workspace
  - assert_app_workload_running        # app-scoped steps default to the last deploy_app

cleanup:                               # runs pass or fail; skipped by --keep
  - finish_cluster
  - assert_no_leftover_vms
```

A step is `name` or `name: {options}`; `- wait: 2m` is shorthand for a step's
first option. `./run_e2e.py --steps` prints every step with its options and
defaults; a starred option must be set. `${VAR}` and `${VAR:-default}` work
anywhere; a bare `${VAR}` means the caller must export it. Unknown step names
fail at load with a suggestion; unknown options at `--dry-run`. Every
`timeout:` accepts `90s`, `15m`, `2h`. After a `deploy_app`, `wait_app_deployed`,
`assert_helmrelease_ready`, `assert_app_config_applied` and
`assert_app_workload_running` refer to that app when given no `name:`; without
a prior `deploy_app` the first three need `name:` and `namespace:`. A valid
app version is a ClusterApp on the cluster: `kubectl get clusterapps -n kommander`
lists `<app>-<version>`.

### Rules

**Assert the object that carries the change.** An image change is visible as
the running image (`assert_change_running`); "the code ran" as a controller
log line (`assert_controller_log`); a chart change as the chart reference
(`assert_chart_refs_changed`); a `metadata.yaml` change as the ClusterApp's
`apps.kommander.d2iq.io/*` annotations (`assert_app_metadata`); a config
override, or a chart change that reaches a workload, as the rendered field
(`assert_jsonpath {kind, name, namespace, path, equals}`). An AppDeployment
reporting Ready, or a Deployment that rolled, proves none of these.

**Record before you change.** `assert_chart_refs_changed {app}`,
`assert_nodes_unchanged` and `assert_platform_version_unchanged` compare
against `record_chart_refs` (every app, no options), `record_nodes`,
`record_platform_version` placed earlier. `upgrade_kommander` takes its own
snapshots; `deliver_changes` does not.

**Assertions poll** until their `timeout:`. Do not put `wait:` in front of them.

**Do not restate a default.** `wait_nodes_ready: {timeout: 25m}` fails the
self-test because 25m is the default. Topology (`control_plane`, `workers`) is exempt: it is part of the template hash.

**Give the platform enough worker CPU**: one 16 vCPU / 48 GiB worker, or
three of the CLI's default 8 vCPU. One default worker leaves pods Pending on
`Insufficient cpu` and the install waits until it times out.

**Pass `version:` to `deploy_app`**, and enable an app's `requiredDependencies`
(a field in its `metadata.yaml`) in the same workspace first: `istio-helm`
needs `kube-prometheus-stack`, which is not enabled on a fresh cluster.

**`machine_image:` in the scenario, or `E2E_MACHINE_IMAGE` in the env.** The
image fixes the kubernetes version; a scenario that must run on one specific
release names it, the starter leaves it to the env.

**Apps on an attached workload cluster render no HelmRelease on the management
cluster.** Assert with `assert_app_on_cluster`.

**`assert_controller_log` looks for a startup line.** On a pod that has run for
days it has rotated out; `restart_workload` first.

**No secrets in YAML.**

---

## 5. Delivering your change

Name the branch. The framework resolves it to a commit, builds what the repo
needs, pushes it under a tag that is the commit, and points the cluster at it.
Uncommitted work is not delivered. A claimed clone carries the change-set it
was frozen with, so the assertions below hold on claims as well as creates.

```yaml
  - create_cluster:                    # a cluster created with the change
      changes: ["kommander@my/branch", "charts@my/branch"]
  - deliver_changes:                   # onto a cluster that exists
      changes: ["kommander@my/branch"]
  - upgrade_kommander:                 # carried by an upgrade
      to_version: v2.18.1-dev
      changes: ["kommander@my/branch", "charts@my/branch"]
```

| Repository | Built | Delivered by | Assert with |
|---|---|---|---|
| `konvoy2` | the `nkp` binary | it is the CLI that creates or upgrades the cluster (`NKP_BIN`; `build-nkp-fast.sh` builds it in ~30 s, 1 s when unchanged) | `assert_nodes_upgraded`, `assert_preflight_skipped` |
| `kommander` | `kommander2-appmanagement` image | set on the HelmRelease through an overrides ConfigMap | `assert_change_running`, `assert_controller_log` |
| `kommander` (main) / `kommander-applications` (2.18) | the changed `applications/<app>/` content | synced into the cluster's git; on an upgrade, also published as the version's OCI bundle | `assert_app_metadata`, `assert_kapps_app_versions`, `assert_app_pins_resolve` |
| `charts` | the changed chart | pushed to your registry; the `OCIRepository` of the app using that chart (namespace `kommander` on the management cluster) is repointed, and flux reconciles it. The app must already be enabled; otherwise the step fails naming the missing OCIRepository. A charts-only delivery takes 2 to 3 minutes | `assert_chart_refs_changed`, `assert_jsonpath` on the workload |

**Where the applications tree lives.** On kommander `main` it is the
`kommander-applications/` directory inside the kommander repository; the
`release-2.18` line still uses the separate `kommander-applications`
repository. The framework detects which one a change-set is on: name
`kommander-applications@ref` on 2.18, `kommander@ref` on main: where one
entry carries both controller code and application content, and a commit that
touches only `kommander-applications/` builds no image. `E2E_KAPPS_DIR`
overrides detection. Run live so far: content-only commits on main
(`kapps-main-line`). Implemented but not yet run on a cluster: a main commit
that changes both controller code and content, and `upgrade_kommander` with
the bundle published from the subtree.

Delivery needs `GHCR_USER` and `GHCR_TOKEN` (a classic PAT, `write:packages`;
images and charts go to `ghcr.io/<you>/nkp-dev:<name>-<sha8>`; make that one
package public once, on github.com), `DOCKERHUB_USER`/`DOCKERHUB_PASSWORD`
(two platform charts come from Docker Hub and the lab's shared egress IP
exhausts the anonymous limit), and Docker to build images. Only
`kommander2-appmanagement` is built for kommander changes today.

---

## 6. Clusters

**Claim or create.** `create_cluster` hashes base version, change-set and
topology and looks it up in the template registry. A hit claims a clone of the
frozen template (~8 min); a miss creates with `nkp create cluster` (~36 min)
and freezes the result when the run ends without `--keep`. The framework checks
the template's VMs still exist on Prism Central before claiming, and a freeze
refuses a cluster that drifted since creation (upgraded or rolled). A claimed
cluster has node names and ages from the template.

**An existing cluster.** `E2E_KUBECONFIG=<path> ./run_e2e.py platform-health`.
`use_existing_cluster {kubeconfig, cluster_name}` takes the path from
`E2E_KUBECONFIG` when not set. Such scenarios create and delete nothing and
have no `cleanup:`.

**Workload clusters.** `create_workload_cluster` attaches one from the
management cluster; `workspace: "@workload"` targets it; `assert_app_on_cluster`
reads the management cluster's `AppDeploymentInstance`; `switch_cluster` is for
objects only the member owns; `delete_workload_cluster` removes it.

**Baselines for upgrades.** `E2E_BASE_NKP_VERSION=v2.17.0` makes
`resolve_baseline` download that release's CLI and create the cluster with it;
your `NKP_BIN` upgrades it. A baseline needs its own `machine_image:`.

**Tearing down.** A normal run cleans up. After `--keep`:
`NKP_BIN=<the CLI that built it> E2E_KUBECONFIG=<its kubeconfig> ./run_e2e.py delete-existing`.
A claimed cluster outside a run: `instant-cluster/teardown_claim.py`, which
detaches volume groups first (Prism refuses to delete a VM with one attached,
and says so only in the task). VMs with no owner: `./sweep_debris.py <prefix>`.
The `instant-cluster/` scripts read `NKP_PC_URL`, `NKP_NUTANIX_USER`,
`NKP_NUTANIX_PASSWORD`; the framework exports them, set them yourself when
running a script directly.

---

## 7. Upgrades

Three verbs, in this order: `upgrade_kommander` (platform), `upgrade_workspace`
(app versions on attached clusters), `upgrade_nodes` (kubernetes and nodes).
Both `upgrade kommander` and `upgrade cluster` exit 0 without doing anything on
a cluster already at the target: every upgrade scenario starts below it and
pairs the verb with a before/after assertion.

| You changed | Verb | Assert |
|---|---|---|
| kommander controllers | `upgrade_kommander` | `assert_change_running`, `assert_platform_upgraded` |
| a chart behind a pinned version | none; flux delivers it | `assert_chart_refs_changed` |
| a platform app version | the per-cluster pin (`set_app_version_pin`) | `assert_app_effective_version` |
| a catalog app version | `upgrade_catalog_app` | `assert_app_effective_version`, `assert_app_on_cluster` |
| konvoy2, CAPX, CAREN | `upgrade_nodes` (also upgrades the CAPI stack on a management cluster) | `assert_nodes_upgraded`, `assert_platform_version_unchanged` |

`upgrade_kommander {to_version, applications_repository, changes}` snapshots
the version carriers, delivers `changes:`, builds the upgrade CLI, publishes
your applications tree as the version's bundle, runs the CLI, then checks
every carrier, the operators, the applications, and that every HelmRelease is
Ready. A `-dev` target pulls that tag's **nightly** operator images, whose
pins name the app versions on the release branch that night; an applications
branch behind it makes a pin unresolvable and the step aborts within a minute
naming it. Keep the branch merged.

`upgrade_nodes {vm_image, skip_preflight}`: `vm_image:` is required by the
CLI. `skip_preflight: [names]` is written by konvoy2 onto the CAPI Cluster's
`preflight.cluster.caren.nutanix.com/skip` annotation, which CAREN's preflight
webhook reads; unknown names are ignored silently. Names in this CAREN:
`NutanixConfiguration`, `NutanixCredentials`, `NutanixPrismCentralVersion`,
`InfraVMImage`. `assert_preflight_skipped` reads the annotation.

The full blast-radius matrix and the reasoning behind each row:
`docs/SCENARIO-REFERENCE.md`.

---

## 8. The scenarios

| Scenario | Proves | Needs | Time |
|---|---|---|---|
| `example-pass` | a cluster comes up healthy and is torn down; the starter |: | 45 min (12 claimed) |
| `platform-health` | pins resolve, bundle carries the pins, federation healthy, dashboard serves | `E2E_KUBECONFIG` | 2 min |
| `developer-flow` | a cluster running your kommander build, istio with an override | `changes:` | 45 min |
| `all-components` | CLI + controller + chart changes on one cluster, every route asserted | `changes:` | 110 min |
| `kapps-main-line` | a `kommander@ref` on main delivers application content from the subtree | `changes:`, `E2E_KAPPS_DIR` | 65 min |
| `kapps-main-line-verify` | its assertions against a supplied cluster | `E2E_KUBECONFIG` | 1 min |
| `workload-attach` | day-2 attach, an app on the attached cluster, verified from management |: | 75 min |
| `demo-full-loop` | 2.18 cluster, istio with override, `upgrade kommander` carrying a three-repo change, five assertions | `changes:` | 75 min |
| `upgrade-existing-changeset` | an upgrade preserves a change-set already on the cluster | `E2E_KUBECONFIG` | 45 min |
| `kommander-upgrade` / `-existing` | a controller change survives `upgrade kommander` | `changes:` / `E2E_KUBECONFIG` | 45–50 min |
| `platform-upgrade` | GA baseline upgraded by the CLI under test; every carrier checked | `E2E_BASE_NKP_VERSION` | 60 min |
| `node-upgrade` / `-existing` | a node roll to the CLI's kubernetes; platform untouched | `E2E_BASE_NKP_VERSION` / `E2E_KUBECONFIG` | 45 / 40 min |
| `node-upgrade-skip-preflight` / `-existing` | `--skip-preflight-checks` reaches the annotation CAREN reads; the roll completes | `E2E_BASE_NKP_VERSION` / `E2E_KUBECONFIG` | 60 / 12 min |
| `workload-version-upgrade` | `upgrade workspace` moves an app version on an attached cluster |: | 100 min |
| `ga-baseline` | build and freeze the previous-release cluster upgrade scenarios claim |: | 120 min |
| `delete-existing` | delete a supplied cluster with the CLI that built it; prove no VM is left | `E2E_KUBECONFIG`, `NKP_BIN` | 2 min |
| `app-upgrade` | not runnable; its header explains why an app-only upgrade cannot be asserted |: |: |

`scenarios/archive/` holds recording aids and experiments; not listed, not templates.

---

## 9. Steps

`./run_e2e.py --steps` prints every step with its options.

**Getting a cluster**: `create_cluster`, `use_existing_cluster`,
`create_workload_cluster`, `install_platform`, `resolve_baseline`.
**Waiting**: `wait_nodes_ready`, `wait_pods_healthy`, `wait_reconciled`,
`wait_app_deployed`, `wait_cluster_attached`, `wait`.
**Delivering**: `deliver_changes`, `override_component_image`, `publish_chart`,
`publish_kapps_artifact`, `point_cluster_at_kapps_bundle`, `set_app_version_pin`,
`preflight_version_artifacts`.
**Apps**: `deploy_app`, `delete_app`, `upgrade_catalog_app`, `switch_cluster`.
**Upgrades**: `upgrade_kommander`, `upgrade_workspace`, `upgrade_nodes`.
**Day-2**: `scale_workers`, `cordon_node`, `drain_node`, `uncordon_node`,
`restart_workload`, `delete_workload_cluster`.
**Recording**: `record_nodes`, `record_platform_version`, `record_platform_carriers`,
`record_app_version`, `record_app_versions`, `record_app_instance`, `record_chart_refs`.
**Assert, the change is live**: `assert_change_running`, `assert_controller_log`,
`assert_chart_refs_changed`, `assert_app_metadata`, `assert_kapps_collection_synced`,
`assert_kapps_bundle_source`, `assert_jsonpath`.
**Assert, apps**: `assert_app_config_applied`, `assert_app_effective_version`,
`assert_app_version`, `assert_app_on_cluster`, `assert_app_instances`,
`assert_app_workload_running`, `assert_appdeployments_ready`,
`assert_helmrelease_ready`, `assert_all_helmreleases_ready`.
**Assert, platform**: `assert_platform_upgraded`, `assert_platform_changed`,
`assert_platform_version_unchanged`, `assert_platform_apps_moved`,
`assert_platform_operators_at_version`, `assert_app_pins_resolve`,
`assert_kapps_app_versions`, `assert_federation_healthy`, `assert_dashboard_serves`.
**Assert, nodes**: `assert_nodes_upgraded`, `assert_nodes_unchanged`, `assert_node_count`,
`assert_node_schedulable`, `assert_no_control_plane_taints`, `assert_preflight_skipped`,
`assert_api_reachable`, `assert_pods_healthy`, `assert_no_leftover_vms`.
**Control**: `pause`, `log`, `run` (a raw `kubectl` or `nkp` command), `fail`,
`collect_diagnostics`, `report_unschedulable_pods`, `finish_cluster`, `delete_cluster`.

Common options: `timeout:`; `namespace:` or `workspace:` (a workspace name
resolves to its namespace); `equals:`/`contains:` on value assertions; `was:`
naming the `record_*` key on before/after assertions; `moved: false` on
`assert_app_on_cluster` for the negative case.

**Adding a step** is one function in `framework/steps.py`: keyword-only
arguments become the YAML options, the first docstring line is what `--steps`
shows. `docs/SCENARIO-REFERENCE.md` has the template.

---

## 10. Configuration

All environment variables, read by `framework/config.py`; `--dry-run` prints
the resolved values.

| Variable | Required | Meaning |
|---|---|---|
| `PC_URL`, `NUTANIX_PRISM_ELEMENT_CLUSTER_NAME`, `NUTANIX_SUBNET_NAME`, `E2E_VIP_POOL` | yes | your Prism Central; no defaults |
| `NUTANIX_USER`, `NUTANIX_PASSWORD` | yes | `tam login pc-dev`; expire in days |
| `NKP_BIN` | yes | the CLI under test |
| `NUTANIX_STORAGE_CONTAINER_NAME` | no (`SelfServiceContainer`) | |
| `NKP_REPOS_DIR` | for `changes:` | parent of the repository checkouts |
| `SPEEDSTART_DIR` | for claims | template registry and claim state |
| `E2E_MACHINE_IMAGE`, `E2E_KUBERNETES_VERSION` | when a scenario sets no `machine_image:` | must match each other |
| `E2E_SSH_PUBLIC_KEY`, `E2E_SSH_USERNAME` | no (`~/.ssh/id_ed25519.pub`, `konvoy`) | without a key a broken node cannot be inspected |
| `E2E_CLUSTER_PREFIX` | no (`e2e-<user>`) | cluster and VM prefix; what cleanup sweeps |
| `E2E_KUBECONFIG` | existing-cluster scenarios | |
| `E2E_TIMEOUT_MINUTES` | no (60) | |
| `E2E_BASE_NKP_VERSION` or `E2E_BASELINE_NKP_BIN` | upgrade scenarios | the release to install first |
| `E2E_BASE_MACHINE_IMAGE`, `E2E_BASE_KUBERNETES_VERSION` | when the baseline differs | |
| `E2E_KAPPS_DIR`, `E2E_KAPPS_BASE` | no (detected) | applications tree override; diff base override |
| `GHCR_USER`, `GHCR_TOKEN` | delivering images or charts | `write:packages` |
| `DOCKERHUB_USER`, `DOCKERHUB_PASSWORD` | recommended | the two Docker Hub charts |
| `E2E_REGISTRY_MIRROR_URL`, `_USERNAME`, `_PASSWORD` | no | all three or none |
| `NKP_INSTANT_CLUSTER_DIR` | no (`instant-cluster/`) | claim machinery override |

---

## 11. When it fails

Read `RESULTS.md`: the failing step, its group and the reason. Then
`scenario.log` around that step, then `diagnostics-failure/`.

| Symptom | Cause | Do |
|---|---|---|
| `these describe YOUR Prism Central and carry no default` | `nkp-e2e.env` incomplete | fill the four named values |
| `Prism Central check failed: … (401)` | expired PC password | `tam login pc-dev`, update `nkp-e2e.env`, `make creds` |
| `RESOURCE_SHORTAGE` on create | the shared PE is full | wait, or `./sweep_debris.py` your own leftovers |
| pods Pending, `Insufficient cpu`; install waits forever | worker too small | `worker_vcpus: 16`, `worker_memory: 48` |
| `failed to wait for HelmRelease kommander-appmanagement` on install | Docker Hub anonymous limit | `DOCKERHUB_USER`/`DOCKERHUB_PASSWORD` |
| install times out on "all enabled applications"; PVCs `Pending` | PC credentials expired mid-run | refresh; `kubectl get pvc -A` |
| `upgrade kommander` waits on *platform Kommander applications*, cluster healthy | an unresolvable pin (section 7) | merge the applications branch onto its release branch |
| `upgrade cluster nutanix` exits in seconds | no `vm_image:` | set it |
| `dial tcp <VIP>:6443: connect: network is unreachable` | your machine's route to the lab changed mid-run | re-run; run long scenarios from the DevVM |
| `assert_controller_log` fails, image is right | startup line rotated out | `restart_workload` first |
| `no chart reference changed` after `deliver_changes` | no `record_chart_refs` before it | add one |
| every application reported changed | `E2E_KAPPS_BASE` from the other layout | remove it |
| claim fails: template VM no longer exists | the template was swept | handled: the run creates instead; a run without `--keep` re-freezes |
| `N VM(s) still present` after a delete | a volume group is attached | tear down as in section 6 |

Before rebuilding a cluster that "failed", check whether it finished on its
own; the CLI's timeout is shorter than a slow install:
`kubectl get kommandercore -A -o jsonpath='{.items[0].status.version}'` set means
installed; `kubectl get hr -A | grep -v True` empty means healthy.

---

## 12. Rules, and where to read more

- Runs create real VMs on a shared Prism Element that is usually above 90 %
  memory. Every scenario ends with `assert_no_leftover_vms`; clean up after an
  aborted run (section 6).
- Do not hand-name clusters or VMs: `<prefix>-<scenario>[-<sha8>]`.
- Before publishing this repository: `make audit` refuses credentials,
  internal hostnames, lab addresses and personal paths.

| Document | For |
|---|---|
| `docs/SCENARIO-REFERENCE.md` | every step's options; why each scenario is shaped as it is; the upgrade matrix |
| `docs/ASSERTION-MAP.md` | NKP layer by layer: what to observe, what exists, what is missing |
| `docs/ARCHITECTURE.md` | run lifecycle, failure path, pause |
| `docs/CLAIM-SPEEDUP.md`, `docs/FAST-BUILD.md` | claims and build caches, with numbers |
| `docs/ENV-SETUP.md` | every variable and the reasoning |
| `docs/PRE-MERGE-SOP.md` | the per-repository `make` suites (`hack/dev-vm/`) and PR evidence: a separate runner, complementary to scenarios |
