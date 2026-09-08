# Scenario file reference

Everything you can put in a `scenarios/*.yaml` file — the source of truth for
scenario authors. A scenario is a flat list of steps: no code, no branching.
If you need something the steps below cannot express, add a step to
`framework/steps.py` (one `@step` function) rather than adding logic to YAML:
scenarios stay readable because they cannot compute.

`./run_e2e.py --steps` prints the live catalogue straight from the registry;
this doc adds every option, default and the semantics.

## Top-level keys

```yaml
name: my-scenario              # optional - defaults to the file name
description: one-line summary  # shown in --list and RESULTS.md
expected_minutes: 45           # sets expectations; NOT a timeout

env:                           # optional scenario-scoped DEFAULTS - a
  E2E_APP: istio               # developer's shell export always wins

steps: [...]                   # the scenario - runs top to bottom, stops on
                               # first failure
collect: [...]                 # runs ONLY when steps failed - diagnostics
cleanup: [...]                 # runs ALWAYS (pass or fail) - teardown
```

Why three lists and not one: the contract is *steps stop on failure, collect
captures the corpse, cleanup always runs*. Encoding that in one list would
need per-step flags ("run-even-on-failure") which every author would get
wrong once.

Two guard rails run before every scenario automatically — authors never write
them: Prism Central reachability (a dead PC fails in second one), and the
scenario's `env:` defaults are applied.

Every cluster-producing step prints a `KUBECONFIG` banner with the export
line for your other terminal — also not a step you write.

## Environment interpolation

`${VAR}` or `${VAR:-fallback}` anywhere in the YAML. Resolution order: your
shell export > the scenario's `env:` block > the inline fallback. A bare
`${VAR}` with no fallback is the scenario saying "you must export this".

## Step syntax

```yaml
steps:
  - require_prism_central               # no options
  - create_cluster: {control_plane: 1}   # options inline...
  - deploy_app:                         # ...or spread over lines
      app: istio
  - group: day-2 operations             # not a step - a banner labeling the
                                        #   following steps in log + RESULTS
  - wait: 2m                            # single-value shorthand for a step's
                                        #   first option (here: duration)
```

Unknown step NAMES fail at load time with a did-you-mean hint. Unknown
OPTION names are caught when the step executes — and by `--dry-run`, which
is why you always dry-run a new scenario first. Every `timeout:` accepts
`90s`, `15m`, `2h`.

---

## Getting a cluster

### `create_cluster` — the way scenarios get a cluster

```yaml
- create_cluster:
    control_plane: 1        # topology is part of the change-set identity
    workers: 3
    changes: []             # ["repo@branch", ...] - YOUR changes. Each is
                            #   resolved to a commit (local checkout first,
                            #   then origin, then ls-remote); the sha8 also
                            #   derives the dev image tag. Empty = vanilla.
    version: ""             # a GA base instead (e.g. v2.17.0): GA binaries
                            #   are fetched and do the create+install; your
                            #   build acts on it via upgrade_kommander.
                            #   Mutually exclusive with changes.
    machine_image: ""       # the node image - a property of the TEST CASE,
    kubernetes_version: ""  #   so set both here, not in the terminal
    timeout: 90m            # budget for the create path
```

Hashes (base | resolved changes | topology) and consults the template
registry: hit -> ~10-minute claim of the frozen template; miss -> traditional
create + lean platform install + delivery of the change-set images.
Pair with `finish_cluster` in cleanup. Nothing to export, no template
names, no image names.

### `finish_cluster` — create_cluster's cleanup counterpart

```yaml
- finish_cluster:
    keep_on_freeze_failure: true   # a failed freeze keeps the cluster for
                                   #   inspection instead of deleting it
```

Claimed cluster -> delete the clone (template untouched). Created cluster ->
FREEZE it under the change-set name (`nkp-tmpl-<base>-<hash>-<topo>`) and
register it, so this change-set never pays for a create again. A cluster
whose creation failed part-way is KEPT, never deleted.

### `use_existing_cluster` — bring your own cluster

```yaml
- use_existing_cluster:
    kubeconfig: ""          # default: $E2E_KUBECONFIG
```

The scenario does not own this cluster's lifecycle; cleanup never deletes it.

### `assert_change_running` — the cluster runs YOUR build

```yaml
- assert_change_running:
    repo: kommander         # must appear in create_cluster's changes
    timeout: 10m
```

Derives the expected image ref from the change-set (sha8 = tag) and the
repo's delivery mapping, then polls the target deployment until its
container image matches. Never name an image in a scenario.

---

## Workload clusters (day-2 attach)

### `create_workload_cluster`

```yaml
- create_workload_cluster:
    control_plane: 1
    workers: 0                                  # 1 VM is enough and is what
                                                #   workload-attach ships; the step
                                                #   DEFAULTS to 1, so set it
    name_suffix: wl                             # name = <mgmt>-<suffix>
    namespace: kommander-default-workspace      # a WORKSPACE namespace is
                                                #   what makes kommander
                                                #   auto-attach the cluster
    timeout: 45m
```

Created FROM the management cluster (CAPX there provisions it). Kubernetes
version and node image are inherited from the management cluster — the one
pairing guaranteed to exist on the PC.

### `wait_cluster_attached`

```yaml
- wait_cluster_attached:
    name: ""                # default: the last create_workload_cluster
    timeout: 20m
```

### `switch_cluster`

```yaml
- switch_cluster:
    to: workload            # or: management
```

Retargets every following step's kubeconfig. In cleanup it degrades to a
no-op when there is nothing to switch to (cleanup stays best-effort).

### `delete_workload_cluster`

```yaml
- delete_workload_cluster:
    timeout: 30m            # delete via the mgmt cluster + VM sweep
```

---

## Apps (the customer path)

### `deploy_app` — what the UI does

```yaml
- deploy_app:
    app: istio                    # catalog app id
    version: "1.23.6"
    workspace: kommander-workspace  # workspace NAME - resolved to its
                                    #   namespace; empty = use namespace:
    namespace: ""                 # explicit target namespace (alternative)
    name: ""                      # AppDeployment name (default: app)
    kind: ClusterApp              # or App
    config_values: |              # inline values -> override ConfigMap,
      key: value                  #   wired into the AppDeployment
    config_overrides: ""          # name of an EXISTING override ConfigMap
```

### The app asserts

