# Evidence — what has actually been run

Every claim below is from a real run with artifacts on disk. Where something
has *not* been run, it says so.

## A. Live runs, 2026-08-26 (`~/Documents/nkp/e2e-live-2026-08-26/`)

| Scenario | Result | Duration | Notes |
|---|---|---|---|
| `cluster-lifecycle` | ✅ **PASS** | 42.6 min | create (32 min incl. kommander core) → scale out 2→3 (~2.5 min) → scale in → reconciled → delete, **zero leftover VMs** |
| `day2-operations` | ✅ **PASS** | 39.7 min | run **from a PC DevVM**; app deployed via ClusterApp AppDeployment, drained worker, app survived, reconciled, deleted |

What the green run proves end-to-end: VIP/LB allocation, the corrected CLI
flags, kommander-core install inside `create --self-managed`, **scaling via
NKPCluster autoscaler annotations** (direct MachineDeployment scaling is
reverted by the topology controller — found by this framework on 2026-08-11,
fixed, now live-proven), reconcile gates, async-delete-tolerant cleanup.

## B. The fast path, live-proven 2026-08-26

| Stage | Time | Proof |
|---|---|---|
| Traditional create (per change; proves the create path) | 26 min | `build_template.sh qa-nrm3` — 14/14 core apps |
| Freeze | ~6 min | `FREEZE COMPLETE`; liveness post-condition: endpoint down |
| Claim `qa-c1` (new name/VIP/LB) | ~6 min window | 4 nodes Ready, 0 unhealthy pods, 0 HRs not ready |
| Identity gates | ~2 min | rc=0 — Available=True, TopologyReconciled=True, CP survived unpause |

The framework consumes claims natively — **`claim-sanity` PASS in 23.2 min**
(claim 405s → identity gates PASS → node/pod/API/reconcile asserts → sweep),
`e2e-live-2026-08-26-claim4/RESULTS.md`. Three failed attempts on the way,
each root-caused: an address contract (claim.py needs a single same-length LB
IP), a missing gates argument, and an SSO-tail wedge under three-way PE
contention (one stale-LB carrier outlives the non-fatal hydrate verify-loop —
known finding, next lever named).

## B2. What the framework has caught (real findings, not harness bugs)

1. **CAPX duplicate-VM name** (2026-08-11): two VMs with the same name wedged
   worker provisioning — product bug class this harness exists to catch.
2. **Topology-managed scaling**: `kubectl scale machinedeployment` silently
   reverted; worker count lives in autoscaler min/max annotations.
3. **`create --timeout` also clocks the kommander-core wait** — 60m is too
   small on a loaded PE; scenarios now pass 90m.
4. **Fixed-name kind bootstrapper = one build per machine**: a parallel
   template build was killed mid-pivot by `reset_bootstrap` (2026-08-26).
   The step now busy-waits when the bootstrapper carries someone else's
   cluster instead of deleting it under them.
5. **Prism deletes are async**: `assert_no_leftover_vms` polls up to 5 min.

## C. Not yet run live — stated plainly

| Scenario | Status |
|---|---|
| `day1-install` | dry-run verified; shares the live-proven create path |
| `platform-upgrade`, `app-upgrade` | dry-run verified; need `E2E_BASELINE_NKP_BIN` / `E2E_APP` pins |
| `single-node` | dry-run verified; experimental profile by design |

## D. Known limitation

`nkp` uses a fixed-name local bootstrap kind cluster, so cluster-building
scenarios run sequentially per machine. Parallel runs need one machine each
(DevVMs) — which is also the doc's Part-1 answer.
