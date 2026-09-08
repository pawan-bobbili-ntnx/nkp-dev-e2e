# NKP Instant Single-Node Cluster — Architecture & Operations

> **Status (2026-08-05): partially historical.** This document captures the architecture rationale
> and the original single-node numbers. The **shipped, current pipeline is node-agnostic** (1 CP to
> 3CP+2W from the same files) and is documented in **`README.md`** — file map, invariants, gates,
> and current measurements (single-node ~8 min solo; 3CP+2W ~12.5 min, all parity gates).
> File names below have been updated to the merged set; §3's two-stage *online* claim narrative
> describes a retired variant kept for rationale — the shipped path is §6's boot-once-correct,
> now `claim.py`. The §1.1 build recipe is point-in-time (its manifest ref has since moved).

**From ~30–40 min traditional creation to ~10 min, with exact traditional parity and proven day-2 operations.**

| | Traditional create | Instant claim |
|---|---|---|
| Wall time | 25m54s measured (POC doc: 30–35 min; multi-node traditional: up to ~40 min) | **~8 min (1 CP, best solo); ~12.5 min (3CP+2W)** — see README.md for the current, gated measurements |
| Human steps | 1 command + waiting | 1 command + waiting |
| Result | Self-managed NKP mgmt cluster, kommander installed | Identical cluster (same versions, same CRs, same apps) on its own VIP/ingress/name |
| Day-2 | Supported | **Proven live**: worker scale-out, machine delete, full CP roll |

The core idea: **build the cluster once, freeze it as a golden template, and "claim" clones of it** — a claim only changes *identity* (IPs, VIP, LB, names, credentials), never *content*. All the expensive work (image pulls, kommander install, flux bootstrap, app convergence) is paid once at template build time and inherited by every clone.

---

## 1. It all starts with ONE traditional cluster — the golden template

Nothing about the instant path skips NKP's real installation. We create **one** completely
normal single-node cluster the traditional way, let every component converge, and that cluster —
frozen and powered off — becomes the golden template every instant cluster is cloned from.
This is the cost the whole approach amortizes; it is paid once per template, not per cluster.

What a traditional creation does and costs (measured with per-step timers):

| Step | Duration | What happens |
|---|---|---|
| Bootstrap (kind) cluster | ~1m00s | Local kind cluster as temporary CAPI management plane |
| Install CAPI stack operators | 1m20s | CAPI operator + providers into bootstrap |
| Create CAPI components | 2m00s | CAPX, CAREN, CABPK, KCP controllers |
| ClusterClass resources | ~5s | nkp-nutanix ClusterClass + templates |
| **Create cluster** (VM → CP ready) | **2m20s** | CAPX creates VM (1m20s infra) + kubeadm init + CNI (1m00s) |
| Pivot: CAPI stack onto the new cluster | 1m11s + 1m43s | Install operators + components on the workload cluster |
| Move CAPI resources (self-manage) | ~1m | clusterctl-style move from kind |
| GitClaim / ManagementPlane operators | ~25s | git-operator bootstrap, kommander operator |
| PlatformVersionArtifact | 1m33s | Platform metadata resolution |
| **Kommander install** | **12m53s** | 14 core applications (dex, traefik, kommander, flux, gatekeeper, …) rendered and converged via flux/helm |
| **Total** | **25m54s** | |

The dominant cost — kommander’s 14-application convergence (~13 min) plus the double CAPI-stack installation (~6 min) — is *content*, identical for every cluster. That is what the template amortizes.

### 1.1 Build the single-node-capable CLI (interim — until the konvoy2 PR merges)

The stock `nkp` CLI taints control-plane nodes `NoSchedule`, so a 1-node cluster could never run
workloads. The single-node feature lives on `konvoy2` branch `issue/project` (base `v2.18.0-rc.2`):
a kustomize patch that ships **empty taints** on the control plane
(`initConfiguration/joinConfiguration.nodeRegistration.taints: []`) so workloads schedule on the
CP, plus per-step timing output.

> **Upstream status: implemented, awaiting PR review.** The branch now carries a
> `--speed-start-template` flag on `nkp create cluster nutanix`: when present, the ClusterClass
> is rendered with control-plane taints cleared (a runtime mutator — nothing is baked into the
> manifests any more) and replica counts default to 1 control plane / 0 workers; when absent,
> behavior is byte-identical to stock (verified: the generated manifests contain no taint patch).
> Covered by a unit test (`pkg/capi/clusterclass/speedstart_test.go`). Once merged, template
> builds are just `nkp create cluster nutanix --speed-start-template …` on a released CLI, and
> the build recipe below is only needed until then. Note: the csi-controller/cilium-operator
> replica right-sizing cannot ride this flag — CAREN v0.52 exposes no replica knob in the
> cluster variables — so it stays in `freeze.py` (a future CAREN change could absorb it).

