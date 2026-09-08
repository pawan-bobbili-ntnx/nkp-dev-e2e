# Demo runbook

Two terminals. One is recorded and shows framework-style logs; the other is
yours and never appears. The cluster is real throughout, so the NKP dashboard
can be opened at every pause.

## Before the recording (not on camera, ~25 min)

Build the cluster and get istio-helm onto it:

    ./run_e2e.py demo-live-prep --keep

`--keep` matters: the cluster has to outlive the run, because the recorded half
drives the same cluster afterwards. When it finishes, confirm the cluster and
grab the dashboard details:

    ./demo_drive.py --dashboard

## The recording

**Terminal A — the one on camera.** Nothing prints until the driver says so:

    ./demo_console.py --reset

**Terminal B — yours, off camera.** This runs the real commands and narrates
into terminal A:

    ./demo_drive.py --console

Then the three bits play out in terminal A, pausing after each one:

| bit | what the screen shows | what you do at the pause |
|---|---|---|
| `show` | the real nodes, the AppDeployment, `minReplicas: 1` from the override, and the dashboard URL and credentials | open the dashboard, show istio-helm enabled |
| `deliver` | one change-set from **three** repositories being resolved to commits, built, pushed, and pointed at | back to the dashboard — the apps are reconciling |
| `verify` | four assertions, each naming what it proved | the dashboard once more: this change is live, pre-merge |

A pause prints the framework's own `AUTOMATION STOPPED` banner and waits.
**Press Enter in terminal A** (the recorded one) to continue — the driver in
terminal B blocks until you do, so the screen and the work never drift apart.

## If you want to run one bit again

    ./demo_drive.py --console --bit deliver

## If something goes wrong mid-take

Terminal A is only a renderer: `Ctrl-C` it and start it again with `--reset`,
and nothing about the cluster changes. Re-run whichever bit you were on. The
cluster is untouched by restarting either process.

## Driving the screen by hand

The console renders whatever is appended to its control file, so anything can
be said on camera without a script:

    echo '@banner istio-helm is reconciling the new chart' >> .demo-console
    echo '@dim   this is the chart the developer built, not the released one' >> .demo-console

`./demo_console.py --help` lists every directive.

## What is real

Everything. The cluster, the apps, the images built from the demo branches, the
registry pushes, the assertions. `demo_console.py` chooses **when** a line
appears on screen; it never invents one — every line originates from a real
command run by `demo_drive.py` against the real cluster.

The one thing deliberately left out of the recorded half is
`nkp upgrade kommander`, which since 2026-09-02 stalls for 40 minutes waiting
for platform apps to move and then fails. `deliver_changes` puts the same
change-set on the cluster through the same machinery and the same
content-addressed tags. See `scenarios/demo-live.yaml`.
