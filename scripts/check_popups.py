"""Live check: popup handling against real pages in the dedicated Chrome (port 9222).

    uv run python scripts/check_popups.py

Serves tests/fixtures/popups on localhost, drives StableTargetBrowser through each case and
prints PASS/FAIL. It uses no model or paid API. Start Chrome first (``uv run jev-remote`` does
this, or open it yourself with --remote-debugging-port=9222).
"""

from __future__ import annotations

import functools
import http.server
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("BU_CDP_URL", "http://127.0.0.1:9222")

from browser_harness.helpers import cdp  # noqa: E402

from jev_remote.player import PLAYER_LABEL  # noqa: E402
from jev_remote.safe_browser import StableTargetBrowser  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "popups"
MAIN_PORT, AD_PORT = 8765, 8766  # the ad runs on "localhost", the page on "127.0.0.1"
BASE = f"http://127.0.0.1:{MAIN_PORT}/"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def controls(page) -> list[str]:
    return [a["label"] for a in page["actions"] if a.get("kind") in {"click", "fill"}]


def click(browser, selector: str) -> None:
    x, y = browser.evaluate(
        f"(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();"
        "return [r.x+r.width/2,r.y+r.height/2]})()"
    )
    for event in ("mouseMoved", "mousePressed", "mouseReleased"):
        browser.call(
            "Input.dispatchMouseEvent",
            type=event,
            x=x,
            y=y,
            button="none" if event == "mouseMoved" else "left",
            clickCount=0 if event == "mouseMoved" else 1,
        )


def page_tabs() -> int:
    return sum(t["type"] == "page" for t in cdp("Target.getTargets")["targetInfos"])


def main() -> int:
    servers = [serve(MAIN_PORT), serve(AD_PORT)]
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    browser = StableTargetBrowser(BASE + "signin_modal.html")
    try:
        page = browser.observe(screenshot=False)
        check(
            "sign-in modal is dismissed, not signed into",
            browser.evaluate("window.__clicked") == ["dismiss"]
            and "Join now" not in controls(page),
            str(controls(page)),
        )

        browser.call("Page.navigate", url=BASE + "cookie_banner.html")
        time.sleep(0.6)
        browser.observe(screenshot=False)
        check(
            "cookie banner: Reject all, never Accept",
            browser.evaluate("window.__clicked") == ["reject"],
        )

        browser.call("Page.navigate", url=BASE + "datepicker.html")
        time.sleep(0.6)
        page = browser.observe(screenshot=False)
        check(
            "date picker the task needs is left open",
            browser.evaluate("window.__clicked") == [] and "Done" in controls(page),
        )

        browser.call("Page.navigate", url=BASE + "subscribe_only.html")
        time.sleep(0.6)
        browser.observe(screenshot=False)
        clicked = browser.evaluate("window.__clicked")
        check(
            "subscribe wall: Escape only, never Subscribe",
            "subscribe" not in (clicked or []) and "closesub" not in (clicked or []),
            str(clicked),
        )

        browser.call("Page.navigate", url=BASE + "stacked.html")
        time.sleep(0.6)
        browser.observe(screenshot=False)
        check(
            "stacked popups are all cleared",
            browser.evaluate("window.__clicked") == ["nothanks", "closenews"],
        )

        browser.call("Page.navigate", url=BASE + "plain.html")
        time.sleep(0.6)
        page = browser.observe(screenshot=False)
        check("plain page: nothing clicked", "Close the account" in controls(page))

        browser.call("Page.navigate", url=BASE + "sticky_header.html")
        time.sleep(0.6)
        page = browser.observe(screenshot=False)
        pressed = browser.evaluate("window.__clicked")
        check(
            "sticky 'Sign in' header and footer are not popups (no Escape, no click)",
            pressed == [] and "Sign in" in controls(page),
            str(pressed),
        )

        browser.call("Page.navigate", url=BASE + "player_page.html")
        time.sleep(1.2)
        page = browser.observe(screenshot=False)
        offered = [a for a in page["actions"] if a["label"] == PLAYER_LABEL]
        check("video player iframe is offered as a control", len(offered) == 1, str(controls(page)))
        if offered:
            browser.act(offered[0], page)  # the real safety path: freshness, hit-test, click
            time.sleep(0.8)
            clicked = browser.evaluate("window.__clicked")
            check(
                "clicking it presses play inside the player, not a Direct Link",
                clicked == ["player-played"],
                str(clicked),
            )

        browser.call("Page.navigate", url=BASE + "player_page2.html")
        time.sleep(1.2)
        browser._player_clicks = 0
        outcome = "never offered"
        for attempt in range(1, 5):
            page = browser.observe(screenshot=False)
            offered = [a for a in page["actions"] if a["label"] == PLAYER_LABEL]
            if not offered:
                outcome = (
                    f"playing after {attempt - 1} click(s)"
                    if "now playing" in page["text"]
                    else "stopped being offered"
                )
                break
            browser.act(offered[0], page)
            time.sleep(1.0)
        check(
            "two-step player (ad on first click) is clicked until it plays, then left alone",
            outcome == "playing after 2 click(s)",
            outcome,
        )

        browser.call("Page.navigate", url=BASE + "ad_popup.html")
        time.sleep(0.6)
        before = page_tabs()
        click(browser, "#play")
        time.sleep(1.2)
        opened = page_tabs() - before
        browser.observe(screenshot=False)
        time.sleep(0.3)
        browser.observe(screenshot=False)
        check("ad popup tab is closed", opened == 1 and page_tabs() == before, f"opened {opened}")

        browser.call("Page.navigate", url=BASE + "beforeunload.html")
        time.sleep(0.6)
        browser.call("Runtime.evaluate", expression="setTimeout(()=>alert('Special offer!'),10)")
        time.sleep(0.5)
        started = time.monotonic()
        page = browser.observe(screenshot=False)
        check(
            "native alert() is dismissed without freezing",
            "Alert" in controls(page) and time.monotonic() - started < 3,
        )
    finally:
        browser.close()
        for server in servers:
            server.shutdown()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