**Why `DEFAULT_ETCD_VERSION` is mandatory:** the ClusterClass template contains an etcd
`imageTag: $DEFAULT_ETCD_VERSION` placeholder, substituted at generation time. When the script
runs under `make`, the build system injects the value from `pkg/api/v1alpha1/types.go`
(`DefaultEtcdVersion = "3.5.24-0"` — the etcd version kubeadm ships for this Kubernetes release).
Invoked standalone, the variable is unset, the placeholder renders as `imageTag: null`, and every
cluster silently comes up on CAREN's *own* default etcd (a different minor version) — a version
drift from traditionally-released clusters that we measured as a real parity break. Passing it
explicitly pins etcd to the release-correct version.

```bash
git -C konvoy2 worktree add ../konvoy2-singlenode origin/issue/project
cd konvoy2-singlenode

# Regenerate the EMBEDDED ClusterClass manifests (the taint patch lives in a template
# that must be baked in). DEFAULT_ETCD_VERSION is MANDATORY — without it the manifest
# renders `imageTag: null` and clusters come up on the wrong etcd.
DEFAULT_ETCD_VERSION=3.5.24-0 \
CAREN_VERSION=v0.52.1 \
CAREN_GITHUB_REPO=nutanix-cloud-native/internal-cluster-api-runtime-extensions-nutanix \
KONVOY_VERSION_TAG=v2.18.0-rc.2 \
ENVSUBST_ASSETS=/opt/homebrew/bin KUSTOMIZE_BIN=/opt/homebrew/bin/kustomize \
GITHUB_CLI_BIN=/opt/homebrew/bin/gh \
./hack/capi/update-cluster-class-templates.sh

# Pin the embedded NKPCluster manifest to a REAL image tag (in-tree referenced a
# nonexistent -dev tag).
make nkpcluster.manifests.update NKPCLUSTER_MANIFEST_REF=v2.18.0-rc.2 NKPCLUSTER_IMAGE_TAG=v2.18.0-rc.2

# Build with the release ldflags (gitVersion drives which component images the CLI deploys).
export PATH="/usr/local/go/bin:$PATH"   # Go 1.25.x, matching go.mod
go build -trimpath -o bin/nkp-single \
  -ldflags "-s -w \
   -X 'github.com/mesosphere/dkp-cli-runtime/core/cmd/version.gitVersion=v2.18.0-rc.2' \
   -X 'github.com/mesosphere/dkp-cli-runtime/core/cmd/version.major=2' \
   -X 'github.com/mesosphere/dkp-cli-runtime/core/cmd/version.minor=18' \
   -X 'github.com/mesosphere/konvoy2/pkg/constants.MindTheGapVersion=v1.25.1'" \
  ./cmd/konvoy/main.go
```

### 1.2 Create the template cluster

Standard traditional creation; 16 vCPU / 32 GiB is the single-node minimum (8 vCPU leaves CAPI + kommander pods Pending). Worker flags are required even at `--worker-replicas=0` (webhook validation).

```bash
./bin/nkp-single create cluster nutanix \
  --cluster-name=qa-sn-tmpl1 --verbose=4 \
  --endpoint=https://<PC>:9440 --insecure \
  --control-plane-endpoint-ip=<TEMPLATE_VIP> \
  --control-plane-replicas=1 --control-plane-vcpus=16 --control-plane-memory=32 \
  --control-plane-prism-element-cluster=<PE> --control-plane-subnets=<SUBNET> \
  --control-plane-vm-image=<NKP_ROCKY_IMAGE> \
  --worker-replicas=0 \
  --worker-prism-element-cluster=<PE> --worker-subnets=<SUBNET> --worker-vm-image=<NKP_ROCKY_IMAGE> \
  --csi-storage-container=<CONTAINER> \
  --kubernetes-service-load-balancer-ip-range=<TEMPLATE_LB>-<TEMPLATE_LB> \
  --registry-mirror-url=https://registry-1.docker.io --registry-mirror-username=… --registry-mirror-password=… \
  --ssh-public-key-file=<PUBKEY> --ssh-username=konvoy \
  --self-managed
# ~26 min. The golden "image" IS this converged cluster's disks.
```

## 2. Freezing the template (`freeze.py <prefix> <kubeconfig>`)

Once the traditional cluster is fully converged, we freeze it. Freezing is not just powering
off — each step below removes a specific way a future clone would hurt itself or its siblings.

What freezing does, and why each step exists:

