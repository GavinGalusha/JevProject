from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    start_url: str = "https://www.google.com/"
    host: str = "0.0.0.0"
    port: int = 8787
    command_timeout_seconds: float = 180.0
    stall_timeout_seconds: float = 45.0

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("JEV_REMOTE_TOKEN", "").strip()
        if len(token) < 32:
            raise RuntimeError(
                "JEV_REMOTE_TOKEN must be at least 32 characters. "
                "Copy .env.example to .env and generate a random token."
            )
        command_timeout = float(os.environ.get("JEV_COMMAND_TIMEOUT_SECONDS", 180))
        stall_timeout = float(os.environ.get("JEV_STALL_TIMEOUT_SECONDS", 45))
        if command_timeout <= 0 or stall_timeout <= 0:
            raise RuntimeError("JEV command and stall timeouts must be greater than zero")
        return cls(
            token=token,
            start_url=os.environ.get("JEV_START_URL", cls.start_url).strip(),
            host=os.environ.get("JEV_REMOTE_HOST", cls.host).strip(),
            port=int(os.environ.get("JEV_REMOTE_PORT", cls.port)),
            command_timeout_seconds=command_timeout,
            stall_timeout_seconds=stall_timeout,
        )