```yaml
- wait_app_deployed:        {name: "", namespace: "", timeout: 20m}
- assert_helmrelease_ready: {name: "", namespace: "", timeout: 15m}
- assert_app_config_applied: {name: "", namespace: "", timeout: 10m}
- assert_app_workload_running: {namespace: "", timeout: 10m, tolerate: 0}
- assert_all_helmreleases_ready: {namespace: "", selector: "", timeout: 25m}
- assert_appdeployments_ready: {namespace: "", timeout: 25m}
- delete_app:               {name: "", namespace: ""}
```

Empty `name`/`namespace` = "the app from the last deploy_app" — steps share
the run's memory, so scenarios don't repeat identifiers. Explicit values
override when a scenario deploys more than one app.
`assert_app_config_applied` checks the platform contract end to end: the
AppDeployment references the override CM AND the rendered HelmRelease
consumes it. (For apps federated to attached clusters, assert on the
workload cluster instead — its state is the proof.)

### Catalog app upgrades

```yaml
- record_app_version:  {app: istio, key: app_before}
- upgrade_catalog_app: {app: istio, to_version: "", to_version_from: "", workspace: ""}
- assert_app_version:  {app: istio, expected: "", was: app_before}
```

---

## The three upgrade verbs, and the three steps that run them

NKP's documented customer order is three separate verbs. The framework now
names each step after the verb it runs, because the old name `upgrade_cluster`
ran `nkp upgrade kommander` while `upgrade_nodes` ran `nkp upgrade cluster
nutanix` - the name pointed at the wrong one.

| Step | Verb | What moves | Change-set? |
|---|---|---|---|
| `upgrade_kommander` | `nkp upgrade kommander` | the management cluster's platform | **yes** - this is where `changes:` is delivered |
| `upgrade_workspace` | `nkp upgrade workspace <ws>` | platform apps on the clusters in a workspace, up to the versions the management cluster already carries | no - it consumes what step 1 landed |
| `upgrade_nodes` | `nkp upgrade cluster nutanix` | Kubernetes and the node images | no |

Writing `changes:` on `upgrade_workspace` would be wrong, not merely
redundant: it would imply a second delivery that does not happen.

An attached workload cluster does **not** follow `upgrade_kommander`. A
scenario that upgrades the platform and then asserts on a workload cluster
without running `upgrade_workspace` is asserting something that was never
supposed to be true yet.

**Asserting on a workload cluster without switching kubeconfig.** Kommander
creates one `AppDeploymentInstance` per (AppDeployment, matched cluster) on the
**management** cluster. `spec.kommanderClusterRef` names the cluster, and the
status carries both halves: `contentHash` is what git asks for,
`observedContentHash` is what flux actually applied *on the target cluster*
(`app_deployment_instance_types.go:78-83`). So `record_app_instance` +
`assert_app_on_cluster` prove an app moved on an attached cluster, from the
management cluster, with no second kubeconfig. `moved: false` asserts the
opposite - that it has *not* moved yet - which is how a scenario proves the
second verb was actually needed.

---

## Upgrade blast-radius matrix — which change needs which upgrade

Not every change needs a full NKP upgrade. The customer sequence on Nutanix
infrastructure is three steps (per the NKP docs): **1) `nkp upgrade
kommander` → 2) `nkp upgrade workspace` (each workspace) → 3) `nkp upgrade
cluster nutanix`**. A developer only needs the part their change can reach.

Every row below was source-verified (two independent audits, file:line
evidence) on 2026-08-29; the konvoy2 row is additionally proven live.

| Change class | Upgrade scope to exercise | Runs against | Assert on (observable) |
|---|---|---|---|
| kapps: artifact behind an already-pinned version | none — git/flux delivery | mgmt claim | HR revision moves; one AppDeploymentInstance **per selected cluster** |
| platform app (`appRef.kind: ClusterApp`) version | **only** via the per-cluster pin `clusterConfigOverrides[].appVersion` — `upgrade catalogapp` REFUSES these live: *"Platform Apps can't be upgraded individually"* | mgmt claim | pin rewritten → effective version moves → HR + workload (`app-upgrade.yaml`) |
| catalog application (`appRef.kind: App`) | `nkp upgrade catalogapp` | mgmt + **every** selector-matched cluster | `appRef.name == <app>-<newver>`. **Needs a catalog repository** - a lean profile has 62 ClusterApps and *zero* catalog apps, so this row cannot run on a lean claim |
| kommander controllers | `upgrade kommander` — an ordered upgrader list (preflights → app-repo-updater → management-plane → root-CA → kommandercore → configOverrides → platform apps). It patches `ManagementPlane`/`NKPCluster.spec.version` **directly**; it does **not** create an `UpgradePlan` (see trap 4) | GA baseline, **built not claimed** (see below) | controller Deployment **image tag** + platform version moved + node set **unchanged** (`kommander-upgrade.yaml`). **No fan-out** to attached clusters — asserting that fails by design |
| konvoy2 / CAPX / CAREN | `upgrade cluster nutanix` — **subsumes capi-components on a management cluster** | GA baseline, **built not claimed**, at **k8s below the CLI target** | node kubeletVersion moved + all Ready + every node maps to a tracked Machine (`node-upgrade.yaml`) |
| CAREN addon versions | CAREN **chart** upgrade (not an image swap) + the node roll | same as above | pinned chart versions on the running cluster |
| release seams | the full three-step customer order | fresh GA create | composition health (`platform-upgrade.yaml`) |

Six traps the audits surfaced, all encoded in the steps:

- **Vacuous green — BOTH upgrade verbs no-op silently on a same-version
  baseline.** `upgrade cluster nutanix` returns early when the cluster is
  already at the CLI's target kubernetes version (konvoy2
  `cluster/upgrade.go:214-218`), and `upgrade kommander` does the same for
  the platform version: `if latest.Spec.Version == kommanderVer { return
  nil }` (`kommander-cli/pkg/upgrade/upgrade_nkpcluster_helper.go:39-41`,
  read directly 2026-08-29). Both exit 0 and print nothing alarming. This
  is why every upgrade scenario starts from the **GA baseline** and pairs
  the upgrade with a before/after assertion — the baseline is not a
  convenience, it is what makes the test falsifiable.
- **Managed clusters get no CAPI upgrade**: the subsumption is gated on
  `IsManagementCluster` (`upgrade.go:422-428`); for a managed cluster the
  ClusterClass must already exist in its namespace.
- **Platform app versions are pinned per CLUSTER**, not per workspace
  (`KommanderCluster.spec.platform.version`), and defaulted only at create
  time — so shipping a new version string moves nothing on its own.
