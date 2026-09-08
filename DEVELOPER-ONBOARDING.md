# NKP Dev Testing — developer onboarding

Everything you need, once, to test your changes every way this repo supports.
(All clusters run on the dev Prism Central — nothing touches other envs.)

## One-time setup

| What | How | Why |
|---|---|---|
| Prism Central creds | `cd <tam-cli> && ./tam login pc-dev` → exports `NUTANIX_USER`/`NUTANIX_PASSWORD` (temporary; re-run when they expire) | every cluster/VM operation |
| GitHub auth | `gh auth login` (SAML-authorize for mesosphere, nutanix-cloud-native, nutanix-core) | private Go modules, release downloads |
| GHCR push token (optional, for dev-image delivery via your own ghcr) | classic PAT with `write:packages` → save as `~/.nkp-dev-registry.env` (`chmod 600`):<br>`export GHCR_USER="<gh-user>"`<br>`export GHCR_TOKEN="ghp_..."` | pushing dev controller images; ttl.sh is the zero-setup default |
| ghcr package visibility (one-time, EVER) | after your first-ever push, flip the single `nkp-dev` package **public** at github.com/users/`<you>`/packages → `nkp-dev` → settings | GitHub has NO API for this (web UI only); public = cluster nodes pull anonymously. ALL dev images live in this ONE package - image name and sha are both in the TAG - so this is the only flip you will ever do |
| SSH key on cluster nodes | `E2E_SSH_PUBLIC_KEY` (+ `E2E_SSH_USERNAME=konvoy`) | without it a broken cluster is undebuggable |
| devbox | https://www.jetify.com/devbox | CI-parity toolchain everywhere |

Known local-machine traps (all auto-handled by the scripts, listed so you
recognize them elsewhere): an exported `GOROOT` in your shell poisons devbox
Go builds; a GOCACHE shared across toolchains does too; `pgrep -f` matches its
own command line — bracket the first character.

## Test your change, by situation

| You changed | Run |
|---|---|
| anything, quick pre-PR check | `./hack/dev-vm/<repo>-pc-test.sh run --target local --suites "unit lint"` |
| anything, full CI parity | same with `--target devvm` (dynamic VM on the dev PC, self-deletes) |
| the CLI (konvoy2) | `NKP_BIN=$(./build-nkp-fast.sh -q)` then any e2e scenario — build is ~1s unchanged / ~60s per change |
| kommander / kapps / CAREN, want it IN a cluster | write a request YAML (repos + branches, local branches resolve first) → `./e2e-from-request.sh my-change.yaml` — claims a cluster (~10 min) and injects exactly what changed |
| upgrade-path behavior | `E2E_BASE_NKP_VERSION=v2.17.0 ./run_e2e.py platform-upgrade` — the base version's CLI creates the cluster, YOUR binary upgrades it |

## E2E scenarios — the contract

`steps` (interleaved actions+assertions) → `collect` on failure (state dumps
**plus the `nkp diagnose` support bundle**, in your artifacts dir) → `cleanup`
always. Every assert takes `timeout:`. `pause:` hands you the cluster
mid-scenario (Enter or touch-file to resume). Add a scenario = drop a YAML in
`scenarios/` — `run_e2e.py --steps` lists the 40+ step vocabulary.

## Where dev artifacts go

| Artifact | Destination |
|---|---|
| dev controller images | `ghcr.io/<you>/nkp-dev:<image>-dev-<sha8>` - ONE package, image+sha in the tag (set `registry:` + `registry_login:` in nkp-instant-build/config.yaml) |
| kapps content | pushed into the claim's own in-cluster git |
| CLI binaries | `~/.cache/nkp-fast-build/<content-key>/` |
| build cache index | `~/.cache/nkp-instant-build/` |

## Naming conventions (concurrency + caching)

