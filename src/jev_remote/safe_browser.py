from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlparse

from jev_ultrafast.browser import Browser, StalePage, fingerprint

from .console import say
from .player import (
    MAX_PLAYER_CLICKS,
    NOTE_GAVE_UP,
    NOTE_NOT_STARTED,
    NOTE_PLAYING,
    PLAYER_LABEL,
    PLAYER_PROBE,
    VIDEO_STATE_JS,
    inline_video_js,
    videos_playing,
)
from .popups import OVERLAY_PROBE, popup_targets_to_close

_GUARD_FIELDS = (
    "node identity",
    "role",
    "name",
    "value",
    "checked state",
    "selected index",
    "read-only state",
    "disabled state",
    "ARIA disabled state",
    "expanded state",
    "ARIA checked state",
    "selected state",
    "destination",
)

_GOOGLE_CHROME_CONTROLS = {
    "google apps",
}


def _is_google_page(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    hostname = (urlparse(url).hostname or "").casefold()
    return hostname == "google.com" or hostname.endswith(".google.com")


_POPUP_PASSES = 3  # an overlay can reveal another one (consent, then newsletter)


def _milliseconds(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _action_signature(page: dict[str, Any]) -> tuple[Any, ...]:
    page_key = page.get("page_key") or []
    navigation = tuple(page_key[:2]) if isinstance(page_key, list | tuple) else ()
    actions = tuple(
        (
            action.get("node"),
            action.get("kind"),
            action.get("role"),
            action.get("label"),
            action.get("value"),
            action.get("checked"),
            action.get("selected"),
            action.get("expanded"),
        )
        for action in page.get("actions", [])
    )
    return navigation, actions


def _target_signature(guard: Any) -> list[Any] | None:
    """Exclude volatile container text while preserving the target's own semantics."""
    if not isinstance(guard, list) or len(guard) < 14:
        return None
    # identity, role, name, value, state, disabled/read-only state, ARIA state, and href.
    # guard[13] is up to 6,000 characters of surrounding container text and can change for
    # unrelated reasons on live result pages.
    return guard[:13]


class StableTargetBrowser(Browser):
    """Reject stale targets without treating unrelated dynamic-page churn as navigation.

    The upstream click guard includes every form value, scroll position, and viewport dimension in
    its page-wide key. Highly dynamic pages can mutate those values while an otherwise unchanged
    target remains safe. We instead bind actionable nodes to the document/navigation identity and
    the target's own semantic guard. Browser.act still resolves live geometry and rejects covered,
    disconnected, hidden, disabled, or otherwise invalid targets immediately before input.
    """

    # Set per run by the controller: playback goals wait for the player to appear.
    expect_player: bool = False
    _player_waited_url: str | None = None
    _player_clicks: int = 0
    _frame_sessions: dict[str, str] | None = None

    def __init__(self, url: str):
        super().__init__(url)
        try:
            # Without Page events the daemon never learns that an alert()/confirm() is open.
            self.call("Page.enable")
        except Exception as exc:
            say(f"   ⚠ could not watch for browser dialogs: {exc}", "dim")

    @staticmethod
    def _filter_actions(page: dict[str, Any], *, announce: bool = True) -> dict[str, Any]:
        actions = page.get("actions", [])
        fill_nodes = {
            action.get("node") for action in actions if action.get("kind") == "fill"
        }
        filtered: list[str] = []
        kept: list[dict[str, Any]] = []
        for action in actions:
            label = str(action.get("label") or "").strip()
            editable_duplicate = action.get("kind") == "click" and action.get("node") in fill_nodes
            google_chrome = (
                _is_google_page(page.get("url"))
                and action.get("kind") == "click"
                and label.casefold() in _GOOGLE_CHROME_CONTROLS
            )
            if editable_duplicate or google_chrome:
                filtered.append(label)
            else:
                kept.append(action)
        if filtered:
            # Upstream exposes both FILL "Search" and CLICK "Open Search" for the same editable
            # node. Google also exposes global launchers that are irrelevant to ordinary result
            # tasks. Neither is a distinct task-level outcome worth offering to the decision model.
            page["actions"] = kept
            page["fingerprint"] = fingerprint(page)
            if announce:
                say(
                    f"   ⊘ filtered non-task controls: {', '.join(dict.fromkeys(filtered))}",
                    "dim",
                )
        return page

    def _observe_through_navigation(self, screenshot: bool) -> dict[str, Any]:
        """Observe, waiting out a page that is mid-navigation instead of failing the run.

        Upstream retries for only ~200ms and then raises StalePage("Document is navigating").
        That error escapes Jev's own recovery path and ends the whole command, even though a
        click that triggers a real navigation (a result link, a play button) is the normal case.
        """
        timeout_ms = _milliseconds("JEV_NAVIGATION_WAIT_MS", 10000)
        deadline = time.monotonic() + timeout_ms / 1000
        announced = False
        while True:
            self._dismiss_native_dialog()
            try:
                return super().observe(screenshot=screenshot)
            except (StalePage, TimeoutError):
                # A TimeoutError here usually means an alert()/confirm() froze the page.
                if time.monotonic() >= deadline:
                    raise
                if not announced:
                    say("   ◷ page is navigating; waiting for it to load", "dim")
                    announced = True
                time.sleep(0.25)

    def observe(self, screenshot: bool = True):
        page = self._observe_page(screenshot)
        for _ in range(_POPUP_PASSES):
            if not self._clear_interruptions():
                break
            page = self._observe_page(screenshot)
        return self._with_video_player(page)

    def _read_videos(self, found: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The real <video> state behind the player, or None if it cannot be read.

        Same-origin frames are read from the page. A cross-origin player is its own CDP target;
        attach to it and ask it directly, which is far better evidence than looking at pixels.
        """
        inline = self.evaluate(inline_video_js(found["node"]))
        if isinstance(inline, list):
            return inline
        from browser_harness.helpers import cdp

        host = urlparse(found.get("src") or "").hostname
        if not host:
            return None
        if self._frame_sessions is None:
            self._frame_sessions = {}
        videos: list[dict[str, Any]] = []
        seen_frame = False
        for target in cdp("Target.getTargets").get("targetInfos", []):
            if target.get("type") != "iframe" or urlparse(target.get("url", "")).hostname != host:
                continue
            seen_frame = True
            session = self._frame_sessions.get(target["targetId"])
            if session is None:
                session = cdp("Target.attachToTarget", targetId=target["targetId"], flatten=True)[
                    "sessionId"
                ]
                self._frame_sessions[target["targetId"]] = session
            result = cdp(
                "Runtime.evaluate",
                session_id=session,
                expression=VIDEO_STATE_JS,
                returnByValue=True,
            )
            videos.extend(result.get("result", {}).get("value") or [])
        return videos if seen_frame else None

    def _player_is_playing(self, found: dict[str, Any]) -> bool:
        """Is something actually playing? Video clock first; pixels only if it cannot be read."""
        try:
            first = self._read_videos(found)
            if first is not None:
                if not first:
                    return False  # the player frame exists but has no <video> yet
                time.sleep(0.6)
                second = self._read_videos(found) or []
                return videos_playing(first, second)
        except Exception as exc:
            say(f"   ⚠ could not read the video state ({exc}); comparing frames instead", "dim")
        return self._frames_change(found.get("rect") or {})

    def _leave_fullscreen(self) -> None:
        """A click that lands on a fullscreen control must not strand the run in fullscreen."""
        try:
            self.call(
                "Runtime.evaluate",
                expression="document.fullscreenElement ? document.exitFullscreen() : null",
                awaitPromise=True,
                userGesture=True,
            )
            time.sleep(0.5)
            say("   ✕ left fullscreen (the click had not started playback)", "yellow")
        except Exception:
            pass

    def _frames_change(self, rect: dict[str, Any]) -> bool:
        """True if the player's pixels change over ~1s, i.e. something is playing.

        A cross-origin iframe's video state cannot be read from the page, so look at the pixels.
        A paused player is a still image and yields identical frames.
        """
        try:
            clip = {
                "x": max(0.0, float(rect.get("x", 0))),
                "y": max(0.0, float(rect.get("y", 0))),
                "width": max(1.0, float(rect.get("w", 1))),
                "height": max(1.0, float(rect.get("h", 1))),
                "scale": 0.5,
            }
            frames = set()
            for index in range(3):
                shot = self.call("Page.captureScreenshot", format="jpeg", quality=40, clip=clip)
                frames.add(shot.get("data"))
                if index < 2:
                    time.sleep(0.5)
            return len(frames) > 1
        except Exception:
            return False

    def _probe_player(self) -> dict[str, Any] | None:
        try:
            found = self.evaluate(PLAYER_PROBE)
        except Exception:
            return None  # mid-navigation or similar; the caller may retry
        return found if isinstance(found, dict) else None

    def _with_video_player(self, page: dict[str, Any]) -> dict[str, Any]:
        """Offer the embedded player as a control; the upstream reader skips iframes."""
        if os.environ.get("JEV_PLAYER_ACTION", "1").strip().lower() in {"0", "false", "no"}:
            return page
        if not getattr(self, "session", None):
            return page
        found = self._probe_player()
        url = page.get("url")
        if not found and self.expect_player and url != self._player_waited_url:
            # Players are usually injected a moment after load; wait once per page for it.
            self._player_waited_url = url
            deadline = time.monotonic() + _milliseconds("JEV_PLAYER_WAIT_MS", 2500) / 1000
            while not found and time.monotonic() < deadline:
                time.sleep(0.25)
                found = self._probe_player()
        if not isinstance(found, dict) or type(found.get("node")) is not int:
            return page
        actions = page.get("actions", [])
        if any(action.get("node") == found["node"] for action in actions):
            return page

        note = None
        offer = True
        if self._player_clicks or found.get("playing"):
            if found.get("playing") or self._player_is_playing(found):
                note, offer = NOTE_PLAYING, False  # never offer a playing player: a click pauses it
            elif self._player_clicks >= MAX_PLAYER_CLICKS:
                note, offer = NOTE_GAVE_UP.format(clicks=self._player_clicks), False
            else:
                note = NOTE_NOT_STARTED.format(clicks=self._player_clicks)
                if found.get("fullscreen"):
                    self._leave_fullscreen()
        if note:
            page["text"] = f"{page.get('text', '')}\n{note}".strip()
            say(f"   ▶ {note}", "dim")
        if offer:
            player = {
                "id": "e_player",
                "node": found["node"],
                "kind": "click",
                "role": "button",
                "label": PLAYER_LABEL,
                "value": "",
                "rect": found.get("rect") or {},
            }
            # Keep the real controls first; scroll/wait pseudo-actions stay at the end.
            at = next(
                (i for i, a in enumerate(actions) if a.get("kind") in {"scroll", "wait"}),
                len(actions),
            )
            page["actions"] = [*actions[:at], player, *actions[at:]]
            page.setdefault("guards", {})[str(found["node"])] = found.get("guard")
        page["fingerprint"] = fingerprint(page)
        if not offer:
            return page
        say(f"   ▶ offering the page's video player ({found.get('tag')}) as a control", "dim")
        return page

    def _observe_page(self, screenshot: bool = True):
        after_action = bool(getattr(self, "after_input", None))
        # Avoid repeated screenshot captures while checking readiness. The normal Jev loop has
        # screenshots disabled; when requested, capture one final image after settling.
        page = self._filter_actions(
            self._observe_through_navigation(screenshot and not after_action)
        )
        timeout_ms = _milliseconds("JEV_PAGE_SETTLE_TIMEOUT_MS", 1500)
        quiet_ms = _milliseconds("JEV_PAGE_SETTLE_QUIET_MS", 150)
        if not after_action or not timeout_ms or not quiet_ms:
            return page

        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        previous = _action_signature(page)
        stable_samples = 0
        while time.monotonic() < deadline:
            time.sleep(min(quiet_ms / 1000, max(0, deadline - time.monotonic())))
            candidate = self._filter_actions(
                self._observe_through_navigation(False), announce=False
            )
            signature = _action_signature(candidate)
            stable_samples = stable_samples + 1 if signature == previous else 0
            page, previous = candidate, signature
            if stable_samples >= 2:
                break

        waited_ms = round((time.monotonic() - started) * 1000)
        if stable_samples >= 2:
            say(f"   ◷ page settled after {waited_ms}ms (two stable semantic samples)", "dim")
        else:
            say(f"   ◷ page settle cap reached after {waited_ms}ms; using latest state", "yellow")
        if screenshot:
            page = self._filter_actions(self._observe_through_navigation(True))
        return page

    def act(self, action: dict[str, Any], page: dict[str, Any], text: str | None = None):
        if action.get("kind") != "click":
            return super().act(action, page, text=text)
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        node = action.get("node")
        if type(node) is not int:
            raise ValueError("Invalid observed node")

        try:
            target = self.evaluate(
                """(action => {
                  const e=window.__jevFast?.nodes.get(action.node);
                  if (!e?.isConnected) return {ok:false,reason:'disconnected'};
                  if (e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]'))
                    return {ok:false,reason:'disabled'};
                  if (!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
                    return {ok:false,reason:'hidden'};
                  const r=e.getBoundingClientRect();
                  if (!r.width || !r.height) return {ok:false,reason:'empty geometry'};
                  const points=[[.5,.5],[.25,.5],[.75,.5],[.5,.25],[.5,.75],
                    [.2,.2],[.8,.2],[.2,.8],[.8,.8]];
                  for (const [px,py] of points) {
                    const x=r.x+r.width*px,y=r.y+r.height*py;
                    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
                    const hit=document.elementFromPoint(x,y);
                    if (hit && e.contains(hit)) return {ok:true,x,y,position:[px,py]};
                  }
                  const center=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                  return {ok:false,reason:'covered',cover:{tag:center?.tagName||null,
                    role:center?.getAttribute?.('role')||null,
                    label:(center?.getAttribute?.('aria-label')||'').slice(0,120)}};
                })"""
                + "("
                + json.dumps(action)
                + ")"
            )
        except StalePage as exc:
            say(f"   ⛨ safety rejected click during geometry check: {exc}", "yellow")
            raise
        if not isinstance(target, dict) or not target.get("ok"):
            detail = target if isinstance(target, dict) else {"reason": "no target state"}
            say(f"   ⛨ safety rejected click geometry: {detail}", "yellow")
            raise StalePage("Target changed or is covered. Observe again.")

        x, y = target["x"], target["y"]
        confirmed = self.evaluate(
            """(({node,x,y}) => {
              const e=window.__jevFast?.nodes.get(node),hit=document.elementFromPoint(x,y);
              return !!(e?.isConnected && hit && e.contains(hit));
            })"""
            + "("
            + json.dumps({"node": node, "x": x, "y": y})
            + ")"
        )
        if confirmed is not True:
            say("   ⛨ safety rejected click: hit target changed before input", "yellow")
            raise StalePage("Target changed immediately before input. Observe again.")
        if target.get("position") != [0.5, 0.5]:
            say(f"   ◎ center was unavailable; using safe point {target['position']}", "dim")
        for event in ("mousePressed", "mouseReleased"):
            self.call(
                "Input.dispatchMouseEvent",
                type=event,
                x=x,
                y=y,
                button="left",
                clickCount=1,
            )
        self.after_input = action
        if action.get("id") == "e_player":
            self._player_clicks += 1
        return {"executed": action["id"]}

    # ----- Interruptions: popup tabs, in-page overlays and native dialogs -----

    def _clear_interruptions(self) -> bool:
        """Close what is in the way. Best effort: a failure here must never end the run."""
        if os.environ.get("JEV_DISMISS_POPUPS", "1").strip().lower() in {"0", "false", "no"}:
            return False
        if not getattr(self, "session", None):
            return False
        try:
            changed = self._close_popup_tabs()
            changed = self._dismiss_overlay() or changed
            return changed
        except Exception as exc:
            say(f"   ⚠ popup check skipped: {exc}", "dim")
            return False

    def _close_popup_tabs(self) -> bool:
        from browser_harness.helpers import cdp

        targets = cdp("Target.getTargets").get("targetInfos", [])
        own = next((t for t in targets if t.get("targetId") == self.target), {})
        popups = popup_targets_to_close(targets, self.target, own.get("url"))
        for popup in popups:
            cdp("Target.closeTarget", targetId=popup["targetId"])
            say(f"   ✕ closed popup tab: {str(popup.get('url'))[:80]}", "yellow")
        if popups:
            cdp("Target.activateTarget", targetId=self.target)
        return bool(popups)

    def _dismiss_overlay(self) -> bool:
        limit = _milliseconds("JEV_MAX_POPUP_DISMISSALS", 8)
        used = getattr(self, "_popup_dismissals", 0)
        if used >= limit:
            return False
        found = self.evaluate(OVERLAY_PROBE)
        if not isinstance(found, dict) or not found.get("found"):
            return False
        self._popup_dismissals = used + 1
        summary = found.get("summary") or ""
        if found.get("x") is None:
            # An interruption with no safe close control: Escape closes most modals.
            say(f"   ✕ popup has no close button; pressing Escape ({summary!r})", "yellow")
            for event in ("rawKeyDown", "keyUp"):
                self.call(
                    "Input.dispatchKeyEvent",
                    type=event,
                    key="Escape",
                    code="Escape",
                    windowsVirtualKeyCode=27,
                )
        else:
            say(f"   ✕ dismissed popup via {found['name']!r} ({summary!r})", "yellow")
            for event in ("mouseMoved", "mousePressed", "mouseReleased"):
                self.call(
                    "Input.dispatchMouseEvent",
                    type=event,
                    x=found["x"],
                    y=found["y"],
                    button="left" if event != "mouseMoved" else "none",
                    clickCount=1 if event != "mouseMoved" else 0,
                )
        time.sleep(0.35)
        return True

    def _dismiss_native_dialog(self) -> None:
        """alert/confirm/beforeunload freeze the page; cancel them so observation can continue."""
        if not getattr(self, "session", None):
            return
        try:
            from browser_harness.helpers import _send

            dialog = _send({"meta": "pending_dialog"}).get("dialog")
            if not dialog:
                return
            self.call("Page.handleJavaScriptDialog", accept=False)
            say(f"   ✕ dismissed a browser {dialog.get('type', 'dialog')}: "
                f"{str(dialog.get('message', ''))[:60]!r}", "yellow")
        except Exception as exc:
            say(f"   ⚠ could not dismiss a browser dialog: {exc}", "dim")

    def fresh(self, page: dict[str, Any], action: dict[str, Any] | None = None):
        if action is not None and action.get("kind") in {"click", "select", "fill"}:
            node = action.get("node")
            page_key = page.get("page_key")
            if type(node) is not int or not isinstance(page_key, list | tuple) or len(page_key) < 2:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey().slice(0,2),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            if not isinstance(current, list) or len(current) != 2:
                say(
                    "   ⛨ safety rejected action: browser returned no current target state",
                    "yellow",
                )
                return False
            expected_guard = (page.get("guards") or {}).get(str(node))
            expected_signature = _target_signature(expected_guard)
            current_signature = _target_signature(current[1])
            if current[0] != list(page_key[:2]):
                say("   ⛨ safety rejected action: document or URL changed", "yellow")
                return False
            if expected_signature is None or current_signature is None:
                say("   ⛨ safety rejected action: retained target disappeared", "yellow")
                return False
            if current_signature != expected_signature:
                changed = [
                    field
                    for field, before, after in zip(
                        _GUARD_FIELDS, expected_signature, current_signature, strict=True
                    )
                    if before != after
                ]
                say(
                    "   ⛨ safety rejected action: target changed "
                    f"({', '.join(changed) or 'unknown field'})",
                    "yellow",
                )
                return False
            if current[1][13] != expected_guard[13]:
                say("   ✓ target unchanged; ignored unrelated surrounding-text churn", "dim")
            return True
        return super().fresh(page, action)
