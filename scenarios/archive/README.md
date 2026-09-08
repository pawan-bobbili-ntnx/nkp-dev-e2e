# Archived scenarios

Not discovered by `run_e2e.py` (discovery is not recursive). Kept because they
record how things were proven, not because they are templates:

- `demo-full-loop` is NOT here: it is the canonical full-loop scenario and lives in `scenarios/`.
- `demo-bit*`, `demo-live*`, `demo-full-loop-workload` — recording aids for the 2026-09 framework presentation; several
  depend on a specific cluster having been left running.
- `proving-*` — experiments that settled a question (e.g. that `nkp upgrade
  workspace` moves app VERSIONS, not chart content). The finding is recorded
  in `docs/SCENARIO-REFERENCE.md`; the file is the evidence.
- `claim-preserve-control` — a control run for the claim path's LB rewrite.

To run one: `mv` it back into `scenarios/`.
