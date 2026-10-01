"""Pick the right season and episode on sites that list a whole series on one page.

Three things go wrong for a model driving such a page, all seen on goojara:

* Season buttons are bare digits ("4", "6"), so "season 4 episode 6" is ambiguous and the model
  bounces between season buttons instead of choosing an episode.
* The season currently shown is a *disabled* button, which the page reader skips, so the model
  cannot tell which season it is looking at (and it differs from visit to visit).
* Episode rows are listed newest first and only the rows inside the viewport are observed, so
  episode 6 of 24 is hundreds of pixels below anything the model can see.

The fix is to label controls unambiguously, say which season is showing, and scroll the requested
episode into view. Nothing here clicks anything.
"""

from __future__ import annotations

import re

_NUMBER = r"(\d{1,3})"
_PATTERNS = (
    # "season 4 episode 6", "season 4, ep. 6", "season 4 - episode 6"
    (re.compile(r"\bseason\s*(\d{1,2})\W{0,12}(?:episode|ep\.?)\s*" + _NUMBER, re.I), (1, 2)),
    # "episode 6 of season 4", "episode 6 season 4"
    (
        re.compile(
            r"\b(?:episode|ep\.?)\s*" + _NUMBER + r"\W{0,12}(?:of\s+)?season\s*(\d{1,2})", re.I
        ),
        (2, 1),
    ),
    # "s4e6", "s04 e06"
    (re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})\b", re.I), (1, 2)),
    # "4x06"
    (re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I), (1, 2)),
)


def parse_episode_goal(goal: str) -> tuple[int, int] | None:
    """(season, episode) if the goal names one, else None."""
    for pattern, (season_group, episode_group) in _PATTERNS:
        match = pattern.search(goal or "")
        if match:
            season, episode = int(match.group(season_group)), int(match.group(episode_group))
            if season > 0 and episode > 0:
                return season, episode
    return None


_MARKER = "Series rules:"

SERIES_RULES = (
    f"{_MARKER} the numbers on season buttons are SEASONS and the numbers on episode rows are "
    "EPISODES; never mix them up. The page text states which season is currently shown. If it is "
    "not the requested season, click the 'Season N' button for the requested season once. Once the "
    "requested season is shown, never click another season button: click the row labelled "
    "'Season N Episode M: ...' for the requested episode instead (it is scrolled into view for "
    "you)."
)


def with_series_rules(goal: str) -> str:
    """Append the season/episode rules to goals that name a season and episode."""
    if _MARKER in goal or parse_episode_goal(goal) is None:
        return goal
    return f"{goal}\n\n{SERIES_RULES}"


# Runs in the page. Returns how to relabel controls and which season is showing. Never clicks.
def relabel_js(nodes: list[int]) -> str:
    return (
        r"""((nodes) => {
  const cache = window.__jevFast;
  const current = document.querySelector('[data-season][disabled]');
  const hasSeasons = !!document.querySelector('[data-season]') ||
    /\bSeason\s+\d/i.test(document.body ? document.body.innerText : '');
  if (!cache || !hasSeasons) return {labels: {}, current_season: null};
  const text = e => (e.innerText || '').trim().replace(/\s+/g, ' ');
  const labels = {};
  for (const node of nodes) {
    const el = cache.nodes.get(node);
    if (!el) continue;
    const attr = el.getAttribute('data-season') ?? (el.closest('[data-season]')
      ? el.closest('[data-season]').getAttribute('data-season') : null);
    if (attr && /^\d+$/.test(attr)) { labels[node] = 'Season ' + parseInt(attr, 10); continue; }
    const own = text(el);
    if (!own || /\bEpisode\s+\d/i.test(own)) continue;
    // A strip of bare numbers next to a "Season N" label ("Season 4 | 1 2 3 ... 24") is
    // an episode picker: each number is an EPISODE of that season.
    if (/^\d{1,3}$/.test(own) && el.parentElement) {
      const m = text(el.parentElement).match(/^Season\s+(\d{1,2})\s+1\s+2\s+3\b/i);
      if (m) { labels[node] = 'Season ' + (+m[1]) + ' Episode ' + (+own); continue; }
    }
    let p = el;
    for (let i = 0; i < 5 && p && p !== document.body; i++, p = p.parentElement) {
      const t = text(p);
      if (t.length > 600) break;
      let m;
      if ((m = t.match(/^(\d{1,3})\s+Season\s+(\d{1,2})\b/i))) {
        labels[node] = 'Season ' + (+m[2]) + ' Episode ' + (+m[1]) + ': ' + own; break;
      }
      if ((m = t.match(/\bSeason\s+(\d{1,2})\D{1,12}(?:Episode|Ep\.?)\s*(\d{1,3})\b/i))) {
        labels[node] = 'Season ' + (+m[1]) + ' Episode ' + (+m[2]) + ': ' + own; break;
      }
    }
  }
  const season = current ? parseInt(current.getAttribute('data-season'), 10) : null;
  return {labels, current_season: season};
})("""
        + str(list(nodes))
        + ")"
    )


