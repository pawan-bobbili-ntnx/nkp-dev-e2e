# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Subprocess helper with dry-run support and secret-safe logging."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .logging_ import Log

#: values of these variables are never echoed
SECRET_ENV = (
    "NUTANIX_PASSWORD",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "DOCKER_PASSWORD",
    "GHCR_TOKEN",
    # passed to the CLI as a flag, so it would otherwise be echoed in full
    "E2E_REGISTRY_MIRROR_PASSWORD",
    # read by dockerhub_auth.py to authenticate flux's chart pulls
    "DOCKERHUB_PASSWORD",
)


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], code: int, tail: str):
        super().__init__(f"`{' '.join(cmd[:4])} ...` exited {code}\n{tail}")
        self.cmd, self.code, self.tail = cmd, code, tail


@dataclass
class Result:
    code: int
    stdout: str

    @property
    def ok(self) -> bool:
        return self.code == 0


#: progress markers worth surfacing live from a long-running CLI call
_INTERESTING = ("✓", "✗", "Created", "Waiting", "Provision", "error", "Error", "failed")
#: some subprocesses are already curated progress feeds - claim.py prints ~66
#: timestamped lines over ~7 minutes, gates.py ~25 over ~3. Filtering those by
#: keyword hides the run; pass stream_all=True and show every line instead.


def _worth_showing(line: str) -> bool:
    return any(token in line for token in _INTERESTING)


def _redact(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, f"$({name})")
    return text


def run(
    cmd: list[str],
    log: Log,
    *,
    dry_run: bool = False,
    check: bool = True,
    timeout: int = 3600,
    env: dict[str, str] | None = None,
    log_file: Path | None = None,
    quiet: bool = False,
    stream_all: bool = False,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> Result:
    """Run a command, streaming to the scenario log. Returns stdout."""
    printable = _redact(" ".join(cmd))
    if dry_run:
        log.info(f"[dry-run] {printable}")
        return Result(0, "")

    if not quiet:
        log.info(f"$ {printable}")

    merged = {**os.environ, **(env or {})}
    started = time.monotonic()

    # Stream rather than capture-and-wait: cluster creation runs for tens of
    # minutes and silence for that long is indistinguishable from a hang.
    lines: list[str] = []
    with subprocess.Popen(  # noqa: S603 - inputs are constructed by us
        cmd,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=merged,
        cwd=str(cwd) if cwd else None,
    ) as proc:
        if stdin is not None:
            proc.stdin.write(stdin)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        sink = None
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            sink = log_file.open("a", encoding="utf-8")
            sink.write(f"\n$ {printable}\n")
        # `timeout` has to bound the READ, not just the wait after it. Iterating
        # proc.stdout blocks until the child closes it, so proc.wait(timeout=)
        # below is only ever reached once the child has effectively finished -
        # i.e. the timeout was unenforced for the entire life of the command.
        # Live-caught 2026-09-01: a gate given a 1200s budget ran 24m40s and
        # never tripped it. A watchdog that kills the child is what actually
        # bounds it; killing closes stdout, which ends the loop.
        timed_out = False

        def _expire():
            nonlocal timed_out
            timed_out = True
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass

        watchdog = threading.Timer(timeout, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                line = _redact(raw.rstrip("\n"))
                lines.append(line)
                if sink:
                    sink.write(line + "\n")
                    sink.flush()
                if not quiet and (stream_all or _worth_showing(line)):
                    log.info(f"  | {line[:160]}")
                else:
                    log.debug(f"  | {line}")
            code = proc.wait(timeout=max(5, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        finally:
            watchdog.cancel()
            if sink:
                sink.close()
        if timed_out:
            raise subprocess.TimeoutExpired(printable, timeout)

    out = "\n".join(lines)
    elapsed = time.monotonic() - started
    if not quiet:
        log.debug(f"  -> exit {code} in {elapsed:.0f}s")

    if check and code != 0:
        raise CommandError(cmd, code, _redact(out[-2000:]))
    return Result(code, out)


def wait_for(
    predicate,
    log: Log,
    *,
    what: str,
    timeout_s: int,
    interval_s: int = 15,
    dry_run: bool = False,
) -> None:
    """Poll until ``predicate()`` is truthy, or raise TimeoutError."""
    if dry_run:
        log.info(f"[dry-run] would wait for {what}")
        return
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            state = predicate()
        except Exception as exc:  # noqa: BLE001 - transient API errors are normal
            state, exc_text = False, f"{type(exc).__name__}: {exc}"
            if exc_text != last:
                log.debug(f"  waiting for {what}: {exc_text}")
                last = exc_text
        if state:
            log.info(f"  {what}: ready")
            return
        time.sleep(interval_s)
    raise TimeoutError(f"timed out after {timeout_s}s waiting for {what}")
