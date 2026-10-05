"""Skip the language-model round trip when a search box can be filled straight from the goal.

Typing "Everybody Hates Chris season 2 episode 4" into a search box costs about 1.4 s for a call
that mostly copies words out of the user's request. In speed mode (JEV_FAST_TEXT=1) a plain
search field is filled by cutting the leading verbs and trailing actions off the goal; any other
field, or a goal that does not reduce to a clear query, still goes to the model.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_SEARCH_FIELD = re.compile(r"\b(search|find|query|look ?up)\b|^q$", re.IGNORECASE)
_TRAILING_ACTION = re.compile(
    r"\s+(?:and|then)\s+(?:play|watch|open|click|start|stop|select|go|pick|show)\b.*$",
    re.IGNORECASE,
)
_LEADING = re.compile(
    r"^(?:please\s+)?(?:search|google|look\s*up|look\s*for|find|play|watch|open|show\s*me|"
    r"put\s*on)\s+(?:(?:google|youtube|tubi|the\s+web|online)\s+)?(?:for\s+)?",
    re.IGNORECASE,
)


_BARE_VERB = re.compile(
    r"(?:search|google|look\s*up|look\s*for|find|play|watch|open|show(?:\s*me)?|put\s*on)",
    re.IGNORECASE,
)


def search_query(goal: str) -> str | None:
    """A search query taken from the user's own words, or None if it is not clear."""
    first = (goal or "").strip().split("\n\n", 1)[0].strip()
    if not first or first.lower().startswith("original user request"):
        return None  # a guided-mode rewrite: let the model read it
    query = _TRAILING_ACTION.sub("", first)
    query = _LEADING.sub("", query, count=1).strip(" \t.!?,;:")
    if not (2 <= len(query) <= 120) or "\n" in query:
        return None
    if _BARE_VERB.fullmatch(query):
        return None  # "play" on its own is a command, not something to search for
    return query


def wrap_field_text(real: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]):
    """Wrap the agent's text helper: fast path for search boxes, the model for everything else."""

    def fast(context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        field = context.get("field") or {}
        label = str(field.get("label") or "")
        if _SEARCH_FIELD.search(label) or field.get("role") == "searchbox":
            query = search_query(str(context.get("goal") or ""))
            if query:
                return query, {"model": "from-goal", "latency_ms": 0, "usage": {}}
        return real(context)

    fast._jev_fast = True  # type: ignore[attr-defined]
    return fast
