# Dev E2E — demo notes

Run `./hack/dev-e2e/demo.sh` (press Enter between steps) or `--auto` to run
straight through. It takes about a minute and touches nothing, so it is safe in
front of an audience.

---

## 1. The problem (30 seconds)

Today a developer changing NKP can run unit tests and kuttl, but nothing that
answers *"does a cluster actually come up, and does day-2 still work?"* short of
raising a PR and waiting on CI, or hand-driving a cluster and remembering to
delete it afterwards.

These are the scenarios we want covered, from the plan:

| Scenario | What it validates | Environment |
|---|---|---|
| Day-1 install sanity | Minimal install, core components healthy | 1 CP + 1 worker |
| Day-2 operations | Scale, restart, cordon/drain, recovery | 1 CP + 1 worker |
| Cluster lifecycle | Create, reconcile, update, delete | 1 CP + 1 worker |
| Platform upgrade | Upgrade flow and post-upgrade health | Versioned baseline |
| App upgrade | App upgrade + rollback | Versioned baseline |
| Single-node | Sanity on a single-node profile | Single node (experimental) |

## 2. The shape (1 minute) — `demo.sh` steps 1–3

A scenario is **one ordered list of steps**, with two phases around it that the
**runner** owns:

```text
steps  ->  collect (on failure)  ->  cleanup (always)
```

Two properties worth calling out, because they are what make it safe to hand to
a team:

- **collect** runs automatically when a step fails — you get node/pod state,
  events, describes, pod logs and the Prism Central VM list without asking.
- **cleanup** runs *even when the scenario failed*. Clusters live on a shared
  Prism Element that is usually above 90% full; a harness that strands VMs on
  failure would be worse than no harness. `--keep` opts out when you want to
  inspect a live failure, and the runner prints how to reach it.

There is deliberately no prepare/execute/check split. Real tests interleave —
do a thing, check it, do the next thing — and forcing that into phases either
reorders the test or splits it across three places.
## 3. Writing a scenario (1 minute)

A scenario is **a YAML file in `scenarios/`** — no Python, no registry to edit:

```yaml
name: day2-drain-reconcile
description: Drain a worker, wait, then verify the cluster reconciles

steps:
  - group: build the cluster
  - create_cluster: {control_plane: 1, workers: 1}
  - wait_nodes_ready: {count: 2, timeout: 25m}

  - group: drain the worker, then let it settle
  - drain_node: {role: worker}
  - wait: {duration: 2m, reason: "allow rescheduling to settle"}
  - uncordon_node: {role: worker}

  - group: did it actually reconcile?
  - wait_reconciled: {timeout: 20m}
  - assert_node_count: {count: 2}
cleanup:
  - delete_cluster
  - assert_no_leftover_vms
```

The layout says the same thing:

```text
run_e2e.py     the runner
scenarios/     what you write   <- YAML only
framework/     the engine       <- you rarely open this
```

`run_e2e.py --steps` lists the vocabulary. Three points worth making:

- **Operate → wait → check reconciliation is first-class.** `wait_reconciled`
  asserts the CAPI Cluster's `Available` / `TopologyReconciled` conditions, not
  just pod health — a cluster whose topology controller has stopped reconciling
  looks perfectly healthy at the pod level.
- **Scenarios are parameterised from the environment** (`${E2E_APP}`), and
  `requires_env` fails before anything is built — not 40 minutes
  in.
- **Work and assertions interleave**, with `group:` labels, so a failure reads
  *step 8/11 'assert_node_count' [scale out]* rather than just "check failed".
- **Extending the vocabulary is one function.** Add a decorated step to
  `framework/steps.py` and every scenario can use it.

## 4. Show it running — `demo.sh` steps 4–9

- **Step 4** `--dry-run` prints the exact `nkp create cluster nutanix …` a real
  run would issue. Good for reviewing a scenario without spending 40 minutes.
- **Step 5–6** a passing scenario, then the `RESULTS.md` it produces — the same
  format the dev-VM pre-PR gate emits, so it can go straight onto a PR.
- **Step 7–8** a failing scenario: a step fails, diagnostics are still
  collected, cleanup still runs, `RESULTS.md` names the failing phase and
  reason, and the process exits non-zero so CI or a hook can gate on it.
- **Step 9** `junit-e2e.xml`, for wiring into CI reporting later.

## 5. What it found on its first real run (30 seconds — the honest bit)

The first live `day1-install` against the dev PC surfaced a genuine CAPX bug:
two VMs were created with the **same name**, and because CAPX resolves VMs by
name it could not decide which one to read, reporting

```text
VMAddressesAssigned=False
VMAddressesFailed: unable to determine network interfaces from VM: …-md-0-…
```

The worker Machine stayed wedged in `Provisioned`. The control plane was fine.
That is exactly the class of problem this harness exists to catch, and it is a
product issue rather than a harness one.

Getting that far also corrected five CLI flags, a missing `--self-managed`, a
Kubernetes version whose image does not exist on this Prism Central, and the
need to allocate a unique VIP and load-balancer range per cluster.

## 6. Status, plainly

- Framework mechanics: **validated** — discovery, phase ordering, the failure
  path, reports, secret redaction, IP allocation against the real network.
- `day1-install` against real infrastructure: **reached cluster creation and
  provisioned the control plane**, then hit the CAPX duplicate-VM bug above. Not
  yet a green end-to-end pass.
- The other five scenarios: written against the CLI and repo conventions,
  **not yet run live**. Expect the first run of each to need small corrections,
  the same way `day1-install` did.

## 7. What we would like from the room

- Which scenario matters most to get green first.
- Whether the shared dev PC has the capacity for routine runs, or whether these
  should target a dedicated environment.
- Volunteers to write the next scenario — the template is one file.
