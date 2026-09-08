# How a test run works — architecture, failure path, pause

## The run lifecycle

```mermaid
flowchart TD
    A["./run_e2e.py my-scenario"] --> B["spec.py loads scenarios/my-scenario.yaml<br/>unknown step = fails HERE, <br/>(bad options: caught by --dry-run)"]
    B --> C["Config.from_env<br/>resolved run header printed<br/>(binary, PC, image, VIP pool)"]
    C --> D["guard rails: PC reachable?<br/>scenario env: defaults applied"]
    D --> E["STEPS - top to bottom"]
    E -->|every step OK| H["cleanup list"]
    E -->|a step fails| F["COLLECT list<br/>kubectl dumps + nkp diagnose<br/>support bundle -> artifacts dir"]
    F --> H
    H --> I["RESULTS.md + junit-e2e.xml<br/>in e2e-results/<timestamp>/"]

    style F fill:#7c2d2d,color:#fff
    style H fill:#1f4e3d,color:#fff
```



Three lists, three guarantees:

1. **steps** stop at the first failure — nothing after a broken step can be
  trusted, so nothing after it runs.
2. **collect** runs only on failure (or always, with `--always-collect`)
3. **cleanup** always runs — pass or fail, the cluster and its VMs go away
  (or, for `finish_cluster`, get frozen). Every cleanup step is  
   best-effort: one failing cleanup step logs and continues instead of  
   aborting the rest. `--keep` skips cleanup when you want the failure live.

## What a failure leaves you

```
e2e-results/20260827-091428/
├── RESULTS.md                       <- table: scenario, verdict, duration
├── junit-e2e.xml                    <- CI-consumable
├── run.log
└── smart-sanity/
    ├── scenario.log                 <- every command, redacted, timestamped
    ├── claim-....log                <- long subprocess logs, one file each
    └── diagnostics-failure/
        ├── nodes.txt, pods-all.txt, events.txt, helmreleases.txt ...
        └── support-bundle-....tar.gz   <- nkp diagnose, openable locally
```

## The pause step

```mermaid
sequenceDiagram
    participant R as runner
    participant C as cluster
    participant Dev as developer

    R->>C: steps run...
    R->>R: pause: reached
    Note over R: prints WHERE the run stopped,<br/>kubeconfig path, resume instructions
    R--)Dev: waiting (nothing touches the cluster)
    Dev->>C: kubectl / ssh / poke around freely
    alt interactive terminal
        Dev->>R: press Enter
    else background / CI run
        Dev->>R: touch artifacts/resume
    end
    R->>C: remaining steps continue
    alt timeout (default 2h) expires
        R->>R: pause FAILS -> collect + cleanup still run
    end
```



Why the timeout fails instead of resuming: a forgotten pause must not leak a
cluster overnight on the shared PE. Failing routes the run through the normal
collect + cleanup path, so even the forgotten case ends with a support bundle
and zero debris.

