from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    start_url: str = "https://www.google.com/"
    host: str = "0.0.0.0"
    port: int = 8787

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("JEV_REMOTE_TOKEN", "").strip()
        if len(token) < 32:
            raise RuntimeError(
                "JEV_REMOTE_TOKEN must be at least 32 characters. "
                "Copy .env.example to .env and generate a random token."
            )
        return cls(
            token=token,
            start_url=os.environ.get("JEV_START_URL", cls.start_url).strip(),
            host=os.environ.get("JEV_REMOTE_HOST", cls.host).strip(),
            port=int(os.environ.get("JEV_REMOTE_PORT", cls.port)),
        )
