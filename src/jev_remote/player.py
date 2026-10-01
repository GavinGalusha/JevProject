"""Make the page's video player a first-class option, and keep playback goals on it.

Embedded players are almost always cross-origin ``<iframe>`` elements, which the upstream page
reader does not list. Without an option for it, the decision model can only see the surrounding
links ("Direct Links", mirrors, downloads) and opens one of those instead of pressing play.
"""

from __future__ import annotations

import re

PLAYER_LABEL = "Video player — click to start playback"

# Runs in the page. Finds the largest visible player and registers it with Jev's element cache,
# so the normal freshness and hit-test checks apply to it like any other control. Never clicks.
PLAYER_PROBE = r"""(() => {
  const cache = window.__jevFast;
  if (!cache) return null;
  const vw = innerWidth, vh = innerHeight;
  let best = null;
  for (const e of document.querySelectorAll('video,iframe,embed,object')) {
    if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    if (e.closest('[aria-hidden="true"],[inert]')) continue;
    const r = e.getBoundingClientRect();
    const w = Math.min(r.right, vw) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    // Big enough to be the main player, not a banner ad or a widget.
    if (w < 320 || h < 180 || w * h < 0.12 * vw * vh) continue;
    if (!best || w * h > best.area) best = {e, area: w * h, r};
  }
  if (!best) return null;
  const e = best.e;
  let id = cache.ids.get(e);
  if (!id) { id = cache.next++; cache.ids.set(e, id); }
  cache.nodes.set(id, e);
  const r = best.r;
  return {
    node: id,
    guard: cache.guard(e),
    tag: e.tagName.toLowerCase(),
    src: (e.src || e.currentSrc || '').slice(0, 300),
    fullscreen: !!document.fullscreenElement,
    playing: e.tagName === 'VIDEO' && !e.paused && !e.ended,
    rect: {x: r.x, y: r.y, w: r.width, h: r.height},
  };
})()"""

# Reads the real <video> state inside a player frame (run in that frame, never in the page).
VIDEO_STATE_JS = r"""(() => [...document.querySelectorAll('video')].map(v => ({
  paused: v.paused, ended: v.ended, t: v.currentTime, ready: v.readyState, d: v.duration,
})))()"""


# Same, for a same-origin iframe or a <video> reached through Jev's element cache.
def inline_video_js(node: int) -> str:
    return (
        "(() => { const e = window.__jevFast && window.__jevFast.nodes.get(%d);"
        " if (!e) return null;"
        " const pick = d => [...d.querySelectorAll('video')].map(v => ({paused: v.paused,"
        " ended: v.ended, t: v.currentTime, ready: v.readyState, d: v.duration}));"
        " if (e.tagName === 'VIDEO') return pick({querySelectorAll: () => [e]});"
        " try { return e.contentDocument ? pick(e.contentDocument) : null; }"
        " catch (x) { return null; } })()" % node
    )


def videos_playing(first: list[dict], second: list[dict], min_duration: float = 120.0) -> bool:
    """True if a real video is unpaused and its clock moved between two reads.

    Clips shorter than ``min_duration`` seconds are treated as ads, not the requested title. A
    live stream (infinite or unknown duration) counts.
    """
    for before, after in zip(first, second, strict=False):
        if before.get("paused") or after.get("paused") or after.get("ended"):
            continue
        duration = after.get("d")
        if isinstance(duration, int | float) and 0 < duration < min_duration:
            continue  # a pre-roll ad or a short clip, not the movie or episode
        if float(after.get("t", 0)) > float(before.get("t", 0)) + 0.15:
            return True
    return False


_PLAYBACK_INTENT = re.compile(
    r"\b(?:play|watch|stream|start playback|put on|start the)\b", re.IGNORECASE
)
_MARKER = "Playback rules:"

PLAYBACK_RULES = (
    f"{_MARKER} once the movie or episode page is open and a video player is visible, click the "
    "video player itself to start playback: its play button if one is visible, otherwise the "
    "centre of the player. Do not open 'Direct Links', download, mirror, external-host or other "
    "server links, and do not leave the episode page while its player is visible. If this looks "
    "like a movie or episode page but no video player is offered yet, WAIT for it to load instead "
    "of opening other links. Players often need more than one click (the first can just wake the "
    "player or trigger an ad), and Jev refreshes the page itself if the player will not start. "
    "The task is complete only when the page text says the video player is playing: choose DONE "
    "then, and never click a player that is already playing, because that would pause it. If the "
    "page text says it has not started yet, click the video player again."
)

# Runs inside the player frame: where is its own fullscreen button? Never clicks.
FULLSCREEN_BUTTON_JS = r"""(() => {
  const sel = '.vjs-fullscreen-control,.jw-icon-fullscreen,' +
    '.plyr__control[data-plyr="fullscreen"],.ytp-fullscreen-button,.fullscreen,' +
    '.fullscreen-button,.btn-fullscreen,#fullscreen,' +
    'button[aria-label*="full" i],[role="button"][aria-label*="full" i],[title*="full" i],' +
    '[class*="fullscreen" i]';
  for (const e of document.querySelectorAll(sel)) {
    if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 8 || r.height < 8 || r.right <= 0 || r.bottom <= 0) continue;
    if (r.left >= innerWidth || r.top >= innerHeight) continue;
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  }
  return null;
})()"""

NOTE_PLAYING = "[Jev] The video player is now playing. The playback goal is complete."
NOTE_NOT_STARTED = (
    "[Jev] The video player has not started playing yet (clicked {clicks} time(s) so far). "
    "Click the video player again."
)
NOTE_GAVE_UP = (
    "[Jev] The video player did not start playing after {clicks} clicks and {refreshes} page "
    "refresh(es)."
)
NOTE_REFRESHED = (
    "[Jev] The page was refreshed because the video player did not start. Click the video "
    "player to start playback."
)
MAX_PLAYER_CLICKS = 4  # per page load; then refresh the page
MAX_PLAYER_REFRESHES = 2

# Runs inside the player frame: where is its own play button? Never clicks.
PLAY_BUTTON_JS = r"""(() => {
  const sel = '.vjs-big-play-button,.jw-icon-display,.plyr__control--overlaid,' +
    '.ytp-large-play-button,.play-button,.play_button,.btn-play,.play,#play,' +
    'button[aria-label*="play" i],' +
    '[role="button"][aria-label*="play" i],[class*="big-play" i],[title*="play" i]';
  for (const e of document.querySelectorAll(sel)) {
    if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 12 || r.height < 12 || r.right <= 0 || r.bottom <= 0) continue;
    if (r.left >= innerWidth || r.top >= innerHeight) continue;
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  }
  return null;
})()"""


def with_playback_rules(goal: str) -> str:
    """Append the playback rules to goals that ask for something to be played."""
    if _MARKER in goal or not _PLAYBACK_INTENT.search(goal):
        return goal
    return f"{goal}\n\n{PLAYBACK_RULES}"


def is_playback_goal(goal: str) -> bool:
    """True once ``with_playback_rules`` has been applied, i.e. the run is about playing media."""
    return _MARKER in goal
