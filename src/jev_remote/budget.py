from __future__ import annotations

import os
import threading
import time
from collections import deque


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


class RequestBudget:
    """Caps on paid API calls: per command, and per rolling hour for TypeSafe and OpenAI.

    A limit of 0 disables that cap. Calls are counted when a Jev step completes, so a step's
    internal retries are not counted.
    """

    WINDOW = 3600.0

    def __init__(
        self,
        max_steps_per_command: int | None = None,
        max_jev_per_hour: int | None = None,
        max_openai_per_hour: int | None = None,
    ):
        self.max_steps = (
            _int_env("JEV_MAX_STEPS_PER_COMMAND", 25)
            if max_steps_per_command is None
            else max_steps_per_command
        )
        self.max_jev = (
            _int_env("JEV_MAX_JEV_CALLS_PER_HOUR", 200)
            if max_jev_per_hour is None
            else max_jev_per_hour
        )
        self.max_openai = (
            _int_env("JEV_MAX_OPENAI_CALLS_PER_HOUR", 50)
            if max_openai_per_hour is None
            else max_openai_per_hour
        )
        self._lock = threading.Lock()
        self._jev: deque[float] = deque()
        self._openai: deque[float] = deque()

    def _prune(self, events: deque[float]) -> int:
        cutoff = time.monotonic() - self.WINDOW
        while events and events[0] < cutoff:
            events.popleft()
        return len(events)

    def record(self, jev_calls: int = 0, openai_calls: int = 0) -> None:
        now = time.monotonic()
        with self._lock:
            self._jev.extend([now] * jev_calls)
            self._openai.extend([now] * openai_calls)

    def exceeded(self, command_steps: int = 0) -> str | None:
        """Return a human-readable reason if another Jev step must not start."""
        with self._lock:
            jev, openai = self._prune(self._jev), self._prune(self._openai)
        if self.max_steps and command_steps >= self.max_steps:
            return f"Step limit reached ({self.max_steps} per command)"
        if self.max_jev and jev >= self.max_jev:
            return f"Hourly Jev (TypeSafe) limit reached ({self.max_jev} calls)"
        if self.max_openai and openai >= self.max_openai:
            return f"Hourly OpenAI limit reached ({self.max_openai} calls)"
        return None
