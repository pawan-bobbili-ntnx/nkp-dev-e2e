# NKP release artifact map — how the repos combine into a release

Traced from the actual repos (2026-08-06, NKP v2.18 era): konvoy2, kommander, kommander-cli,
kommander-applications, charts, CAREN (internal), nkp-nutanix-product-catalog, dkp-release,
dkp-release-specs. This is the foundation for the CD goal: for any change on top of a released
version, know exactly what must be rebuilt and the fastest way to get it onto a cluster.

## 0. The one-paragraph picture

A release is pinned by ONE spec file — `dkp-release-specs/releases/dkp-v2.18.0-N.yaml` — mapping
7 components to git SHAs (`konvoy2`, `kommander`, `kommander-applications`, `kommander-cli`,
`dkp-cli`, `dkp-image-builder`, `bundle`); the external `gh-dkp` extension turns a merged spec
into tags, images, and promoted artifacts. CAREN, CAPX, catalog, kommander-ui enter
*transitively* through those pins. The `nkp` binary is TWO codebases: konvoy2 (cluster
provisioning; embeds provider manifests + CAREN-rendered ClusterClass) and kommander-cli
(the `install kommander` half, vendored as a Go module). Everything platform-level reaches the
cluster as either (a) a container image, (b) a helm chart in an OCI registry, or (c) rendered
YAML/git content — and each of the three has a distinct fast-override vector.

## 1. Per-repo artifact table

| Repo | Builds | Published to | Consumed by (pin location) |
|---|---|---|---|
| **konvoy2** | `konvoy` (nkp cluster CLI), `konvoybundlepusher`, `capimate` image, `konvoy-bootstrap` kind image, airgap image bundle | GitHub release + `s3://downloads.mesosphere.io/dkp/<ver>/`; images → `docker.io/mesosphere` | user runs it; release spec pins the SHA |
| **kommander** | ~12 images via one goreleaser run: `kommander2-core-installer` (10 binaries: kommandercore, managementplane, nkpcluster, upgradeplan, loggingstack, release-operator + webhooks), `kommander2-appmanagement(+webhook,+config-api)`, `kommander2-federation-controller-manager`, `-authorizedlister`, `-webhook`, `kommander2-flux-operator`, `kommander2-licensing-*`, `kommander2-kubetools`; charts `kommander`, `kommander-appmanagement` | images → `docker.io/mesosphere/<img>:v<tag>`; charts → `oci://docker.io/mesosphere/<chart>-chart` | image tags referenced by kommander-applications app dirs + `common/<operator>` kustomizations |
| **kommander-cli** | Go module only (no images) — the imperative installer | vendored into nkp via `go.mod` (`github.com/mesosphere/kommander-cli/v2`) | konvoy2/dkp-cli build |
| **kommander-applications** | FOUR forms per tag: S3 tarball (`kommander-applications-<tag>.tar.gz` → downloads.d2iq.com), OCI artifact (`ghcr.io/mesosphere/kommander-applications:<tag>`), git-server image (`...-server:<tag>`), platform collection artifact | S3 + ghcr | CLI fetches tarball at its own binary version (`kommander-cli/pkg/util/versioning.go`); in-cluster operator pulls the OCI artifact at ldflag `version.Version` (`kommander/common/pkg/oci/kapps.go:16`) |
| **charts** (mesosphere/charts) | helm chart tgzs | `gh-pages` HTTP repo (mesosphere.github.io/charts) → **manually mirrored** to `oci://ghcr.io/mesosphere/charts/<chart>:<Chart.yaml-version>` via k-apps workflow `single-chart-ghcr.yaml` (workflow_dispatch!) | pinned per-app in k-apps `applications/<app>/<ver>/helmrelease/<app>.yaml` → `OCIRepository.spec.ref.tag` |
| **CAREN** (internal fork ships) | controller image (`ghcr.io/nutanix-cloud-native/cluster-api-runtime-extensions-nutanix:v<ver>`), bundle-initializer image (addon charts tar), release assets: `runtime-extensions-components.yaml`, `*-cluster-class.yaml`, `caren-images.txt` | ghcr.io/nutanix-cloud-native | konvoy2 pins version in `pkg/capi/client/providers/providers.go` (v0.55.0) and EMBEDS the rendered manifests (`builtinrepo/components/runtime-extension-caren/`) + ClusterClass (`clusterclass/defaultmanifests/`) |
| **nkp-nutanix-product-catalog** | catalog collection OCI artifact + per-app artifacts; app charts; airgapped tar | `oci://ghcr.io/nutanix-cloud-native/nkp-nutanix-product-catalog/collection:<MAJOR.MINOR>` (NOT Docker Hub — DH is only a pull source for airgap); charts → `oci://ghcr.io/mesosphere/charts` | pinned in k-apps `applications/kommander/<ver>/helmrelease/cm.yaml` with `tag: ""` = defaults to running KommanderCore MAJOR.MINOR (NOT baked in binary) |
| **dkp-release** | base substrate: FIPS etcd, containerd pkgs, k8s tars/rpms/debs, airgap k8s image bundles | docker.io/mesosphere + S3 staging buckets | consumed by `bundle`/`dkp-image-builder` components |
| **dkp-release-specs** | the release plan YAMLs | merged to main → `gh dkp release` executes | THE authoritative version matrix |