- **`UpgradePlan` is NOT the CLI's mechanism** (corrected 2026-08-29, second
  audit). Nothing in kommander, kommander-cli, konvoy2 or konvoy-cli ever
  *creates* an UpgradePlan — its controllers only consume plans something
  else writes, and the only instances in the tree are kuttl fixtures. The
  CLI patches `NKPCluster.spec.version` directly
  (`kommander-cli/pkg/upgrade/upgrade_nkpcluster_helper.go:21-57`). A
  scenario that waits on UpgradePlan conditions after `nkp upgrade
  kommander` would wait forever on a healthy cluster.
- **Never prove a controller moved via an app version.** `kommander` and
  `kommander-appmanagement` are themselves platform apps, but their
  AppDeployment version is the *k-apps app* version (0.18.0 on 2.18), which
  moves only when the app directory is bumped — while the controller image
  tag changes on every kommander build. Assert on the **container image**
  (`assert_change_running`), never on `appRef.name` or
  `clusterConfigOverrides[].appVersion`
  (`common/pkg/installer/applicationmanager/service.go:108-122`).
- **UpgradePlan status shape is version-gated** — condition types, phases,
  reason strings, step names and RBAC all differ between v2.18.0,
  release-2.19 and main. 21 of ~40 claims in the second audit were refuted
  for exactly this reason: the local `kommander` checkout sits on
  `feat/dev-vm-pre-pr-testing`, ~94 commits behind `origin/main`. Pin any
  UpgradePlan assertion to the version under test, and read `origin/main`
  rather than the working tree when auditing.

Live-verified additions (2026-08-29): the effective version on a real cluster came from `clusterConfigOverrides[].appVersion`, **not** `appRef.name` — and its `clusterSelector` matches on `kommander.d2iq.io/cluster-name`, which on a CLAIM is still the **template's** name (the KommanderCluster identity is inherited by the clone). Any per-cluster assertion must account for that.

### `upgrade workspace` — cell CLOSED by source, 2026-08-29

This was the matrix's open cell. `kommander-cli` was located and read, and
the semantics are now pinned:

- **It refuses the management workspace.** `upgrade workspace
  kommander-workspace` returns *"upgrading management cluster's workspace is
  done using `upgrade kommander` subcommand"*
  (`cmd/upgrade/workspace/workspace.go:19,51-53`). The management workspace
  is only ever moved by step 1.
- **Its target is the RUNNING management version, not the CLI's.** It reads
  the chart version of the live `kommander` HelmRelease
  (`workspace.go:67-70`), whereas `upgrade kommander` uses the CLI binary's
  compiled version. **This is why the documented customer order is
  load-bearing**: run `upgrade workspace` before `upgrade kommander` and it
  faithfully upgrades the workspace to the version you already had.
- **What it mutates**, in order (`pkg/upgrade/workspace.go:50-109`): removes
  disabled AppDeployments → deploys ultimate-tier AppDeployments → bumps
  workspace *and project* AppDeployments' `appRef.name` to the latest
  ClusterApp versions → patches **every NKPCluster in the workspace
  namespace** → waits for completion. The AppDeployment bump deliberately
  precedes the cluster patch to avoid a controller race that would rewrite
  `appRef.name` back to the older version (comment at :80-84).
- **It shares the per-cluster helper with `upgrade kommander`** —
  `updateNKPClusterPlatformVersion` (`pkg/upgrade/upgrade_nkpcluster_helper.go:77-105`),
  which patches `spec.version` plus the embedded
  `spec.kommanderCluster.spec.platform.version`. So the **same silent no-op
  guard applies**: a workspace already at the target version reports success
  having changed nothing.
- **An empty workspace is not a no-op**: with zero NKPClusters it still
  updates AppDeployments, then returns early without waiting (:89-92).
- **`--dry-run` lists the operations without performing them**, which makes
  this the one upgrade verb whose semantics can be checked on a live cluster
  for almost no time.
- Note the maintainers' `TODO(gracedo)`: workspace upgrade is on a
  deprecation path in favour of per-cluster upgrade — worth tracking before
  investing in a heavy scenario here.

### `upgrade catalogapp` — cell CLOSED by source, 2026-08-29

An earlier note here said this CLI "lives outside our checkouts". That was
wrong: it is `kommander-cli/pkg/catalogappdeployment/upgrade.go`, and the
whole verb is 100 lines. Reading it confirms the live observation and adds
the thing that matters for test design.

Its guard chain, in order (`upgrade.go:28-78`), each with a distinct error:

| Condition | Result |
|---|---|
| no AppDeployment of that name | `ErrCatalogAppDeploymentNotFound` |
| `appRef.kind != App` | **`ErrTypeClusterApp`** — the exact string we hit live, at `upgrade.go:25` |
| target not semver | `ErrVersionMustBeSemVer` |
| target == installed | **`ErrVersionAlreadyInstalled`** |
| target < installed | `ErrDowngradeNotSupported` |
| no `App` named `<appID>-<toVersion>` in the namespace | `ErrVersionNotFound` |

**This is the one upgrade verb that fails LOUD on a same-version request.**
`upgrade kommander`, `upgrade workspace` and `upgrade cluster nutanix` all
return success having changed nothing; `upgrade catalogapp` returns
`ErrVersionAlreadyInstalled`. So it is the only one where a vacuous-green
baseline is impossible — worth knowing, because it means this row needs no
GA baseline, only a catalog repository.

On success it rewrites the AppDeployment with
`appRef.name = <app.spec.appID>-<toVersion>` — note the prefix is the App's
`spec.appID`, not the AppDeployment's name — so the observable is that field
(`upgrade.go:71,93-95`).

One caution for whoever writes this scenario: `ErrVersionNotFound` means the
**target `App` object must already exist in the namespace**, which is the
concrete reason this row needs a catalog repository synced first. A lean
claim carries 62 ClusterApps and *zero* catalog apps, so it cannot run as-is.

**Does a per-cluster pin survive a catalog upgrade? YES — verified, not
assumed.** The replacement spec carries only `AppRef` and `ConfigOverrides`
(`upgrade.go:88-98`), which looks like it would drop everything else, but two
things save it:

1. the writer fetches the live object and overlays only the keys present in
   the desired spec — *"override known keys, leave out unknown keys"*
   (`kommander-cli/pkg/upgrade/utils.go:85-89`);
2. `clusterConfigOverrides`, `clusterPolicy` and `clusterSelector` are all
   `omitempty` (`kommander/clientapis/pkg/apis/apps/v1alpha3/app_deployment_types.go:89-100`),
   so when unset they never appear in the desired spec and the merge loop
   never reaches them.

`appRef` has no `omitempty` and is therefore always overwritten — which is
exactly the upgrade. One caveat on (1): `clusterSelector` IS set when the
caller passes a non-empty cluster list; `upgrade catalogapp` passes `nil`
(`upgrade.go:99`), so it is safe there, but another caller would overwrite
it.

