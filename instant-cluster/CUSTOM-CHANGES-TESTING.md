# Testing custom changes on an instant cluster — the continuous-delivery design

An instant (cloned) cluster reproduces the frozen template exactly — by itself it cannot test a
code change. This document is the design for how developers test their changes **on** an instant
cluster anyway, and where the boundaries are.

## The delivery chain (how anything lands in NKP) — read this first

Verified against the repos (kommander, kommander-applications, charts, CAREN, konvoy2,
kommander-cli), because every fast-test vector below is an interception point on this chain:

```
konvoy2 (nkp CLI) ──creates──> CAPI providers (incl. CAREN) as OCI bundles
                                 via the CAPI-Stack operator          [pins: pkg/capi/client/providers/providers.go]
kommander-cli (nkp install) ──fetches──> kommander-applications tarball
                            ──installs──> git-operator + flux + kommander-operator
kommander-operator (in-cluster) ──downloads──> kommander-applications OCI artifact
                                ──git-pushes──> the LOCAL kommander.git   [managementplane/pkg/step/deploy_operator.go]
flux (in-cluster) ──reconciles ONLY from──> the LOCAL kommander.git       [GitRepository "management", ns kommander-flux]
each app's HelmRelease ──pulls chart from──> ghcr.io/mesosphere/charts (OCI)
CAREN addons (cilium/CSI/CCM/...) ──HelmChartProxy──> oci://helm-repository.caren-system.svc/charts
                                                       (PVC seeded by a bundle-initializer image)
```

Two facts make fast testing possible:
1. **Flux reconciles from the cluster's own local git repo** — never from GitHub. Whoever writes
   that repo controls the platform's desired state.
2. **The frozen template amortizes the CLI away** — `nkp create` and `nkp install kommander` run
   once at template-build time; claims never run them. CLI changes only matter when the template
   itself must change.

## The change-classification matrix

