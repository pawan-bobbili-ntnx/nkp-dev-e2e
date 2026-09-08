#!/usr/bin/env python3
"""Replay a captured dev-e2e run in the terminal, at recording pace.

This is a REPLAY of a real run - every line is what that run actually printed.
Nothing here is synthesised. The default header says which run and when, so a
viewer is never misled into thinking it is happening live; pass --no-header to
drop it if you are narrating that yourself.

Why it exists: recording a live run means waiting out a 7-minute claim and a
10-minute upgrade in silence, and any flake costs the take. Replaying a known
green run gives the same terminal, in the time you choose, deterministically.

  ./replay_run.py                          # newest green demo-full-loop run
  ./replay_run.py --speed 4                # 4x faster than real time
  ./replay_run.py --max-gap 3              # never wait more than 3s
  ./replay_run.py --list                   # show replayable runs
  ./replay_run.py --run e2e-results/2026…/demo-full-loop/scenario.log
"""
import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]   # repo root: e2e-results/ lives there
TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")
# the framework logs whole `kubectl get -o json` payloads for diagnostics -
# 194k of run 17's 202k lines. Useful in artifacts, noise on a screen.
JSON_NOISE = re.compile(r'^\s*\|\s*[\[\]{}"]|^\s*\|\s*$|^\s*\|\s+"[\w.-]+":')
PAUSE_MARK = "AUTOMATION STOPPED"


def find_runs():
    out = []
    for log in sorted((HERE / "e2e-results").glob("*/*/scenario.log")):
        try:
            head = log.read_text(errors="replace")[:4000]
        except OSError:
            continue
        if "DRY RUN" in head:
            continue
        res = log.parent.parent / "RESULTS.md"
        ok = res.is_file() and "✅ PASS" in res.read_text(errors="replace")
        out.append((log, ok))
    return out


def presentable(path):
    """The lines worth showing, with the seconds-since-previous for each."""
    rows, last = [], None
    for raw in path.read_text(errors="replace").splitlines():
        if JSON_NOISE.match(raw):
            continue
        m = TS.match(re.sub(r"\x1b\[[0-9;]*m", "", raw))
        gap = 0.0
        if m:
            t = datetime.strptime(m.group(0).strip(), "%H:%M:%S")
            if last is not None:
                gap = (t - last).total_seconds()
                if gap < 0:              # midnight rollover
                    gap += 86400
            last = t
        rows.append((raw, gap))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", help="path to a scenario.log to replay")
    p.add_argument("--speed", type=float, default=1.0,
                   help="playback speed multiplier (default 1.0 = real time)")
    p.add_argument("--max-gap", type=float, default=6.0,
                   help="cap any single wait, in seconds (default 6)")
    p.add_argument("--list", action="store_true", help="list replayable runs")
    p.add_argument("--no-header", action="store_true",
                   help="omit the 'replay of <run>' header line")
    p.add_argument("--no-pause", action="store_true",
                   help="do not wait for Enter at the run's pauses")
    a = p.parse_args()

    runs = find_runs()
    if a.list:
        for log, ok in runs:
            print(f"  {'PASS' if ok else 'fail'}  {log.relative_to(HERE)}")
        return 0

    if a.run:
        log = Path(a.run)
    else:
        green = [l for l, ok in runs if ok and "demo-full-loop" in str(l)]
        if not green:
            print("no green demo-full-loop run found; pass --run", file=sys.stderr)
            return 1
        log = green[-1]
    if not log.is_file():
        print(f"no such log: {log}", file=sys.stderr)
        return 1

    rows = presentable(log)
    if not a.no_header:
        run_id = log.parent.parent.name
        print(f"\033[2m— replay of {log.parent.name} run {run_id} "
              f"({len(rows)} lines, captured output) —\033[0m\n")

    for raw, gap in rows:
        wait = min(gap / max(a.speed, 0.01), a.max_gap) if gap else 0.0
        if wait > 0.05:
            time.sleep(wait)
        print(raw, flush=True)
        if PAUSE_MARK in raw and not a.no_pause:
            try:
                input()
            except EOFError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