1. **Right-size csi-controller and cilium-operator to 1 replica.** Both ship as 2-replica
   Deployments with a *hard* pod anti-affinity (each replica on a different node) — on a 1-node
   cluster the second replica is unschedulable **forever** (a perpetual `Pending` pod), and worse,
   csi-controller's rollout strategy (`maxSurge: 0`) *deadlocks* every future update at 2 replicas:
   the rollout may only kill the Pending copy, and its replacement can't schedule beside the
   Running one either. The setting is written into the **HelmChartProxy values** — the CAPI addon
   mechanism (CAAPH) that helm-renders these Deployments — because that is the only durable place:
   a plain `kubectl scale` would be reverted on the next helm render. Result: zero Pending pods
   and wedge-free upgrades on one node.
2. **Suspend the `git-operator` flux Kustomization, then scale the git StatefulSet to 0.**
   Flux continuously re-applies what git declares — including this StatefulSet's `replicas: 1`
   and its PVC manifests. Left unsuspended, flux re-scales the git server back up within about a
   minute (re-mounting the volumes mid-snapshot), and during a claim it re-creates the PVC as an
   *empty* volume in the middle of the restore (measured — it wedged a claim). Suspending
   (`spec.suspend: true`, the API field every flux version honors — the annotation alternative is
   ignored by some versions) quiesces the volumes for a clean snapshot and keeps flux's hands off
   until the claim's restore re-enables it. Scaling to 0 unmounts the volumes, so the snapshot
   captures a cleanly closed filesystem rather than a crash-consistent one.
3. **VolumeSnapshot the two git-operator PVCs** (exact names the claim restores from:
   `git-operator-{git,admin}-volume-snapshot`). What's in them: kommander's **internal Git
   repository — the GitOps source of truth** for the whole cluster (every platform app's
   kustomization, AppDeployment renders, cluster overrides including SSO config) plus the git
   server's admin state. This is the product of the entire ~13-minute kommander install, and it
   lives on **external Nutanix Volume Groups (iSCSI), not inside the VM's disks** — a VM clone
   alone would silently miss it and leave the clone pointing at the template's (shared, RWO)
   volume. The snapshots let every claim mint a *private* copy of that converged git state in
   ~30 seconds — something that cannot be re-created imperatively at claim time.
4. **Pause the CAPI Cluster** (`spec.paused: true`). The template is self-managed: its own CAPI
   controllers run *inside* it, holding records of the template's infrastructure (machine
   identities, VM UUIDs, health checks). A clone boots with those controllers and those — now
   wrong — records. Unpaused, they act immediately: MachineHealthCheck sees an "unhealthy"
   machine and starts **remediation, i.e. deleting and replacing the control-plane VM**; KCP sees
   drift and rolls the control plane — all while the claim is mid-surgery on those very records.
   `paused` makes every CAPI controller ignore the cluster, giving the claim a race-free window
   to rewrite identities; the claim unpauses only after the records match reality.
5. **Disable cloud-init** (`touch /etc/cloud/cloud-init.disabled`). cloud-init decides "is this
   my first boot?" by comparing the platform-provided **instance-id** — and a clone, being a new
   VM, has a new one. It would therefore re-run its first-boot user-data, which on an NKP node is
   the CAPI bootstrap payload: it rewrites the kubeadm configuration and re-executes
   **`kubeadm init`** — regenerating certificates and re-initializing the etcd data directory
   *over* the converged cluster state. That wipes the very thing the template exists to preserve,
   leaving a half-initialized fresh node instead of a clone. With the flag file present, a
   clone's first boot is just a reboot.
6. **ACPI power-off.** The parked, OFF VMs *are* the golden template; the claim clones their
   disks directly (no image-conversion step, nothing to upload or register).

---

## 3. The claim — how an instant cluster is actually created

With the golden template parked and powered OFF on Prism Central, "creating" a cluster no longer
means installing anything. A **claim** clones the template's disks and re-binds the copy's
*identity* — addresses, names, credentials, storage — while all the installed content comes along
untouched. Everything below is what one command (`claim.py`) does, in order — written as a
chain of needs: each step exists because the previous state leaves a specific gap, and the step
is the narrowest safe way to close it.

### Stage A — from "a frozen template" to "a running control plane on new addresses"

**We need a machine that already contains the converged cluster** — every image pulled, kommander installed, flux bootstrapped — without paying the 26-minute build. The frozen template's disks *are* that state, so we clone its powered-OFF VM on Prism Central (`vms/{uuid}/clone`) and power it on. A disk clone is the only Nutanix primitive that copies converged state in seconds.

**We need to reach the machine**, and its address is assigned by DHCP at boot, so we poll PC until the clone's IP appears — the earliest moment any further work is possible.

