from __future__ import annotations

import sys
import threading
import time

_LOCK = threading.Lock()
_COLORS = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}


def say(message: str, color: str | None = None, *, rule: bool = False) -> None:
    """Print one timestamped line to the terminal immediately (safe from any thread)."""
    stamp = time.strftime("%H:%M:%S")
    line = f"{stamp}  {message}"
    if color and sys.stdout.isatty():
        line = f"\033[{_COLORS[color]}m{line}\033[0m"
    with _LOCK:
        if rule:
            print("─" * 64)
        print(line, flush=True)


class Heartbeat:
    """Prints a 'still working' line every few seconds so a quiet terminal never looks hung."""

    def __init__(self, describe, interval: float = 8.0):
        self._describe = describe
        self._interval = interval
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jev-heartbeat", daemon=True)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._done.set()

    def _run(self) -> None:
        while not self._done.wait(self._interval):
            say(f"…  still working — {self._describe()}", "dim")
