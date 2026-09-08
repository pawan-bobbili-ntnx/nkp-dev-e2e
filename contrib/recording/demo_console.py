#!/usr/bin/env python3
"""A terminal that renders framework-style logs from a control file.

You run this in the terminal you are recording. It prints nothing until a line
arrives in the control file, then renders it exactly the way `run_e2e.py`
renders its own output - same timestamp, same scenario tag, same colours - and
waits for the next one. Whoever is driving the demo appends to the control
file, so the pace of the screen is a decision, not a race with a cluster.

  ./demo_console.py                       # renders ./.demo-console
  ./demo_console.py --scenario demo-live  # change the [tag]
  ./demo_console.py --reset               # start from an empty control file

The cluster it is talking about is REAL and is running alongside this - see
demo_drive.py. This renders the narration; the dashboard shown at each pause is
the genuine article.

Control lines (append one per line; anything else is an ordinary log line):

  @banner <text>     ==> cyan section banner
  @step <text>       --> cyan sub-banner
  @warn <text>       WARN: in yellow
  @error <text>      ERROR: in red
  @dim <text>        dim, no timestamp - for commentary
  @raw <text>        printed verbatim, no timestamp
  @cmd <text>        rendered as "$ <text>"
  @out <text>        rendered as "  | <text>" - a command's own output
  @pause <text>      the framework's AUTOMATION STOPPED banner, waits for Enter
  @sleep <seconds>   hold before the next line
  @clear             clear the screen
  @exit              stop the console
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

CYAN, YELLOW, RED, DIM, RESET = (
    "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[2m", "\033[0m")

HERE = Path(__file__).resolve().parent
DEFAULT_CONTROL = HERE / ".demo-console"
# a line appears this long after the one before it unless @sleep says otherwise,
# so a burst written in one go still reads like something happening.
LINE_PACE = 0.28


class Console:
    def __init__(self, scenario: str, pace: float, resume: Path | None = None):
        self.scenario = scenario
        self.pace = pace
        # Whoever is driving is a separate process and cannot see this Enter.
        # Bumping a counter here is how a released pause reaches them; without
        # it the driver runs on while the screen is still waiting.
        self.resume = resume
        self.resumed = 0

    def release(self) -> None:
        if self.resume is None:
            return
        self.resumed += 1
        self.resume.write_text(str(self.resumed))

    def stamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def write(self, line: str, colour: str = "") -> None:
        text = f"{self.stamp()} [{self.scenario}] {line}"
        sys.stdout.write(f"{colour}{text}{RESET}\n" if colour else f"{text}\n")
        sys.stdout.flush()

    def plain(self, line: str, colour: str = "") -> None:
        sys.stdout.write(f"{colour}{line}{RESET}\n" if colour else f"{line}\n")
        sys.stdout.flush()

    def handle(self, raw: str) -> bool:
        """Render one control line. False means stop."""
        directive, _, rest = raw.partition(" ")
        if not raw.startswith("@"):
            self.write(raw)
            return True
        if directive == "@banner":
            self.write("==> " + rest, CYAN)
        elif directive == "@step":
            self.write("--> " + rest, CYAN)
        elif directive == "@warn":
            self.write("WARN: " + rest, YELLOW)
        elif directive == "@error":
            self.write("ERROR: " + rest, RED)
        elif directive == "@dim":
            self.plain(rest, DIM)
        elif directive == "@raw":
            self.plain(rest)
        elif directive == "@cmd":
            self.write("$ " + rest)
        elif directive == "@out":
            self.write("  | " + rest)
        elif directive == "@sleep":
            try:
                time.sleep(float(rest))
            except ValueError:
                pass
        elif directive == "@clear":
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        elif directive == "@pause":
            self.write("--> AUTOMATION STOPPED" + (f" - {rest}" if rest else ""), CYAN)
            self.write("  the cluster is all yours; nothing will touch it until you resume")
            try:
                input()
            except EOFError:
                self.release()
                return False
            self.release()
        elif directive == "@exit":
            return False
        else:                       # an unknown @word is just text
            self.write(raw)
        return True


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--control", default=str(DEFAULT_CONTROL),
                   help=f"control file to follow (default: {DEFAULT_CONTROL.name})")
    p.add_argument("--scenario", default="demo-full-loop",
                   help="the [tag] each line carries (default: demo-full-loop)")
    p.add_argument("--pace", type=float, default=LINE_PACE,
                   help=f"seconds between lines (default: {LINE_PACE})")
    p.add_argument("--reset", action="store_true",
                   help="truncate the control file before starting")
    p.add_argument("--resume-file", default="",
                   help="counter file bumped when a pause is released "
                        "(default: <control>.resume)")
    a = p.parse_args()

    control = Path(a.control)
    if a.reset or not control.exists():
        control.write_text("")

    resume = Path(a.resume_file) if a.resume_file else control.with_suffix(".resume")
    resume.write_text("0")
    console = Console(a.scenario, a.pace, resume)
    # Follow the file rather than reading it: the driver appends while this
    # runs, and a demo that had to be fully written before it could be shown
    # would not be a live console.
    with control.open("r", encoding="utf-8") as fh:
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.12)
                    continue
                line = line.rstrip("\n")
                if not line:
                    console.plain("")
                    continue
                if not console.handle(line):
                    break
                time.sleep(console.pace)
        except KeyboardInterrupt:
            console.plain("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