**We need the storage layer to treat this as a *new* VM.** Nutanix CSI answers "which VM am I?" by reading `/etc/machine-id`. On a normal node nobody writes this by hand: systemd initializes it on *first boot*, and on KVM hypervisors (AHV is KVM) it seeds the value from the VM's own SMBIOS/DMI product UUID — which is why every traditional NKP node naturally has `machine-id == its VM UUID`. But machine-id is *designed to be permanent*: systemd only generates it when the file is empty, and a clone's disk arrives carrying the template's perfectly valid value. The VM got a new UUID; the file didn't follow — so left alone, every future volume attach would target the *template* VM (measured, catastrophic). The claim therefore writes `/etc/machine-id` = the clone's VM UUID (read from `/sys/class/dmi/id/product_uuid`) and verifies it before anything consumes storage, and strips the inherited `providerid` custom attribute from the VM for the same reason on the CAPI side. (*Planned refinement: truncate the file at freeze so systemd's own first-boot seeding regenerates it correctly at clone boot; the claim's write-and-verify stays as the safety net.*)

**We need a working apiserver, but every on-disk record points at addresses that no longer exist.** Static-pod manifests, kubeconfigs, and certificates all carry the template's node IP and VIP; worse, etcd's own membership database records the template IP, so etcd cannot even form quorum. Closing this gap is `reident.py`: rewrite the addresses in every manifest and kubeconfig, regenerate the apiserver/etcd certificates with the new SANs (`kubeadm init phase certs`), and start etcd with `--force-new-cluster` — the one etcd mechanism that re-forms a single-member cluster *while keeping all data*. The apiserver answers ~13s after kubelet starts. We then update the etcd member's advertised peer-URL (force-new-cluster preserves the old one, and future joiners dial it).

**We need the flag gone eventually, but not now**: `--force-new-cluster` is idempotent for a single member, while removing it forces an etcd restart that costs ~45s of apiserver downtime — unaffordable here, where nothing else can run. So we deliberately leave it armed and remove it later, in a window built to absorb the blip; a hard gate at the end asserts it is gone (a day-2 control-plane joiner must never meet that flag).

**We need the longest convergence in the whole system to start as early as physics allows.** Rebinding cilium's DaemonSet to the new VIP triggers a ~2-minute agent roll, and nothing webhook-dependent can finish before that roll settles — so the instant the apiserver answers (still inside Stage A), we begin the in-cluster rebind *DaemonSets first*, overlapped with Stage A's tail. This single ordering decision is worth 4 minutes.

### Stage B — from "running on new addresses" to "a green, day-2-safe cluster that believes its new identity"

**We need CNI and the cloud-controller alive before anything else can heal**, because both dial the apiserver at the address baked into their pod templates — the dead template VIP — and every admission webhook in the cluster needs CNI to get a pod sandbox. So pass 1 patches exactly the 8 plain-text VIP carriers (cilium/cilium-envoy DaemonSets; cilium-operator/CCM/konnector Deployments; cluster-info/kubeadm-config/nutanix-config ConfigMaps). We deliberately do *not* touch secrets or HelmChartProxies yet: they are webhook-gated, admission is still down, and each doomed apply burns a 60-second timeout — they belong to pass 2.

**We need the Node object to carry the clone's providerID, and that field is immutable once set.** CAPI binds Machine→Node through it; the clone's Node still carries the template's. The only path is re-registration: we bake `--provider-id=nutanix://<cloneUUID>` directly into kubelet's flags, delete the Node object, and restart kubelet so the node registers once, already correct. We bake the flag rather than letting CCM assign it because CCM's name-based fallback can match the frozen template VM — which carries the identical node name — and stamp the wrong, immutable value (measured).

**Now we can afford the etcd flag removal**: the phases that follow are pure read-and-retry, so we `sed` the flag out of the manifest and let kubelet's file-watch restart etcd. The ~45s apiserver blip lands in a window designed to tolerate it.

**We need evidence, not assumption, that the re-identity left nothing broken** — so three cheap sweep phases (pod-CIDR coherence, cilium health, broken-sandbox) run as tripwires. On a healthy claim they find zero; the day they find something, they either fix it or fail loudly.

**We need per-clone GitOps state, and it cannot be shared.** Kommander's source of truth is a git repo on an RWO Nutanix Volume Group; clones inheriting the template's VG would corrupt every sibling (and the first claim would delete the shared VG — measured). So, on a background thread: the git-operator PVCs are recreated from the VolumeSnapshots taken at freeze — each clone minting private VGs with converged content — and the helm-charts PVC is recreated empty (its init container reseeds it from an image), with the old PV set to `Retain` so the template's VG survives.

**We need dex to serve the claim's ingress, and the change must be made in git, not on the objects.** Flux re-applies whatever git says; any direct patch to the rendered ConfigMaps is overwritten minutes later (measured as a +3-minute double-roll when attempted early). So the same thread rewrites the 4 SSO override files *inside the restored git repo*, commits, and pokes flux — making the correct ingress the cluster's own declared state.

