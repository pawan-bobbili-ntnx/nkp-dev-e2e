# Assertion map — what the framework should be able to prove, and for whom

Written 2026-09-07 from the repositories on disk, the CRDs on a live 2.18.1 cluster,
the `nkp` verb tree, and what the twelve live scenarios actually assert. The
question it answers: **a developer changes X — what must be observed on a real
cluster before that change is believed?** Everything below is an observable
object or field, not a feeling.

Numbers that frame it: 79 steps today, 29 of them assertions. **18 of the 29 are
used by no live scenario.** `assert_jsonpath` is used nine times as an escape
hatch — four on Deployments, four on HPAs — which is the clearest signal of
assertions that want a name.

---

## 1. The ecosystem, as layers a change lands in

| layer | what it is | repos | the objects that carry its truth |
|---|---|---|---|
| **L0 infrastructure** | VMs on Prism Central; CAPI adoption of them | `cluster-api-provider-nutanix` (CAPX), CAPI core, `cluster-api-addon-provider-helm` | `NutanixCluster/Machine/MachineTemplate/FailureDomain`, `Cluster/ClusterClass/Machine/MachineDeployment/MachineHealthCheck/KubeadmControlPlane`, the VM itself on PC |
| **L0½ runtime extension** | how a cluster is *shaped*: CAREN mutates kubeadm/CAPX from topology variables and installs addons | `cluster-api-runtime-extensions-nutanix` | `ExtensionConfig`; per-provider mutation handlers (`controlplaneendpoint`, `controlplanevirtualip`, `prismcentralendpoint`, `machinedetails`, `failuredomains`); lifecycle addons as `HelmChartProxy/HelmReleaseProxy` (cni calico/cilium/multus, csi nutanix/localpath/snapshot, ccm, metallb, registry, nfd, cosi, cluster-autoscaler, konnectoragent, ingress) |
| **L1 CLI** | the customer's hands | `konvoy2` (`create/upgrade cluster nutanix`, `nodepool`, `scale`, `check cluster`, `diagnose`), `kommander-cli` (`install/upgrade kommander`, `upgrade workspace/catalogapp`, `attach/detach`, `workspace/project`, `get dashboard`) | exit codes, the objects each verb writes, and the *waits* each verb performs |
| **L2 platform operators** | version carriers | `kommander` (`cmd/`) | `ManagementPlane` (conditions: `KAppsOCIRepositoryReady`, `*OperatorReady`, `Reconciled`), `NKPCluster` (`CAPIClusterAdopted`, `CAPIClusterTopologyUpToDate`, `KommanderClusterReady`, `PlatformAppsReconciled`), `KommanderCore` (`InstallSucceeded`, `CoreAppDeploymentsDeployed`, …), `CAPIStack`, `LoggingStack` |
| **L3 kommander controllers** | ~110 reconcilers | `kommander` | app lifecycle (`AppDeployment`, `AppDeploymentInstance`, `ClusterApp`, `App`); cluster membership (`KommanderCluster` → flux, kubefed, tunnel, observer, karma, platform-version); tenancy (`Workspace`, `Project`, federated namespaces, kustomizations); RBAC (`VirtualGroup*`, `Kommander{Workspace,Project}Role`); identity (dex, dex-k8s-authenticator, TFA, kube-oidc-proxy); catalogs (`GitRepository`/`OCIRepository` apps); `UpgradePlan`; license; insights |
| **L4 delivery** | how content reaches clusters | `kommander-applications` (60 apps: 37 `nkp-core-platform`, 24 `internal`; scope 57 workspace / 4 project; 15 with `requiredDependencies`), `charts` (35 staging / 42 stable), `flux-oci-mirror` | flux `OCIRepository/GitRepository/HelmChart/HelmRelease/Kustomization`; kubefed `Federated*` + `KubeFedCluster`; git-operator `GitClaim`; the OCI bundle a version installs from |
| **L5 workloads** | what the customer sees | (rendered) | Deployments, HPAs, Services/LoadBalancers, the dashboard behind traefik+dex |

A change in one repo is *believed* only when its effect is observed at the
layer where it becomes customer-visible, **and** the layers it passed through
are shown not to have swallowed it (a rendered chart is not a delivered one;
an `AppDeployment` that is "Ready" is not an app that ran).

---

## 2. Change classes → what to observe → where the framework stands

Legend — **have**: a step exists and is used; *unused*: exists, no live scenario
uses it; **gap**: nothing observes it. Options in `{}` are what the step should
accept.

### 2.1 `konvoy2` — the cluster CLI