def scroll_to_episode_js(season: int, episode: int) -> str:
    """Scroll the requested episode's row into view unless it is. Returns true if it moved."""
    return (
        r"""((season, episode) => {
  const text = e => (e.innerText || '').trim().replace(/\s+/g, ' ');
  const mine = [
    new RegExp('^0*' + episode + '\\s+Season\\s+' + season + '\\b', 'i'),
    new RegExp('\\bSeason\\s+' + season + '\\D{1,12}(?:Episode|Ep\\.?)\\s*0*' + episode +
      '\\b', 'i'),
    new RegExp('^S0*' + season + '\\s*E0*' + episode + '\\b', 'i')];
  // Only act on a real episode list (season buttons, or several "<n> Season <m>" rows), never on
  // a search result or article that merely mentions an episode.
  const rowLike = /^\\d{1,3}\\s+Season\\s+\\d{1,2}\\b/i;
  const listy = !!document.querySelector('[data-season]') ||
    [...document.querySelectorAll('div,li,tr,article,section,span')]
      .filter(e => { const t = text(e); return t.length < 600 && rowLike.test(t); }).length >= 3;
  if (!listy) return false;
  let best = null;
  for (const e of document.querySelectorAll('div,li,tr,article,section,span')) {
    const t = text(e);
    if (!t || t.length > 600 || !mine.some(r => r.test(t))) continue;
    if (!best || t.length < text(best).length) best = e;
  }
  if (!best) return false;
  const r = best.getBoundingClientRect();
  if (r.top >= 60 && r.bottom <= innerHeight - 60) return false;
  best.scrollIntoView({block: 'center'});
  return true;
})("""
        + f"{int(season)}, {int(episode)})"
    )


def on_episode_page(text: str, target: tuple[int, int] | None) -> bool:
    """True if the visible text names the requested episode as the page being viewed."""
    if not target:
        return False
    season, episode = target
    return bool(
        re.search(rf"\bS0*{season}\s*,?\s*E0*{episode}\b", text, re.I)
        or re.search(rf"\bSeason\s+{season}\s+Episode\s+0*{episode}\b(?!\s*:)", text, re.I)
    )


NOTE_ON_EPISODE = (
    "[Jev] You are on the page for Season {season} Episode {episode}. Do not click any season "
    "or episode number again; start the video player."
)


def season_note(current: int | None, target: tuple[int, int] | None) -> str | None:
    """A sentence for the page text: which season is showing, and what to do next."""
    if current is None:
        return None
    note = f"[Jev] The episode list currently shows Season {current}."
    if target:
        season, episode = target
        if current != season:
            note += f" The goal needs Season {season}: click the 'Season {season}' button once."
        else:
            note += (
                f" This is the requested season. Click the row labelled "
                f"'Season {season} Episode {episode}: ...'. Do not click any season button."
            )
    return note
