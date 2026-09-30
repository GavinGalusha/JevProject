from __future__ import annotations

import hmac
import secrets
import socket
import threading
import time

# No 0/O/1/I/L so a code can be read off a screen and typed without mistakes.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def lan_ip() -> str:
    """Best-effort LAN address of this computer. Sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def normalize(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class Pairing:
    """A single-use, short-lived code that a phone trades for the remote token."""

    def __init__(self, ttl_seconds: float = 900, max_failures: int = 5):
        self.ttl = ttl_seconds
        self.max_failures = max_failures
        self._lock = threading.Lock()
        self._code = ""
        self._expires = 0.0
        self._failures = 0
        self.new_code()

    def new_code(self) -> str:
        with self._lock:
            self._code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            self._expires = time.monotonic() + self.ttl
            self._failures = 0
            return self._code

    @property
    def display_code(self) -> str:
        return f"{self._code[:4]}-{self._code[4:]}"

    def consume(self, supplied: str) -> bool:
        with self._lock:
            if not self._code or time.monotonic() > self._expires:
                return False
            if hmac.compare_digest(normalize(supplied), self._code):
                self._code = ""  # single use
                return True
            self._failures += 1
            if self._failures >= self.max_failures:
                self._code = ""
            return False