| Artifact | Convention |
|---|---|
| dev images | `ghcr.io/<gh-user>/nkp-dev:<image>-dev-<sha8>` - per-developer namespace (no collisions), ONE stable package (`nkp-dev`) flipped public exactly once ever, image name + sha both in the TAG (content-addressed; the build cache maps 1:1 to a durable ref) |
| frozen templates | `nkp-tmpl-<base>-g<N>` (e.g. `nkp-tmpl-v2.18.0-g1`); change-set templates `nkp-tmpl-<base>-<changeset8>` |
| clusters/claims | `<user>-<purpose>`, lowercase RFC 1123, max 32 chars (enforced fail-fast) |

## Fast-path (claim) model

Frozen GA template → each request claims a clone (~10 min) → deltas injected
(cached; repeat of same commit skips builds). First cluster per GA is built
traditionally once (`build_template.sh` + `freeze.py`). Roadmap: change-set
freezing — build CLI → create traditionally → inject once → freeze → every
scenario claims a cluster *born with* your changes.

## When a run fails: the two failures that are not your change

Most red runs are the change under test. These two are not, and both cost
real time before they were made legible.

**"failed to wait for HelmRelease kommander-appmanagement" during install.**
Almost always Docker Hub's anonymous pull limit, not a broken cluster. The
`kommander` and `kommander-appmanagement` charts come from
`oci://docker.io/mesosphere/...` on both the 2.17 and 2.18 lines, while every
other chart comes from `ghcr.io` - so those two are the ones that fail when
the shared lab egress IP exceeds 100 pulls per 6h. `install_platform` now
detects this, skips its retry (which would waste another ~12 minutes) and
prints the registry's own `TOOMANYREQUESTS` message plus the remedies. Check
it yourself with:

```sh
kubectl get ocirepository -A          # look for READY=False on the docker.io ones
```

Note `--registry-mirror-url` does **not** fix this: it configures containerd
on the nodes, while flux's source-controller pulls charts itself over HTTPS.

**A scenario passes but tested nothing.** Both upgrade verbs no-op silently
on a same-version baseline and still exit 0 - `upgrade cluster nutanix`
(konvoy2 `cluster/upgrade.go:214-218`) and `upgrade kommander`
(`kommander-cli/pkg/upgrade/upgrade_nkpcluster_helper.go:39-41`). This is why
every upgrade scenario starts from the frozen GA baseline and pairs its
upgrade with a before/after assertion. If you change an upgrade scenario's
`create_cluster:` topology, you silently stop claiming that baseline and
start rebuilding it - the change-set hash covers topology.

### Two more that are not your change (added 2026-08-30)

**"Waiting for all enabled applications to be ready ... timed out" — check
your Prism Central credentials.** They are temporary generated users and they
expire *mid-session*. When they do, the in-cluster CSI can no longer resolve
the storage container, a PVC sits `Pending`, its pod never schedules, and
Helm hits its deadline. Nothing in that chain mentions credentials. The tell
that misleads: PVCs created EARLIER in the same cluster are still `Bound`,
because the cluster outlived its credentials — so it reads as a capacity
problem. `install_platform` now names it for you; if you are diagnosing by
hand:

```sh
kubectl get pvc -A | grep -v Bound          # Pending?
kubectl describe pvc <name> -n <ns>         # ProvisioningFailed message
```

Refresh with `./tam login pc-dev` and rewrite ALL FOUR keys in
`speedstart-state/smoke-creds.env` (`NKP_NUTANIX_*` **and** `NUTANIX_*`).
Note you cannot sweep a stranded cluster until you do — sweeping talks to
Prism Central too.

**The CLI gives up before the cluster does.** Twice in one day a "failure"
was a premature timeout: `nkp create cluster` gave up at 11/14 core
applications and the same cluster reached 14/14 on its own; and a
Docker-Hub-throttled install completed unattended once the rate-limit window
rolled. **Before rebuilding a cluster that failed, check whether it finished:**

```sh
kubectl get kommandercore -A -o jsonpath='{.items[0].status.version}'   # set = installed
kubectl get hr -A | grep -v True                                        # empty = healthy
```

A cluster kept for inspection is usually re-usable — `finish_cluster` keeps
one whose create failed part-way precisely so you can.
