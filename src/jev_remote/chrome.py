from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_PROFILE = Path("..") / "work" / "jev-chrome-profile"


def cdp_alive(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=1):
            return True
    except OSError:
        return False


def chrome_command(port: int, profile: Path) -> list[str]:
    flags = [f"--remote-debugging-port={port}", f"--user-data-dir={profile}"]
    if sys.platform == "darwin":
        return ["open", "-na", "Google Chrome", "--args", *flags]
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get(var, "")) / "Google/Chrome/Application/chrome.exe"
            for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        exe = next((str(c) for c in candidates if c.is_file()), "chrome.exe")
        return [exe, *flags]
    exe = shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"
    return [exe, *flags]


def ensure_chrome(timeout: float = 30.0) -> str:
    """Ensure the dedicated Chrome is listening, launching it if needed."""
    cdp_url = os.environ.get("BU_CDP_URL", "").strip() or DEFAULT_CDP_URL
    os.environ["BU_CDP_URL"] = cdp_url
    if cdp_alive(cdp_url):
        return f"Chrome already running at {cdp_url}"

    parsed = urlparse(cdp_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise RuntimeError(f"Cannot auto-launch Chrome for {cdp_url}; start it yourself.")

    profile = Path(os.environ.get("JEV_CHROME_PROFILE", "") or DEFAULT_PROFILE).expanduser()
    profile = profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        chrome_command(parsed.port, profile),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_alive(cdp_url):
            return f"Launched Chrome (profile {profile}) at {cdp_url}"
        time.sleep(0.5)
    raise RuntimeError(f"Chrome did not open {cdp_url} within {timeout:.0f}s")
