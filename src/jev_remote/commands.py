from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MediaAction = Literal["pause", "play", "toggle", "mute", "unmute", "volume", "seek", "fullscreen"]


@dataclass(frozen=True)
class ParsedCommand:
    kind: Literal["media", "jev"]
    text: str
    action: MediaAction | None = None
    amount: float | None = None


def parse_command(raw: str) -> ParsedCommand:
    # Speech recognition adds punctuation ("Pause.", "Volume up!"), so ignore it when matching.
    text = " ".join(re.sub(r"[^\w\s]", " ", raw.lower()).split())
    if not text:
        raise ValueError("Command cannot be empty")

    exact = {
        "pause": "pause",
        "pause video": "pause",
        "play": "play",
        "resume": "play",
        "resume video": "play",
        "play pause": "toggle",
        "toggle playback": "toggle",
        "mute": "mute",
        "unmute": "unmute",
        "fullscreen": "fullscreen",
        "full screen": "fullscreen",
    }
    if action := exact.get(text):
        return ParsedCommand("media", text, action)  # type: ignore[arg-type]

    if re.fullmatch(r"(volume )?(up|louder)", text):
        return ParsedCommand("media", text, "volume", 0.1)
    if re.fullmatch(r"(volume )?(down|quieter|softer)", text):
        return ParsedCommand("media", text, "volume", -0.1)

    seek = re.fullmatch(
        r"(?:(?:skip|seek) )?(forward|ahead|back|backward)(?: (\d{1,4}))?(?: seconds?)?",
        text,
    )
    if seek:
        seconds = float(seek.group(2) or 30)
        if seek.group(1) in {"back", "backward"}:
            seconds *= -1
        return ParsedCommand("media", text, "seek", seconds)

    return ParsedCommand("jev", raw.strip())