## What is implicit, and why

The rule: **a verb owns its own contract; a scenario owns its own claim.**

If something must be true for the verb to have succeeded, the verb checks it —
nobody should be able to forget it, because forgetting is how a run goes green
having proved nothing. If something is the *reason this scenario exists*, it
stays in the file, because that is what a reviewer reads.

| Implicit — the verb's contract | Explicit — the scenario's claim |
|---|---|
| `create_cluster` does not return until `control_plane + workers` nodes are Ready. Creating is synchronous (`nkp create cluster` blocks; the claim path runs identity gates), so a following `wait_nodes_ready` only ever re-stated the topology the step was already given. | Containment — `assert_nodes_unchanged`, `assert_platform_version_unchanged`. These differ per row of the matrix and are the point of the test. |
| `upgrade_kommander` snapshots the before-state, then checks every version carrier, the release content, the operator roll-out, the applications, that delivered changes still run, that kommander pods are healthy and that every HelmRelease is Ready. | App-specific assertions — `assert_app_config_applied`, `assert_jsonpath` on a rendered workload, `assert_app_effective_version`. |
| Diagnostics on failure. A scenario with no `collect:` still gets nodes, pods, events, CAPI objects, HelmReleases and the logs of everything unhealthy. Nobody would ever choose not to. | `pause` — where a developer wants the cluster handed over. |
| `cleanup:`. A scenario whose steps produced a cluster gets `finish_cluster`. It already owns the decision: sweep a claim, freeze a create, keep a half-built one, and leave a SUPPLIED one alone. | A scenario that must tear down something else first — `delete_workload_cluster` before the management cluster. |

Two consequences worth knowing:

- **`finish_cluster` will not delete a cluster it did not create.** `use_existing_cluster` marks the run, and `finish_cluster` returns early. Before that guard existed, merely listing `finish_cluster` in an existing-cluster scenario would have deleted the developer's cluster — the mode was unset, every branch fell through, and it reached `delete_cluster`.
- **An option that repeats its step's default is a lint failure.** `selftest.py` fails on `kubernetes_version: ""` or `timeout: 90m`, because a line that looks like a decision but is not makes a reviewer ask why the version is blank — when the answer is that it was never set. Topology (`control_plane`, `workers`) is exempt: it is part of the change-set hash, so leaning on a default would let a change to that default silently invalidate every frozen template.

An optional change-set is written `changes: ["${E2E_CHANGE}"]`; an empty entry
means "no changes" rather than a malformed one, so the option stays visible in
the file instead of hiding behind an environment variable nobody knows exists.

## Testing the NKP upgrade process

From an older release to the version your binary reports — see
`platform-upgrade.yaml` and `kommander-upgrade.yaml` for working examples.

### `upgrade_kommander`

```yaml
- upgrade_kommander:
    to_version: ""                # what the cluster must look like afterwards
    changes: []                   # commits to deliver BEFORE upgrading
    verify: true                  # run the full post-upgrade check set
    binary_from: ""               # which CLI performs it; empty = NKP_BIN
    applications_repository: ""   # override the apps repo; empty = default
    timeout: 60m
```

Runs `nkp upgrade kommander` — step 1 of the customer's order. The kubernetes
side is a separate step, not a missing one: `upgrade_nodes` runs
`nkp upgrade cluster nutanix`, which on a management cluster also subsumes
`upgrade capi-components`.

**The target version comes from the binary that runs the command.** There is no
version flag (`kommander-cli cmd/upgrade/kommander/kommander.go:72,124`), and
every downstream object follows that one value: the k-apps tarball URL,
`KommanderCore`, `ManagementPlane`, `NKPCluster`, `KommanderCluster`, the Flux
OCIRepositories, the operator Kustomizations, and every AppDeployment's
resolved app version.

`to_version:` is therefore a declaration, not a flag. It does three things:

1. **preflights the artifacts** — a version with no published k-apps tarball
   fails in seconds instead of wedging the run around minute 40. This is also
   how the framework discovers what is testable, per run, with no allow-list
   to go stale. Probed 2026-08-31: `v2.18.0`, `v2.17.0`, `v2.19.0-dev.23` and
   `v2.18.0-rc.1` exist; `v2.18.1`, `v2.18.2`, `v2.19.0` do not.
   It also resolves the **OCI bundle**, which is the gate that actually stops
   an upgrade — see "You cannot invent a version name" below.
2. **checks the binary against the declaration** — if the CLI in hand reports
   a different version the released CLI for the target is fetched instead, so
   the upgrade cannot silently go somewhere else. With `binary_from:` set the
   run FAILS instead of swapping, because testing your own build is the point.
3. **records the target**, so the checks below are held to it rather than to
   whatever the cluster happened to reach.

Leave it empty and the target is read from the binary and preflighted anyway.

`changes:` delivers commits before the upgrade runs, the same way
`create_cluster: {changes: [...]}` does on the create path. Ordering is the
point of an upgrade test: land the change on the old cluster, upgrade over it,
prove the upgrade did not put the shipped code back.

`verify: true` (the default) makes `upgrade_kommander` snapshot the before-state
and then run, in order:

| check | what it catches |
|---|---|
| `assert_platform_upgraded` | ten version carriers held to the target, including `NKPCluster.status.platformVersion` (the "NKP Version" a user sees) and the kommander HelmRelease chart tag (what the NEXT upgrade preflights). A partial upgrade passes `assert_platform_changed`, which reads one field. |
| `assert_kapps_collection_synced` | the release CONTENT arrived — both per-version Flux OCIRepositories Ready at the target tag. Without it every version field can read the new number while the cluster still runs the previous release's manifests. |
| `assert_platform_operators_at_version` | the six platform operator Kustomizations rolled and are Ready |
| `assert_platform_apps_moved` | the applications themselves moved, and every resolved appVersion has a ClusterApp to resolve to |
| `assert_kapps_bundle_source` | the cluster is still resolving apps from YOUR published bundle. Without it the worst k-apps outcome is invisible: the platform reports the target version reconciled, every app is healthy, and the bundle it pulled was `ghcr.io/mesosphere`'s — so the change under test was never installed. |
| `assert_change_running` | every delivered change is still running — an upgrade re-renders every HelmRelease from the new release's directory |

**Why this is opt-out rather than opt-in:** `nkp upgrade kommander` returns
early and exits 0 when the cluster is already at the CLI's version
(`upgrade_nkpcluster_helper.go:39-41`). A scenario that forgets its assertions
is a green run that upgraded nothing. Set `verify: false` only to assert
something unusual by hand.

