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
    if (e.tagName === 'VIDEO' && !e.paused && !e.ended) continue;  // already playing
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
    rect: {x: r.x, y: r.y, w: r.width, h: r.height},
  };
})()"""

_PLAYBACK_INTENT = re.compile(
    r"\b(?:play|watch|stream|start playback|put on|start the)\b", re.IGNORECASE
)
_MARKER = "Playback rules:"

PLAYBACK_RULES = (
    f"{_MARKER} once the movie or episode page is open and a video player is visible, click the "
    "video player itself to start playback. Do not open 'Direct Links', download, mirror, "
    "external-host or other server links, and do not leave the episode page while its player is "
    "visible. If this looks like a movie or episode page but no video player is offered yet, "
    "WAIT for it to load instead of opening other links. After you have clicked the video player "
    "once, the task is complete: choose DONE and do not click the player again, because that "
    "would pause it."
)


def with_playback_rules(goal: str) -> str:
    """Append the playback rules to goals that ask for something to be played."""
    if _MARKER in goal or not _PLAYBACK_INTENT.search(goal):
        return goal
    return f"{goal}\n\n{PLAYBACK_RULES}"


def is_playback_goal(goal: str) -> bool:
    """True once ``with_playback_rules`` has been applied, i.e. the run is about playing media."""
    return _MARKER in goal
