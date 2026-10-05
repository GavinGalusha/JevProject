"""Login walls, paywalls and CAPTCHAs: never go through them, back out and try another link.

Jev never types credentials and never solves a CAPTCHA. When a click lands on one, the browser
goes back one step, the link that led there is hidden from the model for the rest of the command,
and a note asks it to choose a different link.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from .popups import site_of

# Runs in the page. Returns null, or {kind, reason, transient}. It never clicks.
WALL_PROBE = r"""(() => {
  if (!document.body) return null;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const shown = e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 &&
      e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  };
  const text = norm(document.body.innerText).slice(0, 4000);
  const title = norm(document.title);
  const path = location.pathname.toLowerCase();

  // CAPTCHA widgets and bot checks
  const captchaFrame = [...document.querySelectorAll('iframe')].some(f => shown(f) &&
    /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|geetest/i
      .test(f.src || ''));
  const captchaWidget = [...document.querySelectorAll(
    '.g-recaptcha,.h-captcha,.cf-turnstile,#cf-challenge-running,#challenge-form,[data-sitekey]'
  )].some(shown);
  if (captchaFrame || captchaWidget) {
    return {kind: 'captcha', reason: 'a CAPTCHA is on the page', transient: false};
  }
  const humanCheck = new RegExp(
    "verify (that )?you(?:'re| are) (a )?human|are you (a )?(robot|human)|i'm not a robot|" +
    'prove you are human|complete the (captcha|security check)|solve the captcha|' +
    'unusual traffic|press and hold|security check to access|' +
    'attention required|access denied|you have been blocked');
  const passive = /^(just a moment|checking your browser|verifying you are human|one moment)/;
  const checking = /checking (if the site connection is secure|your browser)/;
  if (passive.test(title) || checking.test(text)) {
    const reason = 'a browser check ("' + title.slice(0, 40) + '")';
    return {kind: 'captcha', reason, transient: true};
  }
  if (humanCheck.test(text) && text.length < 2500) {
    return {kind: 'captcha', reason: 'a human-verification page', transient: false};
  }

  // Login walls and paywalls
  const passwordShown = [...document.querySelectorAll('input[type="password"]')].some(e =>
    shown(e) && e.getBoundingClientRect().bottom > 0 &&
    e.getBoundingClientRect().top < innerHeight);
  const loginUrl = new RegExp(
    '/(log-?in|sign-?in|authwall|checkpoint|auth|sso|oauth|account/login|users/sign_in)' +
    '(/|$|\\.|\\?)').test(path + '/');
  const wallText = new RegExp(
    '(sign|log) ?in to (continue|view|watch|see|access|read)|please (sign|log) ?in|' +
    'you must be (signed|logged) ?in|(sign|log) ?in (is )?required|' +
    'create (a free )?account to (continue|watch|view|read)|members only|' +
    'subscribe to (continue|watch|read|view)|premium (content|members)|' +
    'become a member to (watch|view|read)');
  const hasWallText = wallText.test(text);
  const loginTitle = /\b(sign|log) ?in\b|^welcome back/.test(title);
  if (passwordShown && (loginUrl || hasWallText || loginTitle)) {
    return {kind: 'login', reason: 'a sign-in form', transient: false};
  }
  if (loginUrl && hasWallText) return {kind: 'login', reason: 'a sign-in page', transient: false};
  if (hasWallText && text.length < 1200) {
    return {kind: 'login', reason: 'a "sign in / subscribe to continue" page', transient: false};
  }
  return null;
})()"""

_MARKER = "Wall rules:"
WALL_RULES = (
    f"{_MARKER} if a page asks you to sign in, subscribe or solve a CAPTCHA, never fill it in or "
    "try to get past it. Jev goes back a step by itself and hides the link that led there; "
    "choose a different link. "
    "Stop as soon as the goal is visible: when the requested results or page are showing, answer "
    "DONE. Do not open filters, sort menus, dropdowns or any other control that the goal did "
    "not ask for."
)

NOTE_WENT_BACK = (
    "[Jev] That page needed a {kind} ({reason}), so Jev went back to this page and hid that "
    "link{label}. Choose a different link."
)
NOTE_NO_HISTORY = (
    "[Jev] This page needs a {kind} ({reason}) and there is no earlier page to go back to. "
    "Do not interact with it."
)
NOTE_GAVE_UP = (
    "[Jev] {count} pages in a row needed a sign-in or CAPTCHA. Jev will not try more; "
    "choose BLOCKED."
)


def with_wall_rules(goal: str) -> str:
    return goal if _MARKER in goal else f"{goal}\n\n{WALL_RULES}"


def normalize_href(href: Any, base: str | None) -> str | None:
    """An absolute URL without its fragment, or None for empty / javascript: links."""
    if not isinstance(href, str) or not href.strip():
        return None
    href = href.strip()
    if href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    absolute = urljoin(base or "", href)
    return absolute.split("#", 1)[0] or None


def host_of(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def guard_href(page: dict[str, Any], node: Any) -> str | None:
    """The destination of an observed control: the 'destination' slot of its guard."""
    guard = (page.get("guards") or {}).get(str(node))
    if isinstance(guard, list) and len(guard) > 12:
        return normalize_href(guard[12], page.get("url"))
    return None


class Avoid:
    """Links to hide for the rest of the command because they led to a wall."""

    def __init__(self) -> None:
        self.hrefs: set[str] = set()
        self.sites: set[str] = set()
        self.labels: set[str] = set()

    def __bool__(self) -> bool:
        return bool(self.hrefs or self.sites or self.labels)

    def remember(self, culprit: dict[str, Any] | None, wall_url: str | None) -> str:
        """Record the link that led to the wall; returns its label for the note."""
        culprit = culprit or {}
        href, label = culprit.get("href"), (culprit.get("label") or "").strip()
        if href:
            self.hrefs.add(href)
        elif label:
            self.labels.add(label)
        # A wall on another site hides every link to that site; on the same site only the link.
        wall_site, from_site = site_of(wall_url), site_of(culprit.get("from_url"))
        if wall_site and from_site and wall_site != from_site:
            self.sites.add(wall_site)
        return label

    def excludes(self, page: dict[str, Any], action: dict[str, Any]) -> bool:
        if action.get("kind") != "click" or type(action.get("node")) is not int:
            return False
        href = guard_href(page, action["node"])
        if href and (href in self.hrefs or site_of(href) in self.sites):
            return True
        return (action.get("label") or "").strip() in self.labels and not href

    def apply(self, page: dict[str, Any]) -> list[str]:
        """Drop excluded controls from the page; returns the labels that were dropped."""
        if not self:
            return []
        kept, dropped = [], []
        for action in page.get("actions", []):
            (dropped if self.excludes(page, action) else kept).append(action)
        if dropped:
            page["actions"] = kept
        return [a.get("label", "") for a in dropped]


_LOOKS_LIKE_WALL_URL = re.compile(r"/(log-?in|sign-?in|authwall|checkpoint)\b", re.I)


def looks_like_wall_url(url: str | None) -> bool:
    return bool(_LOOKS_LIKE_WALL_URL.search(urlparse(url or "").path))
