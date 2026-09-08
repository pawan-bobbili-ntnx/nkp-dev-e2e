# NKP Instant Single-Node Cluster

Claim a fully converged, day-2-safe, single-node NKP management cluster in **~10 minutes**
(traditional creation: ~26–40 min) by cloning a frozen golden template and re-binding only its
*identity* — never its content.

Full architecture, timing breakdowns, the 10-minute floor analysis, and the template-vs-clone
difference table live in [`INSTANT-SINGLE-NODE-CLUSTER.md`](INSTANT-SINGLE-NODE-CLUSTER.md).

## Results (measured, zero-intervention)

- Claim: **10m09s best / ~10.5 min median** over 5 clean runs; every claim ends with hard parity gates.
- Day-2 proven live on a claimed clone: worker scale-out, machine deletion, **full control-plane roll**
  (etcd learner join against the re-formed member, promote, VIP failover).

## Prerequisites

- A frozen golden template on Prism Central (see *Template lifecycle* below).
- `kubectl`, `python3`, `ssh` on the operator machine; SSH key authorized on the template image.
- The template's admin kubeconfig available as `<template-prefix>.conf` (see `NKP_TEMPLATE_KC_DIR`).
- Prism Central credentials **of the claiming user** — verified pre-flight; used for every PC
  operation and written into the clone's CCM/CSI/CAPX secrets (per-user resource attribution).

## Quickstart

```bash
export NKP_NUTANIX_USER=<your-pc-user> NKP_NUTANIX_PASSWORD=<your-pc-password>

python3 claim_boc.py <template-prefix> <cluster-name> <template-vip> <new-vip> <lb-start> [lb-end]
# e.g.
python3 claim_boc.py qa-sn-tmpl1 qa-team-alpha <lb-address> <lb-address> <lb-address> <lb-address>
```

Per-claim inputs: a unique cluster name, a free VIP, a free ingress/LB IP. Concurrent claims from
one template are supported. The command is fully unattended and exits non-zero (with the failing
phase named) rather than ever shipping a subtly broken cluster.

## Why boot-once-correct (the only claim path)

`claim_boc.py` performs ALL identity surgery **at rest**: one kubelet window edits the disk,
re-forms etcd, and byte-replaces VIP/LB/providerID through the full source-of-truth chain
(NKPCluster -> Cluster -> NutanixCluster -> KCP -> KubeadmConfig -> Node). The cluster therefore
converges exactly once, already believing its new identity.

An older ONLINE path (`claim.py` + `stage_b_claim.py` + `reident.py`) fixed identity on a running
cluster and was ~4 min faster. **It was retired on 2026-07-27** because it renamed each clone's VMs
to the TEMPLATE's machine names: with N concurrent clones that produces N+1 identically-named VMs,
separable only by UUID — and CAPX's delete name-guard then wedges day-2 operations. Boot-once-correct
keeps unique clone VM names, which is what makes many concurrent claims from one template safe.
Concurrency outranks the wall-clock difference.

Note: `freeze-sn.py` freezes templates with kubelet DISABLED (clones boot inert); the claim
re-enables it during surgery.

## Testing code changes: dev-image overrides (`override_image.py`)

A claimed cluster reproduces the frozen template — it cannot test a code change by itself. But
every NKP component ships as an image, so a change is testable by swapping ONE image:

    override_image.py <kubeconfig> apply capx=myrepo/capx:pr-1234 reloader=my-tag
    override_image.py <kubeconfig> revert          # restore everything (state kept in-cluster)