**We need working Prism credentials for CSI/CAPX, but only if they rotated** since template build — so a thread probes the baked credentials against PC and refreshes the secrets only on failure. On a fresh template this costs nothing.

**We need the single-node steady state to be legal.** Two components ship 2 replicas with hard pod-anti-affinity — on one node the second replica is unschedulable forever, and worse, csi-controller's rollout strategy (`maxSurge: 0`) deadlocks at 2 replicas because it can only ever kill the Pending copy. We inject `replicas: 1` into their CAAPH HelmChartProxy values — the one place the setting survives, since helm re-renders deployments from there.

**We need the rebind to be durable, not just current.** A HelmChartProxy still holding the old VIP will re-render cilium back to it hours after we declare success; the CAPI kubeconfig *secret* is what KCP's remote probe dials. So pass 2 — now that webhooks answer — does the full-scan rebind including secrets and HCPs, points MetalLB/kommander-vars/traefik at the claim's LB range, and swaps the SSO override CMs (safe now: git already says the same thing).

**We need CAPI's records to describe the clone's infrastructure, or every day-2 operation destroys the wrong VMs.** While the cluster is still paused (controllers idle — this is why the template froze paused), we re-mint the tree: Machine/NutanixMachine providerIDs and vmUUIDs → clone VM; **`Cluster.spec.controlPlaneEndpoint` → claim VIP**, because CABPK renders every future join's discovery endpoint from it — our live day-2 test proved that without this, a scaled-out worker dials the dead template VIP forever; unique claim-prefixed Machine names via rename-by-recreate, re-pointing child ownerReferences *before* deleting the old Machine (or the garbage collector cascades into a live node's bootstrap config); and the PC VMs renamed to match, because CAPX's name-guard refuses to delete a VM whose name differs from its Machine.

**We need NKP tooling to find the cluster by the claim's name** while the immutable CAPI object keeps the template's — so the NKPCluster gets an alias label carrying the claim name.

**We need CAPI running again — without it rolling the control plane.** The VIP change makes KCP consider the cloned CP "outdated"; unpausing naively replaces the entire control plane (measured on early builds). So: freeze KCP with its `paused` annotation (never by scaling its controller to zero — that kills the webhook KCP itself serves, deadlocking the topology controller), unpause the Cluster, let the topology controller render the new VIP into KCP's desired spec, re-mint the cloned CP's KubeadmConfig to match that rendered spec exactly, then unfreeze — KCP's first evaluation finds the control plane already up-to-date, and the clone survives.

**Finally, we need proof, not hope** — so every claim ends with gates that would rather fail the claim than ship a subtly broken cluster: all pods healthy; all flux kustomizations Ready; `Available=True` and `TopologyReconciled=True`; control plane up-to-date and not rolling; VM names == Machine names (day-2 delete safe); `--force-new-cluster` absent (day-2 scale safe); `controlPlaneEndpoint` == claim VIP (day-2 join safe); topology↔reality coherent (etcd version, encryption-at-rest).

### Understanding `--force-new-cluster`

etcd is a Raft cluster, and it stores **its own membership as data**: inside etcd's database (the bbolt file at `/var/lib/etcd/member/snap/db`) there is a member table recording, for every member, its ID and its **peer URL — an IP address**. On startup, etcd reads that table and tries to (re)join the cluster it describes.

A cloned disk therefore contains an etcd that believes it belongs to a cluster living at the **template's IP** — an address that no longer answers (the template is powered off, and the clone booted on a new DHCP address anyway). Raft requires a quorum of the *recorded* members, so the cloned etcd starts, dials a ghost, and hangs forever. No amount of manifest editing fixes this, because the stale address lives inside the database, not in any config file.

`--force-new-cluster` is etcd's built-in escape hatch for exactly this class of disaster recovery. When etcd starts with this flag it:

1. **Keeps the entire keyspace untouched** — every Kubernetes object, all of kommander's state, everything the template converged. This is the property the whole architecture rests on.
2. **Discards the membership table** and rewrites it as a brand-new single-member cluster consisting of only the local node — no ghost peers left to dial.
3. Preserves the member's old *advertised* peer URL, which is why the claim explicitly updates it to the clone's IP afterwards (`etcdctl member update`) — future joiners read that URL to find the cluster.

Two safety properties govern how we use it:

- **For a single-member cluster the flag is idempotent** — re-asserting "I am a cluster of one" on every restart changes nothing. That is why the claim can safely leave it armed for a few minutes and remove it off the critical path (its removal forces an etcd restart costing ~45s of apiserver downtime, which we place in a retry-tolerant window).
- **The flag must never survive into day-2**: if a second control-plane node ever joined while the first still carried `--force-new-cluster`, the next etcd restart would discard *that* membership too — destroying the cluster. This is why a hard gate at the end of every claim FATALs unless the flag is verifiably gone, and why the live CP-roll test (a new control plane joining this exact re-formed etcd as a learner and promoting) was part of the parity proof.