Presence-aware throughout: a 2.17-shaped cluster has no `ManagementPlane` and
no `NKPCluster`, so those carriers are absent before and after and are skipped
with a printed note. Absent-having-existed, or disagreeing, still fails. If
NONE of the carriers are readable the step fails rather than reporting success
— every NKP cluster has at least `KommanderCore` and the kommander
HelmRelease, so reading zero means the kubeconfig or RBAC is wrong.

### Docker Hub rate limiting stalls an upgrade opaquely

The `kommander` and `kommander-appmanagement` charts come from
`oci://docker.io/mesosphere/...`; every other chart is on ghcr. A shared lab
egress IP burns Docker Hub's anonymous allowance (100 pulls / 6h / IP), and an
upgrade re-pulls both. When that happens `nkp upgrade kommander` simply sits on
"Ensuring KommanderCore is upgraded" while two OCIRepositories report `failed
to determine artifact digest`, with nothing in the CLI output naming the cause.
Live-caught 2026-09-01: 17 minutes of no progress, cleared within 45s of
authenticating.

Export `DOCKERHUB_USER` and `DOCKERHUB_PASSWORD` and both the claim path and
`upgrade_kommander` hold an authenticated session for the duration
(`_dockerhub_hold`). Without credentials it is a no-op — a run that is not
rate-limited does not need it.

### You cannot invent a version name

Live-caught 2026-09-01. `--kommander-applications-repository` feeds the CLI's
own steps only. The **ManagementPlane controller** resolves applications from
an OCI bundle keyed by version — it creates
`kommander-applications-<rfc1123 version>` pointing at
`oci://ghcr.io/mesosphere/kommander-applications:<version>`
(`common/pkg/oci/kapps.go:16`, applied with server-side apply and
`ForceOwnership`, so patching the object is reverted) and blocks on it. The
wait is `10 * time.Minute`, hardcoded, no flag
(`pkg/managementplane/wait.go:19`). A version nobody published therefore fails
after exactly ten minutes with `client rate limiter Wait returned an error`,
which names nothing.

Worse, the target version is not a label. In kommander-applications the
operator kustomizations carry
`managementplane.nkp.nutanix.com/version: "${kommanderChartVersion:=v2.18.1-dev}"`
and the **same variable is the image tag** those manifests run
(`mesosphere/kommander2-core-installer:${kommanderChartVersion}`).
`IsKustomizationAtVersion` requires that annotation to equal the target exactly
— only `-SNAPSHOT` is stripped
(`managementplane/pkg/step/flux_kustomization.go:119`). So a made-up name would
have to be a tag whose images exist.

`upgrade_kommander` now refuses the mismatch up front, naming the version the
branch declares. For `release-2.18` that is `v2.18.1-dev`; your chart, k-apps
and controller changes still ride on it.

The second consequence is subtler: **an upgrade reinstalls app definitions from
the bundle**, quietly reverting whatever the devloop git route synced. A
kommander-applications change only survives an upgrade if the bundle itself is
yours. So when `changes:` names kommander-applications, `upgrade_kommander`
publishes that commit as the target version's bundle and points the cluster at
it:

```yaml
- publish_kapps_artifact:      {version: "", sha8: "", package: nkp-dev, point_cluster: true}
- point_cluster_at_kapps_bundle: {url: ""}
- assert_kapps_bundle_source:  {version: "", url: ""}
```

The bundle recipe is data-driven from the repo's own `.include-airgapped`
(`clusters`, `common`, `applications`, `charts`) minus `.exclude-airgapped`;
rebuilding `v2.18.0` that way reproduces the released artifact's file list
exactly (543 files). It is pushed as a **tag on the already-public `nkp-dev`
package**, for the same reason `publish_chart` is — a new GHCR package is
private and the cluster pulls anonymously.

`KAPPS_OCI_URL` (`managementplane/cmd/main.go:130`) is the only supported
override, and setting it with `kubectl set env` is **not** durable on its own:
the upgrade re-applies the management-plane operator from the bundle and wipes
it, after which the controller rewrites the OCIRepository back to
`ghcr.io/mesosphere` and Flux re-pulls the shipped apps. So the operator
manifests inside the published bundle carry the env themselves. That is what
`assert_kapps_bundle_source` exists to hold.

### Around the upgrade

```yaml
- record_platform_version:   {key: platform_before}    # one field
- assert_platform_changed:   {was: platform_before}
- record_platform_carriers:  {key: carriers_before}    # all ten
- record_app_versions:       {key: app_versions_before}
- preflight_version_artifacts: {version: "", require: true, need_cli: false}
- record_app_version:  {app: istio, key: app_before}
- upgrade_catalog_app: {app: istio, to_version: "", to_version_from: "", workspace: ""}
- assert_app_version:  {app: istio, expected: "", was: ""}
- install_platform:    # explicit platform install (create_cluster does this
    binary_from: ""    #   itself; direct use is for special flows)
    disable_apps: []   #   "name?" = skip if absent
    timeout: 45m
```

Record a carrier snapshot explicitly only when you need it taken at a specific
moment. `upgrade_kommander` takes one itself otherwise — **before it delivers
`changes:`**, so the baseline is the cluster as it was, not as the delivery
left it. Taking it afterwards made `assert_chart_refs_changed` compare the new
chart tag against itself and report no movement (fixed 2026-09-01). It also
refreshes its own snapshot before a second upgrade, so a scenario that upgrades
twice does not measure the second against the first.

The individual assertions are all registered steps and can be listed by hand
with `verify: false`, but the default set is the one that has been tested.

## Nodes and day-2 operations

```yaml
- wait_nodes_ready:   {count: 4, timeout: 25m}
- wait_pods_healthy:  {namespace: null, timeout: 15m, tolerate: 0}   # null ns = all
- wait_reconciled:    {timeout: 20m, kind: cluster}    # NKPCluster Reconciled
- cordon_node:        {node: null, role: worker}       # node null = pick by role
- uncordon_node:      {node: null, role: worker}
- drain_node:         {node: null, role: worker, timeout: 10m}
- scale_workers:      {replicas: 4, machine_deployment: ""}
- restart_workload:   {target: deploy/coredns, namespace: kube-system, timeout: 10m}
```

## Assertions (every one polls until its `timeout:` expires)