| change | observe | status |
|---|---|---|
| `create cluster` flags / defaults | node count, roles, kubelet version, machine image on every `NutanixMachine`, CP endpoint | **have** `create_cluster`, `wait_nodes_ready`, `assert_node_count`(*unused*); **gap** `assert_machine_image {image}` — the image actually on each VM, from PC, not the flag you passed |
| `upgrade cluster nutanix` (node roll) | every node's kubelet + image moved, uids changed, KCP/MD rollout complete, platform NOT touched | **have** `upgrade_nodes`, `assert_nodes_upgraded`, `assert_platform_version_unchanged` |
| `--skip-preflight-checks` family (**6 blockers, 121 days, NCN-113929…117277**) | preflight *not* run when told; `UpgradePlan` reflects it; nodepool creation honours it | **gap** — `upgrade_nodes {skip_preflight: [Registry, …]}`, `create_nodepool {skip_preflight}`, `assert_preflight_skipped {check}` reading the CLI's own output and the `UpgradePlan` conditions. A table-driven scenario retires this whole family |
| nodepool lifecycle | `MachineDeployment` per pool: replicas, ready, k8s version, machine template (cpu/mem/disk/gpu/categories/project) | **gap** `create_nodepool {name, replicas, vcpus, memory, disk, gpu, k8s_version, pc_project, pc_categories}`, `scale_nodepool`, `delete_nodepool`, `assert_nodepool {name, replicas, ready, version}` |
| `nkp check cluster` / `diagnose` | the CLI's own health verdict; a bundle collected | **gap** `assert_cluster_check` (wrap `nkp check cluster`, fail on its non-zero); `collect_diagnostics` **have** |
| VIP / LB pool / DHCP | API reachable on the VIP; LB addresses answer HTTP (not just ICMP — MetalLB does not answer ping) | **have** `assert_api_reachable`; **gap** `assert_lb_serves {service, path, expect: 302}` — today's dashboard episode had nothing to catch it |

### 2.2 CAPX — the infrastructure provider

| change | observe | status |
|---|---|---|
| VM shaping | vcpus/memory/disk on the **PC VM** equal the requested values; categories/project applied | **gap** `assert_vm_shape {role, vcpus, memory, disk}` via the PC API — the CAPX→PC contract, otherwise only visible when a customer's VM is wrong |
| failure domains | control-plane machines spread across `NutanixFailureDomain`s | **gap** `assert_failure_domain_spread {min_domains}` |
| machine phases | all `NutanixMachine` Provisioned, `providerID` set, no `MachineHealthCheck` remediation looping | **gap** `assert_machines_provisioned`; `assert_remediated {node}` after `delete_vm {node}` on PC (MHC really replaces it) |

### 2.3 CAREN — runtime extension and addons

| change | observe | status |
|---|---|---|
| a mutation handler (CP endpoint, PC endpoint, machine details, VIP) | the rendered kubeadm/CAPX object carries the value; `NKPCluster.CAPIClusterTopologyUpToDate=True` | **gap** `assert_topology_uptodate`; `assert_cluster_variable_applied {variable, kind, name, path, equals}` — one topology variable in, one rendered field out (cilium `replicas: 1` via `CNI.AddonConfig.Values.SourceRef` is the worked example) |
| a lifecycle addon (cni/csi/ccm/metallb/registry/nfd/cosi/autoscaler) | its `HelmReleaseProxy` Ready at the pinned **chart version**; its values contain the change | **gap** `assert_addon {name, chart_version, values_contain}` — reads `HelmChartProxy`/`HelmReleaseProxy` — this is the "CAREN chart upgrade is not an image swap" row of the blast-radius matrix, currently proven by hand |
| registry mirror / airgap (60 apps carry `certifications: airgapped`) | every running image's registry host is the mirror | **gap** `assert_images_from {registry}` — reads `pod.status.containerStatuses[].imageID`; the cheapest real airgap regression test there is |
| node-level config (containerd, kubelet flags) | the file on the node | **gap** `run_on_node {role, cmd}` / `assert_node_file {path, contains}` over `E2E_SSH_*` |

### 2.4 `kommander` controllers

**App lifecycle** (`appdeployment`, `appd_instance`, `kommandercluster_appdeployments`, `project_appdeployments`, `workspace_default_appdeployment`) — the best-covered area: **have** `deploy_app`, `delete_app`, `assert_app_effective_version`, `assert_app_config_applied`, `assert_app_on_cluster` (`contentHash` vs `observedContentHash`), `assert_app_instances`, `set_app_version_pin`. Gaps:

