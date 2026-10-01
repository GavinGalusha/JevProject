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


def kiosk_enabled() -> bool:
    return os.environ.get("JEV_KIOSK", "1").strip().lower() not in {"0", "false", "no", "off"}


def chrome_flags(port: int, profile: Path) -> list[str]:
    """Launch flags for the dedicated Chrome: debuggable, quiet, and (by default) UI-less."""
    flags = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ]
    if kiosk_enabled():
        # No tab strip, address bar or window frame: nothing of Chrome shows on the TV.
        flags.append("--kiosk")
    # Open on a blank page instead of Chrome's new-tab page (and its search box).
    return [*flags, "about:blank"]


def chrome_command(port: int, profile: Path) -> list[str]:
    flags = chrome_flags(port, profile)
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