### Day-2 (proven live on a claimed clone)

Worker scale-out (via autoscaler min/max annotations — CAPI forbids explicit replicas with them), machine deletion, and a **full control-plane roll** (new CP joins the re-formed etcd as learner, promotes, old CP drains and is deleted, VIP fails over). After the first roll the surgered node is gone — the cluster is indistinguishable from a traditional one. One known first-roll caveat: the outgoing (cloned) CP can wedge the roll for ~25 min if its cilium datapath goes stale while it still holds the VIP; fix is one `kubectl delete pod` of that node's cilium agent.

---

## 4. Measured timeline of a claim

Now that each phase has meaning, here is where the ~10.5 minutes (median, zero-intervention) go:

| Phase | Duration | What happens |
|---|---|---|
| **Stage A** | **2m11–2m26** | |
| — clone API + power-on | ~41s | PC v3 `vms/{uuid}/clone` of the OFF template + power-on task |
| — boot → IP visible | 22–46s | VM boots (Rocky/CIS image), DHCP, PC reports IP |
| — identity prep (SSH) | ~22s | `/etc/machine-id` ← clone VM UUID; strip inherited providerID attribute |
| — re-identify + etcd re-form | ~35s | Rewrite manifests/certs to new IP+VIP, `--force-new-cluster`, apiserver up at +13s, etcd member peer-URL update |
| **Stage B** | **~7–9m** | |
| — pass-1 VIP rebind (targeted) | ~45–60s | 8 known objects: cilium DS/envoy, cilium-operator, CCM, konnector, 3 CMs |
| — node re-mint (waves) | ~45s | providerID baked into kubelet flags, Node delete/re-register |
| — etcd flag-drop | ~45s (overlapped blip) | `--force-new-cluster` removed via kubelet fsnotify restart |
| — heal/assert phases | ~30s | CIDR sweep (finds 0), cilium check, crashloop sweep |
| — right-sizing + rebind pass 2 + LB + SSO | ~1m30s | csi/cilium-operator → 1 replica; HCPs, MetalLB pool, ingress, git repo rewrite |
| — CAPI tree re-mint | ~1m40s | providerIDs, controlPlaneEndpoint, unique machine names, VM rename |
| — unpause + KCP no-roll dance | ~1m | freeze KCP → unpause → render → re-mint KubeadmConfig → unfreeze |
| — gates | ~1m40s | All pods healthy, all flux kustomizations, CAPI Available/no-roll/day-2 asserts |
| (parallel, hidden) | 0 on critical path | PVC restore from snapshots, git deep-fix, credential refresh, HCP pass 2 |

Kommander on a claim costs **zero install time**: core pods run continuously from the template (several never restart at all); only the ~10 ingress/SSO-touching HelmReleases re-render (~1–3 min, fully overlapped with the CAPI work).

---

## 5. What actually differs between the template's VM and a claimed clone's VM

Everything below is a deliberate, gated change; anything *not* listed is bit-identical to the template — which is precisely where the speed comes from.

| Area | Template VM (frozen) | Instant clone (after claim) | Why the change is required |
|---|---|---|---|
| `/etc/machine-id` | template VM's UUID | **clone VM's UUID** | Nutanix CSI resolves "which VM am I" via machine-id; wrong value attaches volumes to the template VM |
| VM `providerid` custom attribute | present (template UUID) | **removed** | CAPX would resolve the clone to the template VM |
| kubelet flags | no `--provider-id` | **`--provider-id=nutanix://<cloneUUID>` baked** | Node must register with the clone's identity; CCM's name-fallback can match the frozen template VM |
| Node IP | template's DHCP lease | **clone's own DHCP lease** | Two VMs can't share an address; every on-disk reference is rewritten to it |
| Control-plane VIP | template VIP (e.g. .245) | **claim's VIP** in kube-vip manifest, kubeconfigs, apiserver probes | Each concurrent clone must own a unique apiserver endpoint |
| apiserver/etcd certificates | SANs for template addresses | **regenerated** — SANs include the clone IP + claim VIP (+localhost/127.0.0.1) | TLS must be valid for the new endpoints |
| etcd membership (inside bbolt) | member @ template IP | **re-formed single member @ clone IP** (`--force-new-cluster` + peer-URL update) | Raft quorum is unreachable at the ghost address; data is preserved |
| `--force-new-cluster` flag | absent | armed at boot, **removed during claim, gated absent at the end** | See explainer above; must never meet a day-2 joiner |
| Node object | template-era (old providerID) | **deleted + re-registered once, correct** | `Node.spec.providerID` is immutable |
| VM name on Prism | template machine name | **renamed to the claim's Machine name** | CAPX's name-guard refuses day-2 deletion on mismatch |
| CAPI Machine objects | template names, template providerIDs | **claim-prefixed names, clone providerIDs, clone vmUUIDs** | Day-2 operations must target the clone's real infrastructure |
| `Cluster.spec.controlPlaneEndpoint` | template VIP | **claim VIP** | CABPK renders every future join's discovery endpoint from it |
| git-operator volumes | template's Volume Groups | **fresh VGs restored from freeze-time snapshots** | RWO VGs cannot be shared between clones; content (converged git history) is preserved via snapshot |
| helm-charts volume | template's VG | **fresh empty VG, reseeded by init container** | Same sharing problem; content is fully regenerable |
| Git repo content (SSO overrides) | template ingress IP | **claim ingress IP (committed)** | dex must serve the claim's issuer; flux re-applies whatever git says |
| MetalLB pool / ingress | template LB range | **claim's LB range** | Concurrent clones would ARP-fight over shared ingress IPs |
| CCM/CSI/CAPX credential secrets | template builder's creds | **claiming user's creds (always refreshed)** | Per-user resource attribution on Prism Central |
| csi-controller / cilium-operator replicas | 2 (1 perpetually Pending) | **1** | Single-node steady state; avoids a rollout deadlock class |
| NKPCluster | template name | **+ alias label = claim name** | NKP tooling addresses the cluster by the user's chosen name |
| CAPI paused state | paused (frozen that way) | **unpaused, with the no-roll dance** | Controllers must manage the cluster again without replacing the CP |