```yaml
- assert_node_count:        {count: 4, role: null, timeout: 2m}
- assert_node_schedulable:  {node: null, role: worker, timeout: 2m}
- assert_pods_healthy:      {namespace: null, tolerate: 0, timeout: 5m}
- assert_api_reachable:     {timeout: 2m}
- assert_no_leftover_vms:   {timeout: 12m}
- assert_no_control_plane_taints: {node: null, timeout: 2m}
- assert_jsonpath:                          # the universal "did it land"
    kind: hpa
    name: istio-ingressgateway
    namespace: istio-system
    path: "{.spec.minReplicas}"             # quote paths with \. escapes
    equals: "1"
    timeout: 5m
```

`wait_*` and `assert_*` differ only in intent (progress gate vs verdict) —
both are safe against slow clusters.

### Added 2026-09-07 — the checks whose absence cost hours

| step | proves | options |
|---|---|---|
| `assert_app_pins_resolve` | every platform AppDeployment pin (`clusterConfigOverrides[].appVersion`) names a ClusterApp that exists — the bundle and the operator agree | `timeout` |
| `assert_kapps_app_versions` | a kommander-applications ref carries the app versions given (`apps: {kommander-ui: 17.234.34}`) — before the bundle is built | `apps`, `ref` (default: the change-set's k-apps commit), `repo` |
| `assert_federation_healthy` | every KubeFedCluster Ready, claimed by a KommanderCluster, and Federated* propagating — no orphaned member | `timeout` |
| `assert_dashboard_serves` | `nkp get dashboard`'s URL follows to a 200 whose title contains `title_contains` (default `Log In`) — LB → traefik → forward-auth → dex in one request | `title_contains`, `timeout` |
| `assert_app_metadata` | a `metadata.yaml` field reached the cluster: kommander writes it onto the ClusterApp as `apps.kommander.d2iq.io/<field>` annotations (`display-name`, `description`, `category`, `scope`, …). Proven live 2026-09-08 on the main-line subtree (`kapps-main-line`). | `app`, `field` (default `description`), `contains`, `namespace`, `timeout` |
| `assert_preflight_skipped` | the CAPI Cluster's `preflight.cluster.caren.nutanix.com/skip` annotation lists the checks — the only thing that decides whether CAREN skips them | `checks`, `namespace`, `timeout` |

`upgrade_nodes` gained `skip_preflight: [names]` (or `[all]`), passed as
`--skip-preflight-checks`. It also needs `vm_image:` (or `E2E_MACHINE_IMAGE`) - the
CLI refuses to run without `--vm-image`, and the step now says so by name. CAREN's
skip evaluator does prefix matching on the annotation and ignores unknown names; real
check names in this CAREN: `NutanixConfiguration`, `NutanixCredentials`,
`NutanixPrismCentralVersion`, `InfraVMImage`. `platform-health.yaml` runs the first four against
any existing cluster in ~2 minutes; `node-upgrade-skip-preflight.yaml` proves the
fifth on a GA baseline.

### Where kommander-applications lives (2026-09-07)

On kommander `main` the applications tree is the `kommander-applications/`
directory inside the kommander repository (merged 2026-06-11); `release-2.18`
still uses the separate repository. `_select_kapps_source` in `steps.py`
decides per change-set: `kommander-applications@ref` means the separate repo,
`kommander@ref` whose tree has `kommander-applications/applications` means the
subtree at that commit, and every helper (`_kapps_changed_apps`,
`_kapps_declared_version`, `_kapps_artifact_tree`, `_staged_kapps_repo`,
`_deliver_kapps`, `assert_kapps_app_versions`) runs git in the resolved root
with the resolved path prefix. On the main line a `kommander@ref` that touches
`kommander-applications/applications/` is delivered through the k-apps route
as well as the image route. `E2E_KAPPS_DIR` overrides detection.

## Utilities and escape hatches

```yaml
- pause:                       # AUTOMATION STOPPED - cluster is yours.
    message: inspect at will   #   Enter resumes (interactive); touch
    timeout: 2h                #   <artifacts>/resume (background). Timeout
                               #   FAILS the run so cleanup still happens.
                               #   comment the step out for unattended runs.
- fail: {message: "..."}       # deliberate failure (tests the failure path)
- run:                         # ONE raw command when no step fits; if you
    kubectl: "get pods -A"     #   use it twice for the same thing, promote
    nkp: null                  #   it to a step
    expect_success: true
- collect_diagnostics:         # dumps + `nkp diagnose` support bundle
    label: failure
    bundle: true
- report_unschedulable_pods: {filename: unschedulable.txt}
```

## Internal machinery (not for scenario authors)

`claim_cluster` / `delete_claim` — driven by `create_cluster`/`finish_cluster`
with identity from the template registry; only claim-machinery tests call
them directly. `override_component_image` — the delivery vector
`create_cluster` drives from `changes:`; direct use is for pipeline debugging.
`require_prism_central` — runs automatically before every scenario.
`reset_bootstrap` — runs automatically before every self-managed create
(waits for a busy bootstrapper, deletes a stale one).

---

---

# Scenario catalogue

The scenario files themselves carry no commentary — everything about why a
scenario is shaped the way it is lives here, so there is one place to read and
one place to correct. Ten scenarios, each with at least one live green run.

## Choosing one

| Your change | Scenario | Cluster | Live PASS |
|---|---|---|---|
| anything, first look | `developer-flow` | claim 1cp+3w | 19.7 min |
| CLI + controller + charts at once | `all-components` | claim 1cp+3w | 20.7 min |
| a platform application version | `app-upgrade` | claim 1cp+3w | 16.9 min |
| day-2 attach / workload clusters | `workload-attach` | claim + create | 27.3 min |
| kommander controller, cheap re-run | `kommander-upgrade-existing` | yours | 16.0 min |
| konvoy2 / CAREN, cheap re-run | `node-upgrade-existing` | yours | 14.0 min |
| kommander controller, real upgrade | `kommander-upgrade` | build 1cp+1w | 57.2 min |
| konvoy2 / CAPX / CAREN | `node-upgrade` | build 1cp+1w | 53.6 min |
| blast radius you cannot bound | `platform-upgrade` | build 1cp+1w | 52.8 min |
| (not a test — a baseline factory) | `ga-baseline` | build 1cp+1w | 50.8 min |

---

## `developer-flow`

The developer story end to end: give branches, get a cluster running your
build, enable an app with a config override, prove the override took effect.

- The framework resolves branches to commits (local checkout first, then
  origin), decides claim-or-create, and delivers the images built from those
  commits — the sha8 **is** the registry tag.
- The first run of a change-set pays create + platform and freezes the result
  as a template **born with** your images; later runs claim it.
- `changes:` defaults to the COMMIT the current template was frozen from
  (`kommander@7efd9db8`, hash `527051b2`, template `qa-nrm3`). A branch ref
  moves, and the change-set hash moves with it, so the frozen template stops
  matching and the run silently becomes a build instead of a claim. Point it
  at your own work with `E2E_KOMMANDER_REF=my-branch`.
- `kube-prometheus-stack` is deployed first because istio's HelmRelease
  depends on it.
- The `minReplicas: 1` override IS the proof: istio ships `2`.
- The `pause` steps print resume instructions (Enter, or create the named
  file). Comment the step out for unattended runs — a pause
  left in an unattended run eventually fails by design.

## `all-components`

Changes in every component class at once, on one cluster, with every delivery
route asserted: konvoy2 builds the CLI that creates the cluster, kommander's
image is injected and asserted by derived ref, kommander-applications is
synced into the cluster's own git and asserted in the rendered workload.

- Defaults are the three commits template `f97ba824` was frozen from, for the
  same claimability reason as `developer-flow`.
- `strict: true` on `assert_change_running` is correct HERE and only here: the
  template was born with these images, so "old code never ran on this cluster"
  is a true invariant. It is false by construction on any upgrade scenario.
- The `demo.nkp.nutanix.com/e2e-pipeline` annotation is what makes the
  kommander-applications assertion falsifiable: GA istio does not render it.
  **It is a VALUES change, not a chart change.** Commit `dab2da0d` adds
  `podAnnotations` to `applications/istio/1.23.6/helmrelease/cm.yaml`; the
  chart itself is still stock `oci://ghcr.io/mesosphere/charts/istio:1.23.3`.
  An app directory holds a *reference* to a chart (an OCIRepository url + tag)
  plus its values — so this route delivers values, HelmRelease spec,
  dependencies and the chart TAG, but it cannot change chart CONTENT. That
  needs the chart published to a registry first; nothing here does that.

## `app-upgrade`

A platform application's version moves through the **per-cluster pin**, not
through a workspace or an upgrade verb.

- `nkp upgrade catalogapp` REFUSES platform apps (verified live 2026-08-29:
  *"this AppDeployment references a Platform App. Platform Apps can't be
  upgraded individually"*). Catalog apps take the `upgrade_catalog_app` path
  and need a catalog repository.
- The EFFECTIVE version is the first matching
  `spec.clusterConfigOverrides[].appVersion`, and only otherwise
  `spec.appRef.name` (`version_selector.go:39-60`). Asserting `appRef` alone
  can pass while a stale per-cluster pin holds the old version.
- An AppDeployment fans out as one AppDeploymentInstance per selected cluster
  (`synchronizer.go:187-220`) — that is what proves federation, not a single
  HelmRelease.
- Platform app versions are pinned per cluster
  (`KommanderCluster.spec.platform.version`), not per workspace.

## `workload-attach`

Self-contained proof of the day-2 attach flow: get a management cluster,
attach a workload cluster, deploy an app with an override to the workspace,
verify both **on the workload cluster**.

- Minimal footprint: 2-node management (the single worker sized up so the
  lean platform fits), 1-node workload with the control plane untainted.
- `assert_app_instances` runs BEFORE the deep assert on purpose. An
  AppDeployment whose `spec.clusterSelector` does not select the cluster is
  accepted silently, never federates, and the only symptom is a deployment
  that never appears — which the deep assert reports 40 minutes later as
  "deployment not found", pointing at the workload cluster instead of at the
  AppDeployment. Checking federation first turns that into a precise failure
  in under a minute. Live-diagnosed 2026-08-30: 0 instances,
  `status={"observedGeneration":1}`.
- Cleanup removes the workload cluster first, then the management cluster —
  or freezes it, if this run was the change-set's first. The frozen template
  is never touched.

## `kommander-upgrade`

The canonical row-3 scenario, and the reference for "can an NKP upgrade
silently put old code back?". The change is delivered to the older cluster
BEFORE the upgrade and asserted again after.

Source-verified facts it depends on (audit 2026-08-29, second pass; 21 of ~40
first-pass claims were refuted, so only survivors are used):

- **`nkp upgrade kommander` does NOT create an UpgradePlan.** Nothing in
  kommander, kommander-cli, konvoy2 or konvoy-cli ever creates one; the
  controllers only consume plans written by something else, and the only
  instances in the tree are kuttl fixtures. The CLI patches
  `NKPCluster.spec.version` directly
  (`kommander-cli/pkg/upgrade/upgrade_nkpcluster_helper.go:21-57`). Waiting on
  UpgradePlan conditions would hang forever on a healthy cluster.
- **It cannot touch nodes.** Across the whole kommander-cli repo exactly one
  non-test file imports `sigs.k8s.io/cluster-api`, and only to call
  `AddToScheme` (`cmd/upgrade/kommander/kommander.go:15-17`). No upgrader
  imports it at all — so `assert_nodes_unchanged` is a real invariant.
- **The dev-image hook SURVIVES the upgrade.** `<app>-overrides` is a standing
  `valuesFrom` entry in the shipped HelmRelease — present in release-2.17 (app
  0.17.2) and release-2.18 (0.18.1) alike, at the last/highest-precedence
  index, `optional: true`. No step in the upgrader list writes or deletes it;
  the only ConfigMap the configOverrides step applies is
  `kube-prometheus-stack-mgmt-overrides`
  (`kommander-cli/pkg/upgrade/mgmt_configoverrides_upgrader.go:31-33`).
- **Never prove a controller moved via an app version.** `kommander` and
  `kommander-appmanagement` are themselves platform apps, but their
  AppDeployment version is the k-apps APP version (0.18.0 on 2.18) and moves
  only when the app directory is bumped — while the controller image tag
  changes on every kommander build. Hence `assert_change_running`, which
  compares container images, never `assert_app_effective_version`.
- **`strict: false` is deliberate and must stay false here.** `strict` asserts
  that every ReplicaSet generation in history ran your image. That is the
  right invariant for a born-with template; it is false by construction on an
  upgrade test, because an older baseline legitimately ran shipped kommander
  before the change was delivered. Turning it on would fail every run AND mark
  the cluster tainted, which deletes it.
- The change is delivered with `deliver_changes` rather than
  `upgrade_kommander: {changes: [...]}` for one reason: so it can be asserted
  BEFORE the upgrade too. Proving the change was live first is what makes "the
  upgrade did not revert it" mean something.

Topology must match `ga-baseline`'s (1 control plane + 1 worker): the
change-set hash covers topology, so a different node count silently rebuilds
instead of claiming.

## `kommander-upgrade-existing`

Row 3 against a cluster **you** supply — the cheap re-run path. Point
`E2E_KUBECONFIG` at a cluster running a platform version below your CLI's.

- **This is not the reference; `kommander-upgrade` is.** This file upgrades
  first, then delivers the change, then runs `nkp upgrade kommander` a second
  time at the same version. That second run is not a no-op for our purposes —
  the version patch short-circuits
  (`upgrade_nkpcluster_helper.go:39-41`) but the platform upgrader still
  re-applies every HelmRelease from the app kustomize directories, and
  surviving that re-render is a real no-old-code property. But it proves only
  same-version re-render survival. `kommander-upgrade` proves survival across
  a real version-CHANGING upgrade (live 2026-08-30: dev image delivered at
  2.17, still running after the platform reached 2.18).
- The original reason for this ordering was a fear that a 2.18-line dev image
  on a 2.17 cluster would crashloop and destroy the baseline. Measured
  2026-08-30: it does not (ready 2/2, Running, 0 restarts). The ordering now
  simply makes the file usable against a cluster already at your CLI's
  version, where the canonical scenario would have nothing to upgrade.
- No `cleanup:` block — this scenario did not create the cluster, so it must
  not delete it. Sweep it yourself.

## `node-upgrade`

Row 4: konvoy2 / CAPX / CAREN. Rolls an older cluster's nodes to the CLI's
kubernetes and proves it stabilises — no kommander upgrade, no app waves.

- `upgrade cluster nutanix` **NO-OPs** if `topology.version` already equals the
  CLI's target (`konvoy2 cluster/upgrade.go:214-218`), which is why the
  baseline is k8s 1.34.1 against a 1.35.2 target.
- On a management cluster the same command upgrades the CAPI stack and
  re-applies the ClusterClass (`upgrade.go:226,416-463`), so a separate
  `upgrade capi-components` step would be a duplicate.
- `upgrade addons` does not exist for Nutanix / topology clusters
  (`addon/upgrade.go:44-59,115-119`) — addons ride this roll.
- `assert_platform_version_unchanged` is the other half of the matrix's
  separability claim: row 3 proves a platform upgrade leaves nodes alone, this
  proves a kubernetes-layer roll leaves the platform alone. Without both
  directions measured, "separable blast radii" is an assertion about the code
  rather than an observation of the cluster.

## `node-upgrade-existing`

Row 4 against a cluster you supply. Same assertions as `node-upgrade`; only
the way the cluster is obtained differs.

- The supplied cluster MUST be at a kubernetes version BELOW the CLI's target,
  or the command returns early and exits 0 having rolled nothing. Check first:

  ```
  kubectl get nodes
  kubectl get nkpcluster -A -o jsonpath='{.items[0].spec.capiCluster.topology.version}'
  ```

- `E2E_CLUSTER_NS` is the namespace of the **CAPI Cluster**, which is not
  always `kommander`. A cluster created with `--self-managed` and then upgraded
  still has its CAPI Cluster in `default`, and the wrong namespace makes the
  CLI fail with a flat "cluster not found". `upgrade_nodes` auto-detects it
  when unset, so the variable is a manual override rather than a requirement.
- `assert_nodes_upgraded` is what makes this falsifiable: it fails if every
  node still reports an old kubelet, which is exactly what a silent no-op
  looks like. It also re-checks that every node maps to a Machine the CAPI
  tree tracks — a roll is precisely where CAPX duplicate-name twins have
  appeared twice before.
- No `cleanup:` block, for the same reason as above.

## `platform-upgrade`

The whole platform: the row for a change whose reach you cannot bound, or the
release-shaped check that runs the customer's documented upgrade in order. It
is the most expensive scenario in the suite — reach for a narrower row when
the change is narrow.

- Baseline is the previous release, claimed from the template `ga-baseline`
  produces. Topology must match `ga-baseline`'s.
- Cleanup is `finish_cluster` alone. It deliberately KEEPS a cluster whose
  create did not finish (an hour of build is worth more than a tidy teardown)
  or whose freeze failed. Pairing it with `assert_no_leftover_vms` contradicts
  that — the assert fails on exactly the VMs `finish_cluster` was told to
  preserve, turning a kept-for-inspection cluster into a red cleanup.
  Live-caught 2026-08-29.

## `ga-baseline`

Not a test — a **factory** for the thing every upgrade row starts from. Every
upgrade scenario needs a cluster running the previous release, because the
upgrade verbs no-op when the cluster is already at the CLI's target.

**Status 2026-08-31: freeze works, claim does not.** `freeze.py` and
`claim.py` now know the 2.17 layout — 2.17 has no `NKPCluster` CRD and keeps
its CAPI Cluster in namespace `default`; ~40 call sites were version-gated. A
2.17 baseline therefore builds and freezes (proven live 2026-08-30, 50.8 min,
gates green). Claiming one back does not yet converge: after four attempts the
SSO chain re-renders `kube-oidc-proxy-config/oidc.issuer-url` back to the
template load balancer and three Flux Kustomizations never reconcile.
Change-set `2c0e1c36` is deliberately UNREGISTERED so nothing silently claims
a broken template.

Consequence: the shared-template design is not in effect. `node-upgrade`,
`platform-upgrade` and `kommander-upgrade` each build their own baseline.
Running this file today gets you a healthy previous-release cluster kept for
reuse via `use_existing_cluster` — which is what the `*-existing` scenarios
consume, and they finish in 14–16 minutes.


## Checking your scenario

```bash
./run_e2e.py my-scenario --dry-run    # full plan, every command printed, no cluster
./selftest.py                         # framework self-tests incl. all scenario files load
```

Dry-run catches unknown steps, bad options and unparseable YAML in seconds.
Make it your pre-commit habit.

## Adding a step

One function in `framework/steps.py`. Keyword-only arguments become the YAML
options; the first docstring line is what `./run_e2e.py --steps` shows. Use
`ctx.kube`, `ctx.nkp`, `ctx.pc`, `ctx.log`, `ctx.remember`/`ctx.recall` rather
than raw `subprocess`, so `--dry-run` works and secrets stay redacted.

```python
@step("assert_my_thing")
def assert_my_thing(ctx, *, name: str, namespace: str = "kommander", timeout: str = "10m") -> None:
    """One line saying what this proves."""
    if ctx.config.dry_run:
        return
    def ready() -> bool:
        return (ctx.kube.json(["get", "mything", name, "-n", namespace]).get("status", {}).get("phase") == "Ready")
    wait_for(ready, ctx.log, what=f"{namespace}/{name} to be Ready", timeout_s=parse_duration(timeout), dry_run=False)
```