| You changed | Where it lives at runtime | Fast-test vector | Time |
|---|---|---|---|
| Controller Go code (kommander-operator, appmanagement, CAREN, CAPX, git-operator…) | a container image | build ONE image → `override_image.py apply` | ~3 min |
| App config / versions / new app (kommander-applications) | content of the local `kommander.git` | git-push the changed app dirs into the claim's own repo (same write-path `git_hydrate.py` already uses); **suspend the kommander-operator's git re-push first** (it re-seeds from the OCI artifact on reconcile — scale `kommander-operator` to 0 or it will overwrite you) | ~2 min |
| An app's Helm chart (charts repo) | OCI chart the HelmRelease pulls | push dev chart to any OCI registry (even ttl.sh) → git-push the `OCIRepository` URL/tag bump via the vector above | ~4 min |
| CAREN addon chart or values (cilium/CSI/CCM…) | `HelmChartProxy` CRs | patch the HCP `valuesTemplate`/version (override_image.py's CAAPH layer already does values; version+repo is one more patch) | seconds |
| ClusterClass / CAREN handler behavior | ClusterClass objects + the CAREN image | image override + apply the re-rendered ClusterClass — **this intentionally triggers the rollout you are testing** | ~5 min |
| konvoy2 CLI / provider pins / embedded templates | only the create flow | cannot be intercepted on a running cluster — rebuild binary, build a new template (~30 min, once), which every claim then inherits | ~30 min once |

**The git-push vector — VALIDATED LIVE (qa-n7, 2026-08-05):** edit inside the claim's git pod →
push → flux fetch → object changed in-cluster in **18 seconds** (~30s including the operator
suspend). Two traps found live, both now part of the recipe:

1. **HR `metadata.labels` cannot carry your change.** The AppDeployment machinery injects a JSON
   patch (`op: add` on `/metadata/labels`) into each app's flux Kustomization, and JSON-patch `add`
   on an existing key REPLACES the whole map — any label you add in git is wiped at build time,
   silently (flux reports success, no diff). Test through `spec` fields or the app's `cm.yaml`
   defaults instead.
2. **The per-app Kustomizations live in namespace `kommander`** (not `kommander-flux`) **with a 6h
   reconcile interval** — after pushing, annotate the app's own Kustomization
   (`reconcile.fluxcd.io/requestedAt`) or your change waits hours.

Also verified: the git pod's shell is busybox (no GNU grep flags), and `kommander-appmanagement`
does not need to be stopped for spec-field changes — only `kommander-operator` (the OCI re-pusher).

**Baking:** when a delta is validated and the team wants every subsequent cluster to carry it,
freeze the modified claim — it becomes the next template generation (~7 min). Template
generations are proven live (original build → g3 → claims from g3). This is the "change the
frozen template" idea done safely: never edit a frozen artifact; edit a live claim and re-freeze.

**Concurrency:** every developer claims their own cluster from the shared template (concurrent
claims are proven isolated — disjoint VMs/VIPs/LBs/VGs); all vectors above touch only the claim's
own keyspace/git/registry objects. Nothing here writes to the template or to shared state.

## The observation that makes it possible

Everything NKP ships runs as a container image (75 distinct images across 82 workloads on a
single-node cluster). A change to one component is testable by swapping that one component's
image — the other 74 images stay exactly as the template froze them.

## The developer loop

```
build the one changed image (~2 min)  →  push it  →  claim an instant cluster (~10 min)
  →  override_image.py <kubeconfig> apply <component>=<repo:tag>     (seconds)
  →  test against a real, full NKP cluster
  →  override_image.py <kubeconfig> revert                            (seconds)
```

Total: ~12 minutes to "my change is running in a real cluster", versus ~26 minutes for a full
from-scratch build — and the same claimed cluster can be reused across iterations (override →
test → revert → override the next build).

## Why one tool needs three strategies

A naive `kubectl set image` gets undone: the platform is self-healing and re-applies each
component's master settings. The override must land at each component's **master settings**, and
NKP manages images at three different layers (all validated live):

| Layer | Components | Mechanism | Notes |
|---|---|---|---|
| kommander apps | the 22 platform apps (dex, traefik, reloader, ...) | write the app's `<app>-overrides` ConfigMap (a designed hook, outside git — flux cannot revert it) | the image values-key is resolved automatically from the chart's own template stored in helm release storage |
| CAAPH components | cilium, CCM, CSI, autoscaler, cosi, konnector | patch the HelmChartProxy `valuesTemplate` | multi-image charts (cilium) need `component@subimage=` targeting; a bare apply refuses with the sub-image list |
| controllers | CAPX, CAPI, CAREN, kommander-operator, git-operator | patch the Deployment image directly | capi-operator does not guard provider Deployments (its declarative override is inert under NKP's bundle serving); `--hold-operators` scales it to 0 for a bulletproof hold |

Core Kubernetes images (apiserver/etcd/…) are a fourth layer — on-disk static-pod manifests —
already edited by the claim pipeline itself.

Supporting behaviors, all validated:
- `images <component>` lists a component's sub-images before overriding.
- Originals are stored in-cluster (`kommander/speedstart-image-overrides`), so revert works from
  any machine.
- A bad image degrades gracefully in most cases: the rollout keeps the old pod serving until the
  new one is Ready. (Exact behavior follows each Deployment's own rollout strategy.)
- `--pull-secret <registry>=<user>:<password>` creates a docker-registry Secret and attaches it
  to the component's ServiceAccount so private dev images can be pulled; detached and deleted on
  revert. (Mechanics validated; end-to-end private pull still to be exercised against a real
  private registry.)

## What this deliberately does NOT cover

Image swap tests a change **inside** a component. It cannot test:
- new or changed CRDs / API schemas
- Helm chart / packaging / RBAC / flag changes
- brand-new components

For those, the path remains: build the bundle (or rebuild + re-freeze the template with the
change baked in). That is by design — the image override optimizes the most common dev loop
(controller code changes) without pretending to replace full-build validation.
