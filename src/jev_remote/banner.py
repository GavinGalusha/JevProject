"""An on-screen "Task completed" / "Task failed" card, drawn inside the controlled browser.

It lives in a shadow root (so a site's CSS cannot restyle it), in the browser's top layer (so it
sits above site popups), ignores the mouse, and removes itself after a few seconds so it never
covers the video.
"""

from __future__ import annotations

import json
import os


def banner_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("JEV_BANNER_SECONDS", "").strip() or 5))
    except ValueError:
        return 5.0


def banner_enabled() -> bool:
    return os.environ.get("JEV_BANNER", "1").strip().lower() not in {"0", "false", "no", "off"}


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def banner_js(kind: str, title: str, detail: str = "", seconds: float = 5.0) -> str:
    """JavaScript that shows the card. ``kind`` is "success" or "failure"."""
    ok = kind == "success"
    payload = json.dumps(
        {
            "ok": ok,
            "title": _clip(title, 60),
            "detail": _clip(detail, 140),
            "ms": int(max(0.2, seconds) * 1000),
        }
    )
    return (
        "((cfg) => {"
        " document.getElementById('jev-banner')?.remove();"
        " const host = document.createElement('div'); host.id = 'jev-banner';"
        " host.setAttribute('popover', 'manual');"
        " host.style.cssText = 'all:initial;position:fixed;inset:0;pointer-events:none;"
        "background:transparent;border:0;padding:0;margin:0;overflow:visible;z-index:2147483647';"
        " const root = host.attachShadow({mode: 'closed'});"
        " const color = cfg.ok ? '#16a05a' : '#d93a45';"
        " const style = document.createElement('style');"
        " style.textContent = `"
        ".card{position:fixed;top:5vh;left:50%;transform:translateX(-50%);display:flex;"
        "align-items:center;gap:2.2vmin;max-width:86vw;padding:2.4vmin 4vmin;border-radius:2.4vmin;"
        "color:#fff;font:600 3.4vmin/1.25 system-ui,-apple-system,Segoe UI,sans-serif;"
        "box-shadow:0 1.2vmin 5vmin rgba(0,0,0,.55);background:${color};"
        "animation:in .25s ease-out both}"
        ".icon{font-size:6vmin;line-height:1}"
        ".title{font-size:4.4vmin;font-weight:800;letter-spacing:.02em}"
        ".detail{font-size:2.6vmin;font-weight:500;opacity:.92;margin-top:.5vmin}"
        "@keyframes in{from{opacity:0;transform:translate(-50%,-2vmin)}to{opacity:1}}"
        "@keyframes out{to{opacity:0;transform:translate(-50%,-2vmin)}}`;"
        " const card = document.createElement('div'); card.className = 'card';"
        " const icon = document.createElement('div'); icon.className = 'icon';"
        " icon.textContent = cfg.ok ? '✓' : '✕';"
        " const box = document.createElement('div');"
        " const title = document.createElement('div'); title.className = 'title';"
        " title.textContent = cfg.title; box.append(title);"
        " if (cfg.detail) { const d = document.createElement('div'); d.className = 'detail';"
        " d.textContent = cfg.detail; box.append(d); }"
        " card.append(icon, box); root.append(style, card);"
        " (document.documentElement).append(host);"
        " try { host.showPopover(); } catch (e) { /* older browsers: z-index is enough */ }"
        " setTimeout(() => { card.style.animation = 'out .35s ease-in both'; }, cfg.ms - 350);"
        " setTimeout(() => host.remove(), cfg.ms);"
        " return true; })(" + payload + ")"
    )
