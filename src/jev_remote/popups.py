"""Detect and dismiss interruptions: in-page overlays, popup tabs and native dialogs.

Everything here is deliberately conservative. A popup is only closed through an unambiguous
"close" control (X, Close, Dismiss, No thanks, Not now, Reject...), and only when the overlay
reads like an interruption (sign-in wall, cookie notice, newsletter, promotion). Overlays the task
itself opened, such as a date picker or passenger menu, are left alone. Nothing here ever clicks
Sign in, Accept, Allow, Subscribe, Install, Buy or anything similar.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# Text that makes an overlay an *interruption* rather than part of the task.
INTERRUPTION_WORDS = (
    r"sign[ -]?in|log[ -]?in|join (?:now|linkedin|us|for free)|create (?:an )?account|register|"
    r"sign[ -]?up|subscribe|newsletter|cookie|consent|privacy|gdpr|we use|your choices|"
    r"allow notifications|enable notifications|turn on notifications|install|"
    r"download (?:our|the) app|"
    r"get the app|special offer|limited time|discount|promo|% off|\bsale\b|advertisement|"
    r"sponsored|take (?:our|a) survey|give us feedback|rate us|don.t miss|stay updated|"
    r"stay in the loop|welcome back|unlock|continue reading|you.ve been selected|"
    r"you.re invited|disable (?:your )?ad ?block"
)

# Full-width header/footer bars only count as interruptions if they read like a consent notice.
CONSENT_WORDS = r"cookie|consent|privacy|gdpr|we use|your choices"

# Controls that are safe to click to get rid of an interruption.
DISMISS_NAME = (
    r"close|dismiss|close (?:dialog|modal|popup|pop-up|banner|window|this|notice|message)|"
    r"dismiss (?:dialog|modal|popup|banner|notice|message|sign[ -]?in.*|cookie.*)|"
    r"no,? thanks?|no thank you|not now|not today|maybe later|remind me later|ask me later|"
    r"skip|skip for now|skip this|later|reject all|reject|reject all cookies|decline|decline all|"
    r"deny|necessary only|only necessary|only essential|essential only|reject non-essential|"
    r"continue without (?:accepting|signing in|logging in|agreeing)|[×✕✖xX]"
)

# Never click a control whose name looks like this, even if it also says "close".
NEVER_CLICK_NAME = (
    r"sign[ -]?in|log[ -]?in|join|sign[ -]?up|register|subscribe|buy|purchase|pay|accept|agree|"
    r"allow|install|download|continue with|get started|upgrade|try |start (?:free )?trial|"
    r"\byes\b|\bok\b|confirm|submit|send|enable|turn on|add to|checkout"
)

# Runs in the page. Returns null, or the control to click plus why. It never clicks.
OVERLAY_PROBE = (
    r"""(() => {
  const INTERRUPT = new RegExp(%(interrupt)r, 'i');
  const CONSENT = new RegExp(%(consent)r, 'i');
  const tried = (window.__jevPopupTried ||= new WeakSet());
  const DISMISS = new RegExp('^(?:' + %(dismiss)r + ')$', 'i');
  const NEVER = new RegExp(%(never)r, 'i');
  const vw = innerWidth, vh = innerHeight;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 &&
      e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  };
  const label = e => norm(
    e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText ||
    e.getAttribute('alt') || e.value || '').replace(/[.!]+$/, '');
  const hint = e => (e.id + ' ' + (e.className && e.className.baseVal !== undefined
    ? e.className.baseVal : e.className) + ' ' + (e.getAttribute('data-testid') || '') + ' ' +
    (e.getAttribute('data-test') || '') + ' ' + (e.getAttribute('data-dismiss') || ''));

  // 1. Overlay containers: real dialogs, plus big fixed/sticky layers and consent-style bars.
  //    kind: 'dialog' (a real modal), 'layer' (covers much of the page), 'bar' (full-width strip).
  const found = new Map();
  for (const e of document.querySelectorAll(
      '[role="dialog"],[role="alertdialog"],[aria-modal="true"],dialog[open]')) {
    if (shown(e)) found.set(e, 'dialog');
  }
  const layerish = document.querySelectorAll(
    'div,section,aside,form,header,footer,[class*="modal" i],[class*="popup" i],' +
    '[class*="overlay" i],[class*="consent" i],[class*="cookie" i],[class*="banner" i],' +
    '[id*="modal" i],[id*="popup" i],[id*="consent" i],[id*="cookie" i]');
  let scanned = 0;
  for (const e of layerish) {
    if (++scanned > 4000) break;
    if (found.has(e)) continue;
    const s = getComputedStyle(e);
    if (s.position !== 'fixed' && s.position !== 'sticky' && s.position !== 'absolute') continue;
    if (s.position === 'absolute' && !(parseInt(s.zIndex, 10) >= 100)) continue;
    if (!shown(e)) continue;
    const r = e.getBoundingClientRect();
    const area = (Math.min(r.right, vw) - Math.max(r.left, 0)) *
      (Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const text = norm(e.innerText).slice(0, 3000);
    const chrome = e.closest('header,nav,footer,[role="banner"],[role="navigation"],' +
      '[role="contentinfo"]');
    if (area >= 0.2 * vw * vh && !(chrome && !CONSENT.test(text))) found.set(e, 'layer');
    else if (r.width >= 0.7 * vw && r.height >= 40 && (r.top <= 4 || r.bottom >= vh - 4) &&
             CONSENT.test(text)) found.set(e, 'bar');
  }

  // 2. Keep only interruptions; a datepicker or menu the task opened won't match.
  const overlays = [...found.keys()].filter(e =>
    INTERRUPT.test(norm(e.innerText).slice(0, 3000)) ||
    INTERRUPT.test(norm(e.getAttribute('aria-label'))));
  // Prefer the most specific (smallest) overlay so a page-wide wrapper doesn't win.
  overlays.sort((a, b) => a.getBoundingClientRect().width * a.getBoundingClientRect().height -
    b.getBoundingClientRect().width * b.getBoundingClientRect().height);

  for (const overlay of overlays) {
    const controls = overlay.querySelectorAll(
      'button,[role="button"],a,input[type="button"],input[type="submit"],[tabindex],' +
      '[aria-label],[data-dismiss],[class*="close" i],[class*="dismiss" i],' +
      '[data-testid*="close" i],[data-testid*="dismiss" i]');
    const options = [];
    for (const c of controls) {
      if (!shown(c) || tried.has(c)) continue;
      let name = label(c);
      if (!name && /close|dismiss/i.test(hint(c))) name = 'close';
      if (!name || name.length > 40 || NEVER.test(name) || !DISMISS.test(name)) continue;
      const r = c.getBoundingClientRect();
      for (const [px, py] of [[.5, .5], [.3, .5], [.7, .5], [.5, .3], [.5, .7]]) {
        const x = r.x + r.width * px, y = r.y + r.height * py;
        if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
        const hit = document.elementFromPoint(x, y);
        if (hit && c.contains(hit)) {
          const rank = /^(close|dismiss|[×✕✖xX])$/i.test(name) ? 0 :
            /reject|decline|deny|necessary|essential/i.test(name) ? 1 : 2;
          options.push({x, y, name, rank, area: r.width * r.height, el: c});
          break;
        }
      }
    }
    options.sort((a, b) => a.rank - b.rank || a.area - b.area);
    if (options.length) {
      const o = options[0];
      tried.add(o.el);
      return {found: true, x: o.x, y: o.y, name: o.name,
              summary: norm(overlay.innerText).slice(0, 80)};
    }
  }
  // A real modal with no safe control: Escape once. Never for bars, headers or footers.
  for (const overlay of overlays) {
    if (found.get(overlay) === 'bar' || tried.has(overlay)) continue;
    tried.add(overlay);
    return {found: true, x: null, y: null, name: null,
            summary: norm(overlay.innerText).slice(0, 80)};
  }
  return null;
})()"""
    % {
        "interrupt": INTERRUPTION_WORDS,
        "consent": CONSENT_WORDS,
        "dismiss": DISMISS_NAME,
        "never": NEVER_CLICK_NAME,
    }
)

_TWO_PART_SUFFIXES = {"co", "com", "org", "net", "gov", "ac", "edu"}


def site_of(url: str | None) -> str:
    """Rough registrable domain ('news.bbc.co.uk' -> 'bbc.co.uk'). Empty for blank pages."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2 or all(label.isdigit() for label in labels):
        return host
    keep = 3 if labels[-2] in _TWO_PART_SUFFIXES and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def popup_targets_to_close(
    targets: list[dict[str, Any]], owner: str, owner_url: str | None
) -> list[dict[str, Any]]:
    """Tabs our tab opened that are on another site. Blank tabs are left until they navigate."""
    own_site = site_of(owner_url)
    opened_by = {owner}
    chosen: list[dict[str, Any]] = []
    # A popup may itself open another popup, so follow the opener chain.
    for _ in range(3):
        for target in targets:
            if target.get("type") != "page" or target.get("targetId") in opened_by:
                continue
            if target.get("openerId") not in opened_by:
                continue
            opened_by.add(target["targetId"])
            site = site_of(target.get("url"))
            if site and site != own_site:
                chosen.append(target)
    return chosen
