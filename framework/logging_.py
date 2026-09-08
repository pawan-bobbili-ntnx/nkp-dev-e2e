# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Small logger: readable on a terminal, and tee'd to a per-scenario file."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

CYAN, YELLOW, RED, DIM, RESET = "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[2m", "\033[0m"


class Log:
    def __init__(self, prefix: str = "", path: Path | None = None, verbose: bool = False):
        self.prefix = prefix
        self.path = path
        self.verbose = verbose
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def child(self, prefix: str, path: Path | None = None) -> "Log":
        return Log(prefix=prefix, path=path, verbose=self.verbose)

    def _write(self, line: str, colour: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        tag = f"[{self.prefix}] " if self.prefix else ""
        plain = f"{stamp} {tag}{line}"
        sys.stdout.write(f"{colour}{plain}{RESET}\n" if colour else f"{plain}\n")
        sys.stdout.flush()
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(plain + "\n")

    def banner(self, line: str, level: int = 1) -> None:
        self._write(("==> " if level == 1 else "--> ") + line, CYAN)

    def info(self, line: str) -> None:
        self._write(line)

    def warn(self, line: str) -> None:
        self._write("WARN: " + line, YELLOW)

    def error(self, line: str) -> None:
        self._write("ERROR: " + line, RED)

    def debug(self, line: str) -> None:
        if self.verbose:
            self._write(line, DIM)
        elif self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