## 6. The boot-once-correct variant (`claim.py`) — identity at rest

The classic claim (section 3) repairs identity ONLINE: the running cluster is told about its new
addresses while controllers watch, which is why it needs the node re-mint, a cilium roll and the
KCP choreography. Boot-once-correct asks the opposite question: *what if the clone never observes
a wrong identity at all?*

### The mechanism — one kubelet window

After stage A boots the clone (inert — the template is frozen with kubelet disabled), a single
surgery window (the in-window surgery (inline in `claim.py`), ~35 s of edits) performs, in order:

1. **Disk reident** — node IP + VIP in static-pod manifests and kubeconfigs, certificate
   regeneration with the new SANs (`kubeadm init phase certs`).
2. **etcd re-form** — `--force-new-cluster` is added to the *static kubeadm etcd manifest* (the
   only force-new that is safe; an external transient etcd corrupts nothing but was ruled out the
   hard way) while the apiserver/controller-manager/scheduler manifests are **held aside**, so
   etcd serves for the next step with no apiserver watching (raw writes under a live apiserver
   desync its watch cache — measured).
3. **Keyspace byte-replace** — same-length replacements of the VIP, the ingress LB IP, and the
   VM UUID (providerID) directly in the etcd values. Two rules make this safe: *same length in =
   same length out* (protobuf length prefixes survive untouched), and *strip etcdctl's display
   newline* (one stray `0x0A` re-put into a value makes the apiserver's LISTs fail with
   "unexpected EOF" — the single costliest bug of this project). The replacement walks the FULL
   identity source-of-truth chain:

   `NKPCluster -> Cluster -> NutanixCluster -> KubeadmControlPlane -> KubeadmConfig -> Machine/NutanixMachine/Node`

   The first link matters most: on unpause the NKPCluster controller re-propagates topology
   variables, CAREN re-renders the kube-vip file from them, and KCP compares the result against
   each machine. Any stale VIP anywhere in that chain = a spurious control-plane roll onto a
   freshly provisioned VM (measured before the fix).
4. **Identity-pod deletion + restore** — cilium/CCM pods are deleted in etcd (pods bake their
   environment at creation) and the held manifests are restored. Kubelet starts everything once;
   every controller reads correct state on its first list.

### The lean tail (overlapped with the single convergence)

Only things that genuinely need a live apiserver: encrypted kubeconfig Secrets (ciphertext at
rest), `Node.status.addresses` (owned by the CCM, which only sets it at node initialization —
a cloned node keeps the template IP forever without this patch), the PVC restore, HCP/LB rebinds,
an early unpause, and single-node right-sizing (which must run after unpause — CAREN reverts it —
and after the restore — a csi-controller roll mid-PVC-bind stalls binding by minutes).

### Measured results and the honest trade-off