## 2. The version-propagation chains (the load-bearing mechanics)

**Kommander's single-number trick:** a kommander git tag `v2.18.x` becomes (a) every image tag,
(b) chart versions, (c) the ldflag `common/pkg/version.Version` baked into every binary, and
(d) the kommander-applications artifact tag. At runtime the operator pulls k-apps at exactly
its own baked version — image tag and k-apps version are THE SAME NUMBER. (This is why the W1
ldflag omission was fatal: version="" → no k-apps pull → no AppDeployments.)

**Two k-apps delivery paths (differ by installer):**
- CLI path: `nkp install kommander` → fetch tarball `downloads.d2iq.com/dkp/<cli-version>/...`
  → applies imperatively: flux, git-operator, the 6 `common/<operator>` kustomizations, then
  KommanderCore + ManagementPlane CRs.
- In-cluster path: managementplane's DeployOperatorStep → Flux `OCIRepository
  kommander-applications-<ver>` → `ghcr.io/mesosphere/kommander-applications:<ver>` → commits
  content into the LOCAL kommander.git → flux reconciles from that git only.

**Chart chain (three merges to production):** charts PR (bump `Chart.yaml` version) →
`gh-pages` HTTP repo (auto) → **manual** `single-chart-ghcr.yaml` dispatch mirrors to ghcr OCI →
k-apps PR bumps `OCIRepository.spec.ref.tag`. Three independent version streams per app:
app-dir version (e.g. dex-k8s-authenticator `1.4.7`) ≠ chart tag (`1.4.3`) ≠ image tag
(`v1.4.5-d2iq` in cm.yaml).

**CAREN chain (embedded, not fetched):** internal repo tag → goreleaser → controller image +
rendered `runtime-extensions-components.yaml` + `*-cluster-class.yaml` assets → konvoy2's
`hack/capi/update-bootstrap-components.sh` + `update-cluster-class-templates.sh` re-embed them
into the binary. Addon chart versions: `make/addons.mk` → generated `helm-config.yaml` CM +
`repos.yaml` → bundle-initializer image → in-cluster `helm-repository` PVC serves
`oci://helm-repository.caren-system.svc/charts` → HCPs reference it.

**CAPI providers:** all pinned in ONE file `konvoy2/pkg/capi/client/providers/providers.go`
(CAPX v1.10.1, CAREN v0.55.0, CAAPH v0.6.2, core v1.12.4...); `make/env.mk` greps this file —
it is the single source of truth. Served in-cluster by CAPI-stack-operator as OCI bundles from
an in-cluster registry svc.

## 3. Registry inventory

