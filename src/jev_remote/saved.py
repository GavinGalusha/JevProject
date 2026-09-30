from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_SAVED = 50


class SavedCommands:
    """Named, reusable Jev commands kept in a small JSON file on the PC."""

    def __init__(self, path: Path | None = None):
        default = os.environ.get("JEV_SAVED_COMMANDS_FILE", "").strip() or "saved_commands.json"
        self.path = Path(path or default).expanduser()
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(items, handle, indent=2)
        os.replace(tmp, self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read()

    def get(self, saved_id: str) -> dict[str, Any] | None:
        return next((i for i in self.list() if i["id"] == saved_id), None)

    def add(
        self, name: str, text: str, start_url: str | None = None, fullscreen: bool = False
    ) -> dict[str, Any]:
        name, text = name.strip(), text.strip()
        if not name or not text:
            raise ValueError("A saved command needs a name and command text")
        start_url = (start_url or "").strip() or None
        if start_url and urlparse(start_url).scheme not in {"http", "https"}:
            raise ValueError("Start URL must begin with http:// or https://")
        with self._lock:
            items = self._read()
            if len(items) >= MAX_SAVED:
                raise ValueError(f"You can save up to {MAX_SAVED} commands")
            item = {
                "id": secrets.token_hex(4),
                "name": name[:60],
                "text": text[:2000],
                "start_url": start_url,
                "fullscreen": bool(fullscreen),
            }
            self._write([*items, item])
            return item

    def delete(self, saved_id: str) -> bool:
        with self._lock:
            items = self._read()
            kept = [i for i in items if i.get("id") != saved_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
            return True

