# What to export before running scenarios

Every knob the framework reads, why it exists, and what happens if you skip
it. Source of truth: `framework/config.py` (`Config.from_env`) — this table
mirrors it.

## The minimum for any live run

```bash
# Prism Central credentials (rotate every few days - refresh via tam login)
export NUTANIX_USER="<pc-user>"
export NUTANIX_PASSWORD="<pc-password>"

# the CLI under test - use build-nkp-fast.sh output, or a GA binary
export NKP_BIN=/path/to/nkp

# NOTE: the node image + kubernetes version are create_cluster OPTIONS in
# the scenario yaml (machine_image: / kubernetes_version:) - they belong to
# the test case, not the terminal. The env vars below remain only as a
# fallback for scenarios that do not set them:
# export E2E_MACHINE_IMAGE=...   export E2E_KUBERNETES_VERSION=...
# (claims don't need an image at all, and workload clusters inherit
#  version+image from their management cluster automatically)
```

A convenient pattern: keep creds in a `chmod 600` env file and
`set -a; source creds.env; set +a` — never paste secrets into scenario files
or shell history. Secret values are never echoed by the framework
(`shell.py` redacts them from every logged command).

## Fast path (claims / create_cluster)

```bash
export SPEEDSTART_DIR=~/Documents/nkp/speedstart-state   # frozen-template state home
```

That is ALL. Which template to claim — or whether to create traditionally
instead — is the framework's call, not yours: `create_cluster` hashes
(base | components | topology), looks the hash up in
`$SPEEDSTART_DIR/templates.json`, and claims on a hit or creates-and-freezes
on a miss. Template names, VIPs and LBs live only in that registry, written
by `finish_cluster` at freeze time. You never export them.

(`E2E_TEMPLATE`/`_VIP`/`_LB` still exist as env overrides for claim-machinery
debugging — if you find yourself exporting them for a normal run, the
registry is missing an entry that a first `create_cluster` run would create.)

`SPEEDSTART_DIR` holds the freeze manifests, `templates.json` (the
change-set → template registry `create_cluster` reads), the etcd binaries the
claim pipeline ships to seeds, and claim kubeconfigs. Without it everything
falls back to the instant-cluster directory — fine on a machine that only
ever claims one template, wrong everywhere else.

Note: `claim_cluster` is internal machinery (create_cluster calls it with
identity from the registry). Only machinery-test scenarios like claim-sanity
invoke it directly, with explicit YAML options.

If dev-e2e does not live next to `instant-cluster` anymore:

```bash
export NKP_INSTANT_CLUSTER_DIR=~/Documents/nkp/konvoy2/hack/instant-cluster
```

## Change-set identity (create_cluster)

```bash
export E2E_BASE=v2.18.0                      # base NKP version (default v2.18.0)
export E2E_COMPONENTS="kommander@abc123,kommander-applications@def456"
```

These two + the topology form the hash that decides claim-vs-create. Empty
`E2E_COMPONENTS` = vanilla base. The instant-build orchestrator exports both
when it hands a request off to e2e.

## Upgrade scenarios

```bash
export E2E_BASE_NKP_VERSION=v2.17.0          # resolve_baseline fetches GA binaries
# OR, if you already have the old binary:
export E2E_BASELINE_NKP_BIN=/path/to/old/nkp
export GH_TOKEN=$(gh auth token)             # for the GitHub release download
# if the base version needs a different node image / k8s:
export E2E_BASE_MACHINE_IMAGE=...
export E2E_BASE_KUBERNETES_VERSION=...
```

## Registry (dev images via the instant-build pipeline)

```bash
# ~/.nkp-dev-registry.env  (chmod 600)
export GHCR_USER="<github-username>"
export GHCR_TOKEN="ghp_..."     # scopes: write:packages (+ delete:packages)
```

One-time after your FIRST ever push: flip the single `nkp-dev` package public
at github.com/users/`<you>`/packages → `nkp-dev` → settings. All dev images
live in that one package (`ghcr.io/<you>/nkp-dev:<image>-dev-<sha8>`), so
this is the only visibility flip you will ever do — GitHub has no API for it.

## Scenario knobs (interpolated inside the YAML)

Scenario files parameterise themselves with `${VAR:-default}`; these are not
framework config, just per-scenario inputs. A `${VAR}` WITHOUT a default is
that scenario's way of saying "you must export this to run me" - it fails
with the variable named. `--dry-run` prints every resolved value.

| variable | used by | meaning / default |
|---|---|---|
| `E2E_APP` / `E2E_APP_VERSION` | developer-flow (`istio` / `1.23.6` via its env: block) | which catalog app + version to deploy |
| `E2E_APPLICATIONS_REPOSITORY` | platform-upgrade (empty = GA) | override applications repo for `nkp upgrade` |
| `E2E_BASE_NKP_VERSION` / `E2E_BASELINE_NKP_BIN` | platform-upgrade | the OLD version to install first (see Upgrade scenarios above) |
| `E2E_BASE_MACHINE_IMAGE` / `E2E_BASE_KUBERNETES_VERSION` | platform-upgrade | node image/k8s for the BASELINE create when the base version needs different ones |

## Everything else (defaults are right for the dev PC)

| variable | default | why you'd change it |
|---|---|---|
| `PC_URL` | `https://<your-prism-central>:9440` | different Prism Central |
| `NUTANIX_PRISM_ELEMENT_CLUSTER_NAME` | `<prism-element-cluster>` | different PE |
| `NUTANIX_SUBNET_NAME` | `<subnet>` | different subnet |
| `NUTANIX_STORAGE_CONTAINER_NAME` | `SelfServiceContainer` | |
| `E2E_KUBERNETES_VERSION` | `v1.33.2` | must pair with `E2E_MACHINE_IMAGE` |
| `E2E_VIP_POOL` | `<first-ip>-<last-ip>` | narrow it to give two concurrent runs disjoint pools |
| `E2E_CLUSTER_PREFIX` | `e2e-$USER` | cluster/VM name prefix (RFC-1123) |
| `E2E_KUBECONFIG` | — | for `use_existing_cluster` scenarios |
| `E2E_SSH_PUBLIC_KEY` | `~/.ssh/id_ed25519.pub` | NEVER omit for created clusters — no key = no way into the nodes when something breaks |
| `E2E_SSH_USERNAME` | `konvoy` | |
| `E2E_CONTROL_PLANE_MEMORY` | CLI default | bigger CP for heavy platform runs |
| `E2E_REGISTRY_MIRROR_URL` / `_USERNAME` / `_PASSWORD` | — | mirror WITHOUT creds wedges CAREN mid-reconcile: the three travel together |
| `E2E_TIMEOUT_MINUTES` | 60 | global default step budget |

## Why env vars and not a config file?

Scenarios must be able to run identically on a laptop, the DevVM, and CI. Env
is the one interface all three share, it composes with `${VAR:-default}`
interpolation inside scenario YAML, and secrets stay out of files that get
committed or copied. `--dry-run` prints the resolved config header so you can
see exactly what a run would use before spending a cluster on it.