- `docker.io/mesosphere` — kommander images, kommander/appmanagement charts, konvoy images, base substrate
- `ghcr.io/mesosphere` — kommander-applications OCI + server image, `charts/*` OCI mirrors, mindthegap, tooling
- `ghcr.io/nutanix-cloud-native` — CAREN images + assets, catalog collection, CAPI-stack-operator chart, cosi
- `mesosphere.github.io/charts` — HTTP helm repo (charts source of truth pre-mirror)
- `downloads.d2iq.com` / `s3://downloads.mesosphere.io` — binaries, tarballs, airgap bundles
- In-cluster: local `kommander.git` (flux's ONLY source), `helm-repository.caren-system.svc` (addon charts), CAPI-stack bundle registry svc

## 4. Change → fastest-path matrix (connects to CUSTOM-CHANGES-TESTING.md)

| You changed | Release-path (slow, correct) | Fast path onto a CLAIM (validated vectors) | Fast time |
|---|---|---|---|
| kommander controller Go (federation/, managementplane/, installer/) | tag kommander → all images rebuild | build ONE image → `override_image.py apply` | ~3 min |
| k-apps app config / values / versions | tag k-apps → 4 artifacts | git-push into claim's local kommander.git (suspend kommander-operator first) | ~30 s |
| a chart in mesosphere/charts | 3 merges (charts → ghcr mirror → k-apps pin) | push dev chart to any OCI reg (ttl.sh ok) → git-push the OCIRepository url/tag bump | ~4 min |
| CAREN handler code | tag CAREN → bump konvoy2 pin → re-embed → new binary + template | override CAREN controller image + (if HCP output changes) patch HCPs directly | ~5 min |
| CAREN addon version (cilium/CSI/...) | addons.mk → CAREN release → konvoy2 re-embed | patch the HCP version/valuesTemplate in-cluster (CAAPH layer of override_image.py) | seconds |
| catalog app (ndk/nai/...) | merge to release-2.x → auto republish collection (tag = MAJOR.MINOR, picked up on reconcile — no version bump needed!) | push collection to own OCI reg → patch the catalog OCIRepository url in-cluster; or annotate reconcile after real publish | ~2 min |
| kommander-cli install-flow Go | vendored → dkp-cli rebuild | rebuild nkp binary only; install flow only matters at TEMPLATE BUILD → new template generation | ~40 min once |
| konvoy2 Go / provider pins / ClusterClass | konvoy2 tag → binary + bootstrap image + bundles | rebuild binary → build+freeze new template generation; claims inherit | ~40 min once |
| kommander/k-apps `common/<operator>` manifests | ships inside k-apps | git-push vector (they live in the claim's kommander.git too) | ~30 s |

**Baking rule (unchanged):** once a fast-vector change is validated, either re-freeze the
modified claim as the next template generation (~7 min) or wait for the real release artifact
and build a fresh template from the released binary.

## 5. Corrections to prior beliefs

1. Product catalog publishes to **ghcr.io/nutanix-cloud-native**, not Docker Hub (DH creds are
   only used to *pull* private images when assembling the airgapped tar). Its tag is NOT in the
   nkp binary: the collection tag = NKP MAJOR.MINOR, defaulted in-cluster from KommanderCore's
   version; pin lives in k-apps' kommander app cm.yaml (`tag: ""`).
2. "kommander-applications image directly goes in release artifact" — partially: it ships as
   FOUR artifacts (S3 tarball for the CLI, ghcr OCI artifact for the in-cluster operator, a
   git-server image, and a collection artifact). The tarball/OCI pair is the load-bearing one.
3. The nkp binary does NOT contain kommander install logic natively — that's kommander-cli,
   vendored in as a Go module. Cluster-side and install-side change paths are independent.
4. charts→ghcr OCI mirroring is a MANUAL workflow_dispatch (`single-chart-ghcr.yaml` in
   k-apps), not automatic on charts merge — a often-missed step in "why isn't my chart there".

## 6. Why most repos are NOT built at release time (the two-tier model)

dkp-release-specs pins only 7 components by SHA, yet dozens of repos ship in the product.
Resolution: NKP is a two-tier release system.

- **Tier 1 (leaf repos)** — CAPX, CAREN, CCM, charts, catalog, cluster-api-operator, ... —
  release INDEPENDENTLY: their own CI cuts semver tags and publishes finished artifacts
  (images/charts/manifests) to registries. After that, their source is never needed.
- **Tier 2 (the 7 pinned repos)** — konvoy2, kommander, kommander-applications,
  kommander-cli, dkp-cli, dkp-image-builder, bundle — consume tier 1 BY REFERENCE via pin
  files in their own trees. A tier-2 SHA therefore transitively freezes the exact tier-1
  versions: konvoy2 SHA → providers.go (CAPX/CAREN/...) → CAREN version → addons.mk
  (cilium/CSI/CCM chart...); kommander-applications SHA → every app's chart tag + image tag;
  kommander SHA → all kommander image tags. The catalog is tag-implicit (MAJOR.MINOR at
  runtime) and pinned nowhere.
- At release build time only the 7 are compiled; leaf artifacts are pulled prebuilt (and
  re-mirrored into dkp-container-images / airgap bundles by the `bundle` component).
- Corollary: a leaf-repo change has NO official path to a cluster short of leaf release +
  tier-2 pin bump + integration rebuild — which is exactly the gap nkp-instant-build closes:
  build the leaf artifact from the dev SHA and substitute it at the same consumption point
  the release would have pinned.