Three strategies, resolved automatically and all validated live: kommander apps use the designed
`<app>-overrides` valuesFrom ConfigMap (outside git — flux cannot revert it; the image values-key
is auto-resolved from the chart's own template in helm release storage); CAAPH components patch
the HelmChartProxy valuesTemplate; controllers patch the Deployment directly (capi-operator does
not guard it; `--hold-operators` scales it to 0 for a bulletproof hold). A bad image degrades
gracefully — the rollout keeps the old pod serving. Dev loop: build+push one image (~2 min) +
claim with override (~10 min) ≈ ~12 min to your change running in a real cluster, vs ~26 min for
a full from-scratch build — and the template needs no re-freeze.

## Template lifecycle (paid once)

1. Create the template cluster traditionally with the `--speed-start-template` flag
   (ships in this same change): control-plane taints are cleared and replicas default to
   1 control plane / 0 workers. Full command in the architecture doc, §1.
2. Freeze it: `python3 freeze-sn.py <cluster-prefix> <kubeconfig>` — right-sizes single-node
   replicas, suspends the git-operator Kustomization, snapshots the git volumes under the exact
   names the claim restores from, pauses the CAPI cluster, disables cloud-init, powers off.
   The parked OFF VM **is** the golden image.

**Scope.** The single-node pipeline (`claim_boc.py`) assumes one control-plane node
and no workers — the topology the speed-start template ships. The multi-node (3CP+2W) pipeline now
lives here too (`claim.py` and friends, see below); the two are the **same** boot-once-correct
architecture and are being merged into one node-agnostic set.

## File map

One pipeline, node-count agnostic. Read top to bottom — this is execution order.

| # | Stage | File | What it does |
|---|---|---|---|
| 0 | validate | `preflight.py` | Read-only pre-claim checks: claim name is RFC 1123, PC reachable, ssh key, template artifacts complete (incl. `old_disk` on every golden RP), template VMs OFF **and its control plane actually dead**, VIP/LB free (probed with `/healthz`, not ping), nothing else in flight. Wired into every run wrapper; `SKIP_PREFLIGHT=1` overrides. |
| 1 | freeze *(once per template)* | `freeze.py` | Converged cluster → claimable golden template: verify green → capture git bundles → quiesce → pause → mint golden Recovery Points → **identity gate** → record frozen VM UUIDs → power OFF (UUID-scoped) → **liveness post-condition**. |
| 2 | clone + boot | `clone.py` | Clones the VMs named by the freeze manifest's UUIDs, powers them on in parallel, and fixes machine identity at rest: `/etc/machine-id`, iSCSI IQN, providerID custom-attribute. Writes `claim-map-<claim>.json`. |
| 3 | claim | `claim.py` | The driver. Restores golden RPs in parallel with stage 2, opens the **window** (staged etcd + a webhook-free apiserver, no kubelet), performs all identity surgery at rest, then closes the window and starts the kubelets exactly once. |
| 3a | ↳ window surgery | *(inline in `claim.py`)* | etcd re-form, keyspace byte-replace, node addresses, VolumeAttachment cleanup, git-at-rest pinning. |
| 3b | ↳ helpers invoked by the window | `refresh-creds.py` | Rewrites CCM/CSI/CAPX credential secrets to the claimer's PC credentials. |
| 3b | | `rebind_incluster_vip.py` | VIP reference rewriter. Pass 1 (`REBIND_TARGETED=1`): the 8 known plain-text carriers, DaemonSets first. Pass 2: full scan incl. secrets + HelmChartProxies. |
| 3b | | `remint_machinenames.py` | Claim-prefixed Machine names via rename-by-recreate; re-points child ownerReferences before deleting (GC-cascade guard). |
| 3b | | `rename_vms_to_machines.py` | Aligns Prism VM names to Machine names (CAPX name-guard = day-2 delete safety). Refuses any rename that would create a duplicate name. |
| 3b | | `remint_kubeadmconfig.py` | Re-mints KubeadmConfig so cloned CP machines don't roll on the new VIP. |
| 4 | post-boot | `post_boc.sh` | Everything that needs a live kubelet: wrinkle-2 sweep, flux resume, LEVER-A nodeSelector pin, MHC unpause. Holds **the one topology branch** (see below). |
| 4a | ↳ | `git_hydrate.py` | Rebuilds the git-operator repo from the freeze-time bundles; skips if already correct. |
| 4b | ↳ | `rightsize_hcp.py` | Single-node right-sizing (cilium-operator / csi-controller replicas → 1). |
| 5 | verify | `gates.py` | Parity gates (8-join / 11 / 11.5 / 12) against `claim-map-<claim>.json`. Day-2 assertion switches on `BOC_SKIP_VM_RENAME`. |
| — | cleanup | `teardown_claim.py` | Map-UUID-scoped teardown. Refuses to report success it cannot verify. |
| — | dev tools | `measure_claim.py` | Uniform claim timings → `<claim>-metrics.json`. |
| — | | `override_image.py` | Run a pushed component image on a claimed cluster in seconds (apply/revert/status). |

### Dropped (2026-07-29): superseded standalones

Three files that shipped with earlier iterations were **invoked by nothing** (verified by reference
search) and have been removed — "nothing extra" is a review criterion here. Backups live in the
durable mirror (`speedstart-state/unwired-backup/`) should any of them be wanted again:

- `lb_rebind_lean.py` — LB rebind is done inline by `claim.py` (DELTA-2) + `post_boc.sh`.
- `vg_restore.py` — superseded by `_early_vg_restore()` inside `claim.py`.
- `singlenode_profile.py` — day-0 override CMs for apps (rook-ceph / harbor / knative) that are not
  deployed by default; the 21 default AppDeployments converge green on one node without it. The two
  components that genuinely deadlock at N=1 are handled by `rightsize_hcp.py`. Re-introduce only
  when QA actually enables those apps on a single-node cluster.

### Concurrency

Claims are concurrency-safe by construction, and re-verified after every change to the window:

- **Every artifact is namespaced by claim name** — `claim-map-<NAME>.json`, `seed-db-<NAME>.gz.b64`,
  `<NAME>-window.conf`, `<NAME>-claim.conf`. A fixed filename anywhere here silently cross-wires two
  claims; that bug class is why the online path was retired.
- **The window is per-clone.** `kx()` talks to `WKC`, which points at *that clone's own* window
  apiserver over *that clone's own* etcd. So even a cluster-wide mutation like the conversion-webhook
  bypass touches only the claim's own keyspace.
- **Destructive PC operations are UUID-scoped, never name-scoped** (`freeze.py` power-off,
  `teardown_claim.py`). CAPX names day-2 replacement VMs after the ClusterClass, so a name filter
  matches other live clusters' VMs.
- **`preflight.py` warns rather than fails** when another claim is in flight — concurrent claiming is
  supported; it only invalidates *timing measurements*.

Proven live (2026-07-28, qa-sn-c7 ∥ qa-sn-c8, both from the one frozen template, launched 1s apart
and both cloning the *same* template VM UUID):

| | window | VM uuid | node | VIP / LB | VGs | result |
|---|---|---|---|---|---|---|
| qa-sn-c7 | 365s | `f8e0aa33` | .201.58 | .203.244 / .245 | 3 | green, all gates pass |
| qa-sn-c8 | 359s | `62a5ad36` | .201.59 | .203.249 / .250 | 3 | green, all gates pass |

Zero overlap on VM UUIDs, node IPs, VIP/LB, or volume groups; zero `FATAL`/`TOOL-FAILED` in either
run. Two-way concurrency cost only ~25s of window each versus the 337s solo run.

### Template identity is UUID-based, never name-based

`freeze.py` records the exact VM UUIDs it quiesced into `<prefix>-freeze-manifest.json`, and
`clone.py` clones *those UUIDs*. It used to rediscover the template as "name starts with the prefix
and `power_state == OFF`", which is unsafe: CAPX can leave a **duplicate-name VM pair**, and once
both twins are OFF that match is ambiguous. When this happened (qa-sn-tmpl1, 2026-07-27) the real
cluster was running on the twin CAPI did *not* track, so freeze powered off the wrong VM, reported
success, and minted a template from a half-bootstrapped 16.8 MB etcd — every claim from it then came
up on a blank keyspace. Three gates now make that unreachable:

- **freeze identity gate** — every `Node`'s providerID must be inside the power-off scope derived
  from `Machine`s. Compare the two *sets*; do not join them, because in the failure case they share
  no key. (My first version joined them and silently passed.)
- **freeze liveness post-condition** — once every scoped VM reports OFF, the control-plane endpoint
  must stop answering. A frozen cluster must be dead.
- **claim restored-state gate** — the window must contain CAPI Machines. Asserted once, right after
  the window apiserver is ready, so the error names the cause instead of surfacing three minutes
  later as a JSON decode failure in an unrelated tool.

### The window has no controllers — and that includes conversion webhooks

The window runs one `kube-apiserver` against a staged etcd, with **no controllers and no kubelet**.
`--disable-admission-plugins=ValidatingAdmissionWebhook,MutatingAdmissionWebhook,...` handles
*admission* webhooks. It does **not** handle **CRD conversion** webhooks — those are part of CRD
serving, not admission — and every CAPI CRD ships `spec.conversion.strategy: Webhook` pointing at
`capi-webhook-service`, which in the window has **zero endpoints**.

The result was that every CAPI-typed request blocked until its client timeout. Measured on
qa-sn-c2 (2026-07-27), from `/var/log/spike-apiserver.log` on the seed:

```
failed to prepare current and previous objects:
conversion webhook for cluster.x-k8s.io/v1beta2, Kind=Cluster
```

- `set_lb_range` burned **162s** (4 retries × ~40s) and then logged `DELTA-2 WARN: did not confirm`
- the hook clear burned another **67s** and silently did not apply, which is why the cluster then
  took ~4 more minutes post-boot to reach `Available=True`
- **229s of a 557s window — 41% — spent failing**

The tell was that `NutanixMachine` (the one CAPI kind with `strategy: None`) was also the one whose
operations were fast. `claim.py` now flips every `strategy: Webhook` CRD to `None` for the duration
of the window and restores the saved `spec.conversion` verbatim before closing — including on the
abort path, so a failed claim never leaves the template's CRDs bypassed. It is safe because the
storage version is the version we address (`stored=v1beta2`, kubectl asks for `preferred=v1beta2`):
with one version in play conversion is a no-op, so skipping it removes a call that cannot succeed
rather than changing meaning.

Measured effect — same template, same topology, same host load (qa-sn-c2 baseline vs qa-sn-c5):

| milestone (s into window) | baseline | +bypass | +prep fixes |
|---|---|---|---|
| prep done (staged etcd starts) | 147 | 160 | **130** |
| `DELTA-2` topology LB | 410 (162s spent, then `WARN`) | 267 (2s, confirmed) | **237** |
| hook cleared + unpaused | 504 (67s spent, did not apply) | 301 (applied) | **275** |
| **WINDOW COMPLETE** | **557** | 363 | **337 (−39%)** |

That is *net of* 38s of newly-added work (22s bypass + 16s restore) and of `stale_pod_sweep` now
actually doing its job (+39s); stage A also drifted +7s on load between runs, so the real gain is
slightly larger. Post-boot improves too: `Available=True` lands at t=30s instead of after ~4.3min of
gate polling, because the hook clear now really applies. Green stays t=210s (app-convergence tail).

### The post-boot tail: a pin that could not matter

18 of 22 HelmReleases keep the template's `Ready` condition straight through clone→claim and never
re-reconcile — a strong parity signal, and it means the tail is *only* the SSO chain (`dex` →
`kube-oidc-proxy` → `traefik-forward-auth-mgmt` → `dex-k8s-authenticator`), gated entirely on `dex`.

`dex` in turn waits for the git server. The git pod is ready **18s after it is created** — the cost
was *when* it got created: measured `node-Ready +301s` (qa-sn-c7) and `+198s` (qa-sn-c8), even
though `post_boc.sh` scales the StatefulSet at +74s.

Cause: LEVER-A pins the pod with a `nodeSelector` so it lands on the node whose VGs are already
attached. flux owns that StatefulSet, so the lever suspends the kustomization, patches, and resumes
— and on resume flux reverts the spec and the StatefulSet **recreates the pod** (`generation` had
churned to 7). On a **single-node** cluster that pin cannot change anything: there is exactly one
schedulable node, and `claim.py` already leaves both git VGs attached to it. So the entire delay was
a pod recreation caused by a pin that could not have mattered.

`post_boc.sh` now skips the pin (and the suspend/resume) when the map says 1 CP / 0 workers.
Multi-node keeps it — there the pin is load-bearing.

| | before | after |
|---|---|---|
| storage bring-up done | 166s | **91s** |
| git-ingress check reached | 470s | **198s** |
| StatefulSet generation | 7 | **5** |
| **green after window** | **210s** | **150s** |

SSO verified correct, not merely quiet: `kommander-vars.ingressAddress` = the claim LB, **zero**
objects left on the template LB, all SSO pods Running, all parity gates pass.

### Overlap the conversion restore with git-at-rest

After the hook clear there are no more CAPI-typed kubectl calls, so the bypass is no longer needed —
but the restore (70 CRD patches, each paying full RTT to the window apiserver) used to run *after*
git-at-rest, serially. They are independent (restore = kubectl only; git-at-rest = ssh + PC API
only; verified no kubectl in that region), so the restore now runs in a thread started at hook-clear
and joined before the abort check — a failed claim still never leaves CRDs bypassed. Measured
(qa-sn-f1/f2, 2-way concurrent): restore and git-at-rest finish within 1s of each other — the
restore is fully hidden. Saves ~15-20s on a normal link; on a degraded link the restore alone was
once 137s, all of which would have been dead window time.

### The green tail's bimodality: CrashLoopBackOff, unswept on the happy path

Green time was bimodal — ~240s or 420-480s from the same template in the same run (c7 480 / c8 240,
d1 480 / d2 240). Cause: SSO-chain pods (`dex-k8s-authenticator`, `traefik-forward-auth-mgmt`) that
start before the git server is Ready crash into kubelet's exponential backoff (up to 5min). The
bounce that pulls them out lived ONLY in the stale-ingress repair branch of `post_boc.sh`; on the
now-common "ingress already correct" path nothing swept them, so whether you got 240s or 480s
depended on whether any pod happened to be in backoff when git came up.

`post_boc.sh` now runs a bounded background sweep in BOTH branches (12 rounds × 15s, stop after 2
clean rounds): deleting a deployment pod in `CrashLoopBackOff` recreates it immediately with backoff
reset, converting a 5-minute wait into a ~15s retry. Observed live (qa-sn-f1): the chain crashed
into backoff, the sweep bounced it out over 4 rounds, and the slow path landed at ~315s instead of
~480s.

### prep: don't ship what the node already has

Two fixes, both measured on a live node before being written:

- **62MB of etcd binaries went one at a time** (etcdutl 17 + etcdctl 20 + etcd 26). Sequential 31s
  vs concurrent 14s. They are independent files; a thread-per-file copy collects success flags so a
  failure inside a worker still fails loudly instead of being swallowed.
- **The seed shipped its own database to the laptop and back.** Every CP restores from the *seed's*
  db (FIX-1), and at N=1 the seed is the only node — so that 34MB round trip was the entire
  transfer. `_fetch_db` now stashes `/tmp/seed-preplaced.db` on the seed *in the same command that
  reads it* (same instant, same bytes — not a re-read at prep time), and prep consumes it with
  `mv -f`. Non-seed CPs still receive it over scp.

Result: **prep 60s → 29s**, with stage A unchanged at ~100s, so the attribution is clean.

**Patch the CRDs concurrently.** This cluster has **70** Webhook-strategy CRDs, not the CAPI
handful. The first implementation walked them serially with two kubectl calls each — 140 round
trips, **339s** — which cost more than the 229s it saved. It is now one `get crd -o json` plus a
12-way parallel patch, and the restore is likewise parallel with per-CRD retries.

**The general lesson:** anything in the window that waits for a *controller* to act cannot succeed,
because no controllers run. Write the intent and let post-boot reconcile it — or prove the wait is
against the apiserver alone. `kx()` now logs any call that fails or takes ≥8s, because this cost was
invisible for months: the helper was silent, so 229s vanished with no line naming a cause. That same
instrumentation immediately exposed a second silent bug — `stale_pod_sweep` was reporting a
confident "0 env-stale" when its jsonpath query had actually failed (`rc=1`), i.e. "did not look"
rendered as "found none". It now parses `-o json` in Python and says so loudly when it cannot run.

## Node-count dependence

**Only five things actually depend on node count** — everything else is a loop over N:

1. etcd re-form — 1 node is `--force-new-cluster`; N nodes need a full membership restore
2. kube-vip HA restore — meaningless at CP=1
3. right-sizing profile — replica counts / anti-affinity (`rightsize_hcp.py`)
4. worker handling — single-node has no workers
5. gate expectations — pod counts, tolerated pending anti-affinity replicas

When these merge, those five belong in **one** place (a topology profile). A branch on node count
anywhere else is the smell to reject in review.

### Conventions for anything in this directory

- `NKP_PC_URL` is **required** — no Prism Central hostname is baked into any script.
- Working directory resolves from the script's own location; `INSTANT_CLUSTER_HOME` /
  `SPEEDSTART_DIR` override it.
- Destructive Prism operations are scoped by **VM UUID, never by name pattern** — CAPX names day-2
  replacement VMs after the ClusterClass, so a name filter matches other clusters' VMs.
- A destructive tool must **never fail open** on its own verification: distinguish "gone" from
  "cannot tell", and refuse on the latter.


## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `NKP_NUTANIX_USER` / `NKP_NUTANIX_PASSWORD` | — (required) | The claiming user's Prism Central credentials. |
| `NKP_PC_URL` | — (required) | Prism Central endpoint, e.g. `https://pc.example.com:9440`. |
| `NKP_SSH_KEY` | `~/.ssh/nkp_cluster` | SSH private key for `konvoy@<node>`. |
| `NKP_TEMPLATE_KC_DIR` | script directory | Where `<template-prefix>.conf` (template admin kubeconfig) is found. |
| `SPEEDSTART_DIR` | script directory | Working/state dir for freeze manifests, claim maps, logs and generated kubeconfigs. Honoured by every stage — `freeze.py` and `gates.py` used to disagree (`gates.py` read `INSTANT_CLUSTER_HOME`, `freeze.py` had no override), so running the pipeline against a state dir outside the repo silently split state across two directories. |

## Recovery

A failed claim is resumable — stage boundaries are designed to be safe stopping points:

- Failure after the CAPI re-mint → re-run the claim; identity work is at rest and idempotent,
  or re-run just the storage step with `python3 stageB_restore_only.py` / just the checks with
  `python3 gates.py`.
- Cluster looks done but gates never ran → `python3 gates.py`
- git-operator/PVC restore failed → `python3 stageB_restore_only.py`, then `gates.py`

## The parity gates (every claim)

All pods healthy · all flux Kustomizations Ready · Cluster `Available` + `TopologyReconciled` ·
control plane up-to-date and **not rolling** (the clone survived unpause) · VM names == Machine
names (day-2 delete) · `--force-new-cluster` absent (day-2 CP scale) · `controlPlaneEndpoint` ==
claim VIP (day-2 join) · topology↔reality coherent (etcd version, encryption-at-rest).

Known, accepted deltas vs a traditional cluster are enumerated in the architecture doc (§7).
