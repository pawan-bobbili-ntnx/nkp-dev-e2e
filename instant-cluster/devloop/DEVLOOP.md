# NKP dev-loops — test a change on top of a released version, fast

Companion to `../RELEASE-ARTIFACT-MAP.md` (what a release is made of) and
`../CUSTOM-CHANGES-TESTING.md` (the vector mechanics + traps). These scripts operationalize
the change→fastest-path matrix against a live instant-cluster claim.

Release worktrees (build your change on latest release content, personal checkouts untouched):
`~/Documents/nkp/release-2.18/{konvoy2,kommander,kommander-applications,kommander-cli,caren,charts,catalog}`
(kommander @ tag v2.18.0; others @ release-2.18 / release/v0.52.x / release-2.x / charts@master)

## The loop

```
claim a cluster (~9 min, from the frozen template)        # once per session, reusable
edit code in the release-2.18 worktree of the repo you're changing
run the matching devloop script (seconds..minutes)         # repeat as you iterate
revert / re-run / teardown when done
```

| Your change (repo) | Script | Time | Mechanism |
|---|---|---|---|
| kommander controller Go → an image | `devloop-image.sh <kc> <component>=<ref>` (or `--local <docker-tag>` to push via ttl.sh) | ~3 min | override_image.py 3-layer master-settings override |
| kommander-applications app config/values/version | `devloop-kapps.sh <kc> <kapps-dir> <app>` | ~30 s | token-rendered app dir → claim's kommander.git → flux |
| charts chart templates/values | `devloop-chart.sh <kc> <charts-dir> staging/<chart> <app>` | ~4 min | dev chart → ttl.sh OCI → OCIRepository url/tag rewrite in claim git |
| CAREN addon version/values (cilium/CSI/CCM…) | `devloop-image.sh` CAAPH layer / patch HCP valuesTemplate | seconds | HelmChartProxy is the master setting |
| CAREN handler Go | `devloop-image.sh <kc> caren=<ref>` (+ HCP patch if output changes) | ~5 min | controller Deployment patch |
| product-catalog app | `devloop-catalog.sh <kc> <catalog-dir>` (experimental) | ~2 min | dev collection → ttl.sh → kommander-overrides CM repoints the catalog OCIRepository |
| konvoy2 / kommander-cli Go (create/install flow) | rebuild binary → `build_template.sh` → freeze → claim | ~40 min once | flow only runs at template build; claims inherit |

## Validation record (qa-cd1 claim, 2026-08-06)

- **kapps vector**: reloader values edit from the release-2.18 worktree → deployment label live
  **E2E 58 s** (script 47 s). Root lesson baked into the scripts: *helm-controller does not watch
  valuesFrom ConfigMaps* — git+kustomization+CM all update and nothing rolls until the HR gets a
  `reconcile.fluxcd.io/requestedAt` nudge (`reconcile_hr` in `_lib.sh`).
- **chart vector**: dex-k8s-authenticator template edit → packaged 1.4.3-dev.* → ttl.sh →
  OCIRepository url+tag rewrite → HR reports dev chartVersion, pod Running with the edit (~4 min).
  Gotchas fixed: the operator-rendered yaml QUOTES the url; the chart OCIRepository object is
  named `<app>-<appVersion>-chart` (read it from the HR's chartRef).
- **image vector** (incl. `--local` docker→ttl.sh leg): reloader ran a ttl.sh dev image, revert
  restored ghcr pristine. Two override_image.py bugs found+fixed: duplicate top-level YAML keys
  when overriding repo+tag together (last-key-wins silently dropped the tag → ImagePullBackOff),
  and the missing HR nudge after CM write. **On Apple Silicon**: `docker pull` grabs arm64 —
  build/pull `--platform linux/amd64` or pods crash with exec format error; and re-pushing the
  SAME tag won't repull (kubelet cache) — always use a fresh tag per iteration.
- **catalog vector**: artifact build+push works (37 artifacts via `nkp experimental catalog
  release`), overrides CM applies — but these lab clusters have NO catalog OCIRepository even at
  baseline (catalog subsystem not enabled by the template's install), so end-to-end verification
  is blocked until a template is built with catalog enabled. Script kept as experimental.

## Rules that keep you honest

1. **kommander-operator must stay suspended** while your git edits matter — scripts suspend/resume
   around each push, but for a long session scale it to 0 yourself (scripts print the command).
2. **HR `metadata.labels` cannot carry a test signal** (AppDeployment json-patch replaces the map).
   Test through spec fields or values.
3. **ttl.sh artifacts expire in 24 h** and are anonymous/public — never push proprietary bits you
   can't ship; use `DEVLOOP_REGISTRY=<your-registry>` for private work (docker/helm login yourself).
4. **Baking:** a validated change becomes permanent by re-freezing the claim (next template
   generation, ~7 min) or waiting for the real release artifact.
5. Everything here touches only the claim's own git/objects — concurrent claims stay isolated.
