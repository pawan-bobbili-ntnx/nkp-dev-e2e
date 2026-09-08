#!/usr/bin/env python3
# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run NKP dev E2E scenarios against the dev Prism Central.

    export NUTANIX_USER=... NUTANIX_PASSWORD=...     # ./tam login pc-dev
    ./run_e2e.py --list
    ./run_e2e.py day1-install
    ./run_e2e.py --all --keep
    ./run_e2e.py --all --dry-run                     # prints the plan, touches nothing

The ``nkp`` binary is taken from the current directory by default (NKP_BIN
overrides). Every scenario is steps -> collect (on failure) -> cleanup;
cleanup runs even when something failed, unless --keep is given.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework.config import Config, ConfigError, load_env_file  # noqa: E402
from framework.core import Runner, ScenarioResult  # noqa: E402
from framework.logging_ import Log  # noqa: E402
from framework.report import write_junit, write_results_md  # noqa: E402
from framework.discovery import discover  # noqa: E402
from framework.spec import SpecError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    # FIRST, before anything reads the environment. Settings come from
    # ./nkp-e2e.env (then ~/.nkp-e2e.env) so a fresh shell needs no exports; a
    # real environment variable still wins, so a one-off override on the
    # command line keeps working.
    #
    # Order matters and is not obvious: discover() parses the scenario YAML,
    # and that is where ${NKP_BIN} / ${NKP_REPOS_DIR} are interpolated. Loading
    # the file after discovery expanded them against an empty environment, so
    # bin_path became "" and applications_repository became
    # "/kommander-applications" - silently, with the run continuing.
    env_file = load_env_file()

    try:
        scenarios = discover()
    except SpecError as exc:
        # A malformed YAML scenario is a config mistake; report it as one.
        print(f"ERROR in scenario config: {exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        prog="run_e2e.py",
        description="Run NKP dev E2E scenarios on the dev Prism Central.",
    )
    parser.add_argument("scenario", nargs="*", help="scenario name(s); see --list")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--steps", action="store_true",
                        help="list the steps available to YAML scenarios and exit")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would run without touching Prism Central")
    parser.add_argument("--keep", action="store_true",
                        help="leave clusters in place afterwards (for debugging)")
    parser.add_argument("--always-collect", action="store_true",
                        help="collect diagnostics even when a scenario passes")
    parser.add_argument("--artifacts", type=Path, default=None,
                        help="output directory (default e2e-results/<timestamp>)")
    parser.add_argument("--timeout-minutes", type=int, default=None,
                        help="per-cluster-operation timeout (default 60)")
    parser.add_argument("--env-file", default=None,
                        help="settings file (default: ./nkp-e2e.env, ~/.nkp-e2e.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.steps:
        import inspect

        from framework.steps import STEP_DOC, STEPS

        print("Steps available to YAML scenarios. Options follow each name; "
              "a trailing * marks one the scenario must set.\n")
        for step_name in sorted(STEP_DOC):
            opts = []
            for prm in inspect.signature(STEPS[step_name]).parameters.values():
                if prm.name == "ctx":
                    continue
                if prm.default is inspect.Parameter.empty:
                    opts.append(f"{prm.name}*")
                else:
                    opts.append(f"{prm.name}={prm.default!r}")
            print(f"  {step_name}")
            print(f"      {STEP_DOC[step_name]}")
            if opts:
                print(f"      {', '.join(opts)}")
        print("\nUse them under steps/collect/cleanup in a .yaml scenario "
              "(docs/SCENARIO-REFERENCE.md explains each).")
        return 0

    if args.list or (not args.scenario and not args.all):
        print("Available scenarios:\n")
        for name, cls in scenarios.items():
            kind = "yaml" if getattr(cls, "source", "").endswith((".yaml", ".yml")) else "python"
            print(f"  {name:<24} [{kind}] {cls.description}")
            print(f"  {'':<24} topology: {cls.topology}  (~{cls.expected_minutes} min)")
        print("\nRun one with:  ./hack/dev-e2e/run_e2e.py <name>")
        print("Everything:    ./hack/dev-e2e/run_e2e.py --all")
        return 0 if args.list else 2

    selected = list(scenarios) if args.all else args.scenario
    unknown = [s for s in selected if s not in scenarios]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(scenarios)}", file=sys.stderr)
        return 2

    try:
        config = Config.from_env(
            artifacts_dir=args.artifacts,
            dry_run=args.dry_run,
            keep=args.keep,
            always_collect=args.always_collect,
            timeout_minutes=args.timeout_minutes,
        )
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    _env_note = env_file or "environment only (no nkp-e2e.env found)"
    log = Log(verbose=args.verbose, path=config.artifacts_dir / "run.log")
    log.banner("NKP dev E2E")
    for label, value in config.describe():
        log.info(f"  {label}: {value}")
    total = sum(scenarios[s].expected_minutes for s in selected)
    log.info(f"  scenarios: {', '.join(selected)} (~{total} min total)")
    log.info(f"  settings:  {_env_note}")
    log.info(f"  artifacts: {config.artifacts_dir}")

    runner = Runner(config, log)
    results: list[ScenarioResult] = []
    for name in selected:
        result = runner.run(scenarios[name]())
        result.topology = scenarios[name].topology  # for the report table
        results.append(result)

    results_md = config.artifacts_dir / "RESULTS.md"
    write_results_md(results, config, results_md)
    write_junit(results, config.artifacts_dir / "junit-e2e.xml")

    log.banner("summary")
    for r in results:
        log.info(f"  {r.name:<20} {r.status:<5} {r.seconds / 60:>5.1f} min")
    log.info(f"\nresults: {results_md}")

    failed = [r for r in results if not r.ok and not r.skipped_reason]
    if failed and not config.keep:
        log.info("clusters were cleaned up; re-run with --keep to inspect a failure live")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