| observe | proposal |
|---|---|
| uninstall is complete: HelmReleases, namespace, CRDs the app owned are gone | `assert_app_absent {app, cluster}` — `delete_app` today proves nothing after it |
| `requiredDependencies` enforced (15 apps declare them; istio without kube-prometheus-stack sits at `observedGeneration -1` forever — live-caught) | `assert_app_blocked_on_dependency {app, dependency}` — the negative case, so a controller change that *stops* enforcing it is caught |
| scope enforced (4 apps are project-scoped) | `assert_app_scope_rejected {app, scope}` |
| every pin resolves to an existing `ClusterApp` (**today's root cause**) | `assert_app_pins_resolve` — the `_unresolvable_pins` logic as a step, usable before *and* after any upgrade |
| multi-HelmRelease apps (istio renders five) all Ready | `assert_app_renders {app}` — every HR named `<app>` or `<app>-*` Ready; `assert_helmrelease_ready` is single-name |
| the merged values (defaults CM + override CM) contain a key | `assert_app_values {app, path, equals}` — the `valuesFrom` mechanism itself, not just one HPA field |

**Cluster membership** (`kommandercluster_*` ×16, `autoattach_capicluster`, `nkpcluster_*`) — **have** `create_workload_cluster`, `delete_workload_cluster`, `switch_cluster`; *unused* `wait_cluster_attached`. Gaps:

| observe | proposal |
|---|---|
| `nkp attach cluster` of an **external** kubeconfig (a different code path from create) | `attach_cluster {kubeconfig, name, registry_url?}`, `detach_cluster` |
| `KommanderCluster` conditions: Joined, kubefed Ready, tunnel/observer/karma, platform version synced | `assert_kommandercluster {name, conditions: [...]}` |
| a federated member is reachable and propagation works (**today's second defect**) | `assert_federation_healthy` — every `KubeFedCluster` Ready and claimed by a `KommanderCluster`; `Federated*` objects `PropagationSucceeded` |
| platform apps fan out to a newly attached cluster | `assert_platform_apps_on_cluster {cluster}` — `AppDeploymentInstance` per platform app converged |

**Tenancy and RBAC** (`workspace_*` ×8, `project_*` ×6, `virtualgroup_*` ×8, `*_role` ×6) — **nothing exists.** These are a third of kommander's reconcilers.

| observe | proposal |
|---|---|
| a Workspace materialises: namespace, kustomization, federated namespace on every member | `create_workspace {name}`, `delete_workspace`, `assert_workspace {name, on_clusters}` |
| a Project materialises on selected clusters; project-scoped app deploys there | `create_project {name, workspace, cluster_selector}`, `assert_project_on_clusters` |
| a VirtualGroup + role → a RoleBinding on the member cluster (the *whole* RBAC chain) | `assert_rbac_chain {group, role, cluster}` |

**Identity** (`dex_config`, `dexk8sauthenticator_clusters`, `dextfaclient`, `kubeoidcproxy_federated_clients`, `tfa_federated_clients`) — **nothing observes the login chain.** The dashboard was unreachable for an hour today and no assertion would have said so.

| observe | proposal |
|---|---|
| `nkp get dashboard` URL answers, redirects to dex, dex login page renders (302 → 200 `Nutanix | Log In`) | `assert_dashboard_serves` — HTTP, `-k`, follows redirects, asserts the final title |
| a real login with `dkp-credentials` reaches the UI | `assert_dashboard_login` — the full traefik → TFA → dex → UI chain, one request sequence |

**Catalogs and content sources** (`*catalog*` ×6, `ocirepository_*` ×3, `gitrepository_apps`) — *unused* `assert_kapps_collection_synced`, `assert_kapps_bundle_source`. Gap: `create_catalog {url}` + `assert_catalog_app_available {app, workspace}` (an `App` appears after a catalog is added).

**UpgradePlan / version carriers** (`upgradeplan_*`, `nkpcluster_platform_apps`, `managementplane`, `kommander_core`) — *unused* `record_platform_carriers`, `assert_platform_upgraded`, `assert_platform_changed`, `assert_platform_apps_moved`, `assert_platform_operators_at_version`. These exist and are the right assertions for `platform-upgrade.yaml`; they should be in it. Gap: `assert_upgradeplan {conditions}`.

**Logging / license / insights** — `assert_loggingstack {version}` (four conditions on the CR), `assert_license`. Low priority; cheap.

### 2.5 `kommander-applications`

**have** `deliver_changes` (git-sync + bundle), `record_chart_refs` / `assert_chart_refs_changed`, `publish_kapps_artifact`; *unused* `assert_app_metadata`. Gaps are the app-shape ones in 2.4: `assert_app_renders`, `assert_app_values`, dependency and scope negatives, and `assert_kapps_app_versions {app: [versions]}` (the bundle carries what the operator will pin — the check that would have named today's failure in seconds).

### 2.6 `charts`

**have** `publish_chart`, `assert_chart_refs_changed`, and `assert_jsonpath` ×9 doing the real work. Name the four things it is used for:

| today (`assert_jsonpath`) | proposal |
|---|---|
| `deployment … .metadata.annotations.X == Y` | `assert_workload_annotation {kind, name, namespace, key, equals}` |
| `deployment … containers[0].resources.requests.memory` | `assert_workload_resources {kind, name, container, requests/limits}` |
| `hpa … .spec.minReplicas` | `assert_hpa {name, namespace, min, max}` |
| (implied) image of a container | `assert_workload_image {kind, name, container, image|contains}` |

Keep `assert_jsonpath` as the escape hatch; add `matches:` (regex), `contains:`, `gte:` — today it only has `equals:`.

### 2.7 Day-2 and resilience (all repos)

**have** `cordon_node`, `drain_node`, `uncordon_node`, `restart_workload`, `scale_workers`, `reset_bootstrap`. Gaps: `delete_vm {node}` + `assert_remediated` (MHC), `power_off_vm`/`power_on_vm` (PC chaos), `assert_cluster_autoscaler {scale_from: 0}` (a CAREN addon nobody exercises), `assert_pdb_respected` during a roll.

---

## 3. Options every step should offer (the contract)

Consistency is what makes a scenario readable by someone who did not write it.

| option | on | meaning |
|---|---|---|
| `timeout:` | every assertion and wait | polls until then; **never restate the default** (selftest rejects it) |
| `cluster:` | every assertion | `management` (default), `@workload` (the scenario's attached cluster), or a `KommanderCluster` name — read **from the management cluster** where the object is federated (`AppDeploymentInstance`), switch kubeconfig only where the object truly lives on the member |
| `namespace:` / `workspace:` | app and workload assertions | `workspace:` resolves to the namespace (they differ: `default-workspace` ↔ `kommander-default-workspace`) |
| `equals:` \| `contains:` \| `matches:` \| `gte:` \| `absent: true` | value assertions | one matcher per step; `absent` is the negative form |
| `moved: true\|false` / `was: <key>` | before/after assertions | pair with `record_*`; `moved: false` is a first-class negative (containment) |
| `count:` / `min_count:` | fan-out assertions | instances, clusters, nodes |
| `changes: [repo@ref]` | anything that delivers code | the change-set; a branch resolves to a commit; the commit **is** the tag |
| `version:` / `to_version:` | anything that moves a version | required where omission would silently pick "current" (`deploy_app` live-caught) |
| `skip_preflight: [..]` | `upgrade_nodes`, `create_nodepool` | the CLI flag, table-driven |
| `config_values:` / `config_overrides:` | `deploy_app` | the UI's mechanism, not a shortcut |
| `wait: true\|false` | actions | default true; false only when a later assertion owns the wait |

Rules that fall out of the live-caught bugs (all in `docs/RUNBOOK.md` §3): assert the object that *carries* the change; record before you change; a `Ready` condition on the wrong object is a vacuous green; startup-only log lines rotate away; MetalLB VIPs do not answer ping; pins must resolve to an existing `ClusterApp`.

---

## 4. Where to start (ordered by real misses avoided per hour of work)

1. **`assert_app_pins_resolve`** and **`assert_kapps_app_versions`** — today's 3×40-minute stall, as a 5-second named assertion, usable before any upgrade. *(logic already exists: `_unresolvable_pins`.)*
2. **`assert_federation_healthy`** — the orphan-member defect, as an assertion (the heal exists; the assertion makes a scenario say it).
3. **`upgrade_nodes {skip_preflight}` + `assert_preflight_skipped`** — the six-blocker, 121-day JIRA family; one table-driven scenario retires it.
4. **`assert_dashboard_serves`** — the customer-visible chain; an hour of today would have been a one-line failure.
5. **`assert_app_renders` + `assert_app_values`** — multi-HelmRelease apps and the `valuesFrom` merge; replaces four `assert_jsonpath` uses and the single-HR blind spot.
6. **Tenancy trio** `create_workspace` / `create_project` / `assert_rbac_chain` — a third of kommander's reconcilers, zero coverage.
7. **`assert_addon` + `assert_cluster_variable_applied`** — CAREN's two halves, currently hand-verified.
8. **`assert_images_from`** — airgap in one assertion.
9. **`create_nodepool` / `assert_nodepool` / `assert_vm_shape`** — the konvoy2/CAPX contract with PC.
10. **Put the five unused platform assertions into `platform-upgrade.yaml`** — they exist; the scenario that needs them does not use them.

Not proposed: anything that re-tests Kubernetes, flux, or Helm themselves. The
framework proves *NKP's* wiring of them.
