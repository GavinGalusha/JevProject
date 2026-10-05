"""Do not accept a shaky DONE: make the model look again before it ends a command.

Every correct finish seen so far came with high confidence (0.89+). A DONE at 0.4 is usually the
model giving up one step early, for example a filled-in search form that was never submitted. A
low-confidence DONE therefore gets one nudge and one more decision; whatever the model says then
stands, so this can cost at most one extra model call and can never loop.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

NOTE_VERIFY_DONE = (
    "[Jev] You answered DONE with low confidence. DONE needs clear, visible evidence for every "
    "part of the goal. If a Search or Submit control is still available, or the goal asks for "
    "results or options to be visible and none are shown yet, take that step now."
)


_VERIFIED = re.compile(r"\[verified: Season \d+ Episode \d+\]")


def auto_done_enabled() -> bool:
    return os.environ.get("JEV_AUTO_DONE_ON_PLAYING", "").strip().lower() in {"1", "true", "yes"}


def verified_done() -> dict[str, Any]:
    """A DONE decided by Jev itself because the requested episode is proven to be playing."""
    return {
        "choice": "DONE",
        "operation": "DONE",
        "target": None,
        "confidence": 1.0,
        "probabilities": {"DONE": 1.0},
        "operation_probabilities": {"DONE": 1.0},
        "target_probabilities": {},
        "target_confidence": None,
        "raw_answers": {},
        "model": "verified-playing",
        "usage": {},
        "latency_ms": 0,
        "request": {},
    }


def min_done_confidence() -> float:
    try:
        return float(os.environ.get("JEV_MIN_DONE_CONFIDENCE", "").strip() or 0.6)
    except ValueError:
        return 0.6


def _merge_usage(first: dict[str, Any] | None, second: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(first or {})
    for key, value in (second or {}).items():
        if isinstance(value, int | float) and isinstance(merged.get(key), int | float):
            merged[key] = merged[key] + value
        else:
            merged.setdefault(key, value)
    return merged


def guard_choose(choose: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap the agent's decision function with the low-confidence-DONE check."""

    def guarded(state: dict[str, Any], goal: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        if auto_done_enabled() and _VERIFIED.search(state.get("text", "") or ""):
            return verified_done()  # the requested episode is proven playing: nothing left to ask
        result = choose(state, goal, history)
        threshold = min_done_confidence()
        if (
            threshold <= 0
            or result.get("operation") != "DONE"
            or (result.get("confidence") or 0) >= threshold
        ):
            return result
        nudged = {**state, "text": f"{state.get('text', '')}\n{NOTE_VERIFY_DONE}".strip()}
        second = choose(nudged, goal, history)
        second["usage"] = _merge_usage(result.get("usage"), second.get("usage"))
        second["done_rechecked"] = True
        return second

    guarded._jev_guarded = True  # type: ignore[attr-defined]
    return guarded