Two consecutive zero-intervention claims: **14m04s and 14m48s to gates rc=0**, day-2 worker
scale-out AND scale-in proven live on a claim. The classic path remains faster (~10 min) because
its identity work always overlapped the restore anyway; BOC's value is what it removes: no pod
churn waves, no CNI roll, no CP-roll risk, no first-roll caveat, day-2-correct
`controlPlaneEndpoint` at rest, and unique clone VM names (day-2 delete resolves by UUID via the
READY-NutanixMachine short-circuit of CAPX's name-guard) — the property that makes N concurrent
claims from one template safe.

## 7. Why we cannot go below ~10 minutes (under the constraints we hold)

Having seen the mechanism, the floor argument becomes concrete:

Constraints in force: **(a)** exact traditional behavior — no config deltas from a traditionally-created cluster; **(b)** on-demand only — no pre-staged/idling clones; **(c)** untouched node image — no boot trimming of the CIS-hardened image.

The remaining 10 minutes decomposes into four blocks, each pinned by evidence:

1. **Clone + boot + re-form (~2m15s) — fixed by (b) and (c).** A VM must be cloned and cold-booted on demand; AHV has no memory-state fork. etcd itself restarts in 1.3s (measured; 107 MB db) — the rest is real kernel/systemd/kubelet startup.
2. **The cilium roll (~1.5–2m) — fixed by (a).** Every clone gets a unique VIP; the VIP lives in cilium's pod template (`KUBERNETES_SERVICE_HOST`), so the DaemonSet must roll — exactly as a traditional cluster would if its control-plane endpoint changed. We proved a "conservation law": this convergence can be *started earlier* (we reordered the rebind to hit DaemonSets first, saving 4 min) but never removed. The parity-preserving alternative (pointing cilium at `127.0.0.1`) was rejected because it is visible config drift, breaks day-2 worker-add, and reverts silently on upgrade.
3. **The SSO chain (~3–4m, partially overlapped) — fixed by (a) + causality.** A new ingress IP must re-render dex/kube-oidc-proxy/traefik-forward-auth — the same helm work a traditional install does. Its start time is causally pinned: it needs the rewritten git repo → which needs the restored git volume → which needs CSI identity → which needs the node re-mint. We measured that starting it earlier (patching CMs before the git rewrite lands) causes a double-roll regression (+3 min) because flux re-applies the old git content over the patch.
4. **The blip conservation (~45s).** The one-time etcd clean restart costs ~45s of apiserver unavailability *wherever it is placed* — we moved it three times (stage A → waves → fsnotify window) and the cost moved with it, never shrank, because everything downstream needs the apiserver.

Three consecutive zero-intervention runs at 10:28/10:11/10:09, followed by two thinning attempts that produced one wash and one reverted regression, is the empirical signature of a floor. Going lower requires *bending a constraint* — each option is documented with mechanism and risk (localhost endpoint ≈ −2.5 min; one staged clone ≈ −2 min) — a product decision, not an engineering task.

---

## 7. Creating an instant cluster

**Prism credentials are per-claim and per-user** (requirement): every user claims with their *own* PC account. Those credentials are pre-flight-verified before anything is created, drive every PC operation of the claim (clone, power, rename, volumes — so PC attributes the VMs and tasks to that user), and are **always written into the clone's CCM/CSI/CAPX secrets** (even if the template's baked credentials still work), so the cluster's ongoing storage/infra usage is attributable to the claiming user as well.

```bash
export NKP_NUTANIX_USER=<your-pc-user> NKP_NUTANIX_PASSWORD=<your-pc-password>

python3 claim.py <template-prefix> <cluster-name> <template-vip> <new-vip> <lb-start> [lb-end]
# example:
python3 claim.py qa-sn-tmpl1 qa-team-alpha <lb-address> <lb-address> <lb-address> <lb-address>
```

Output ends with:

```
CLAIM COMPLETE: qa-team-alpha | stageA 2m26s stageB 7m53s TOTAL 10m09s
  apiserver https://<lb-address>:6443   ingress https://<lb-address>
```

Per-claim inputs: a unique cluster name, a free VIP, a free LB/ingress IP. Concurrent claims from one template are supported (each clone owns its VIP/LB/names). If a claim fails mid-way it is resumable (continuation scripts exist for the CAPI-re-mint point, the restore, and the gates), and every failure mode discovered during development now has either an automated fix in the pipeline or a hard gate that refuses to ship a broken cluster.

### Known, accepted deltas vs a traditional cluster (full list)

- Shared cluster CA / kube-system UID across clones of one template (accepted for internal QA).
- Node hostname and CAPI Cluster object name are the template's; the NKP-visible name is the claim's (alias label). Machines and VMs are claim-named.
- 3 PVCs carry `kustomize.toolkit.fluxcd.io/reconcile: disabled` (required: flux cannot server-side-apply the immutable `dataSource` of restored PVCs; inert).
- apiserver/etcd certs carry extra SANs (old + new addresses); `Machine.status.addresses` may show template-era IPs until CAPX refreshes (cosmetic).
- First CP roll on a clone can need one cilium-agent bounce on the outgoing node (documented above). After that roll, zero deltas remain on the node plane.
