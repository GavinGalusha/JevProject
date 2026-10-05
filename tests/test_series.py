from unittest.mock import Mock

import pytest

from jev_remote.safe_browser import StableTargetBrowser
from jev_remote.series import (
    SERIES_RULES,
    on_episode_page,
    parse_episode_goal,
    season_note,
    with_series_rules,
)


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("play season 4 episode 6 of how i met your mother", (4, 6)),
        ("Play How I Met Your Mother Season 4, Episode 6", (4, 6)),
        ("watch season 4 - ep. 6", (4, 6)),
        ("play episode 6 of season 4", (4, 6)),
        ("play s04e06", (4, 6)),
        ("play S4 E6 of HIMYM", (4, 6)),
        ("play 4x06", (4, 6)),
        ("Original user request: find the office and play season 3, episode 5", (3, 5)),
    ],
)
def test_parse_episode_goal(goal, expected):
    assert parse_episode_goal(goal) == expected


@pytest.mark.parametrize(
    "goal",
    ["play Moana", "find the Wikipedia page for Apollo 11", "season finale", "play episode", ""],
)
def test_no_episode_means_no_series_handling(goal):
    assert parse_episode_goal(goal) is None
    assert with_series_rules(goal) == goal


def test_series_rules_are_appended_once():
    goal = "play season 4 episode 6 of himym"
    once = with_series_rules(goal)
    assert SERIES_RULES in once and with_series_rules(once) == once


def test_season_note_tells_the_model_what_to_do_next():
    assert season_note(None, (4, 6)) is None
    wrong = season_note(5, (4, 6))
    assert "currently shows Season 5" in wrong and "choose 'Season 4' once" in wrong
    right = season_note(4, (4, 6))
    assert "requested season" in right and "Season 4 Episode 6" in right
    assert "Do not click any season button" in right
    assert "goal needs" not in season_note(5, None)


def _browser(result, target=(4, 6)):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "session"
    browser.evaluate = Mock(return_value=result)
    browser.target_episode = target
    return browser


def _page():
    return {
        "url": "https://site.example/series",
        "text": "How I Met Your Mother",
        "scroll": {"y": 0, "height": 3000},
        "actions": [
            {"id": "e1", "node": 10, "kind": "click", "role": "button", "label": "4"},
            {"id": "e2", "node": 11, "kind": "click", "role": "button", "label": "6"},
            {"id": "e3", "node": 12, "kind": "click", "role": "link", "label": "Some Title"},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }


def test_bare_digit_buttons_and_episode_links_get_unambiguous_labels():
    labels = {"10": "Season 4", "11": "Season 6", "12": "Season 4 Episode 6: Some Title"}
    browser = _browser({"labels": labels, "current_season": 5})

    page = browser._label_series_controls(_page())

    assert [a["label"] for a in page["actions"][:3]] == list(labels.values())
    assert page["actions"][3]["label"] == "Wait for the page to update"
    assert "currently shows Season 5" in page["text"]
    assert "choose 'Season 4' once" in page["text"]


def test_pages_without_seasons_are_left_alone():
    browser = _browser({"labels": {}, "current_season": None})
    original = _page()
    page = browser._label_series_controls(_page())
    assert page["actions"] == original["actions"] and page["text"] == original["text"]


def test_relabelling_failures_never_break_observation():
    browser = _browser(None)
    browser.evaluate.side_effect = RuntimeError("document changed")
    assert browser._label_series_controls(_page())["actions"] == _page()["actions"]


def test_the_requested_episode_is_scrolled_into_view_only_when_a_goal_names_one():
    browser = _browser(True)
    browser._scroll_to_target_episode()
    assert browser.evaluate.call_args.args[0].rstrip().endswith("(4, 6)")

    other = _browser(True, target=None)
    other._scroll_to_target_episode()
    other.evaluate.assert_not_called()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How I Met Your Mother S4, E6 - Happily Ever After", True),
        ("Season 4 Episode 6 Happily Ever After", True),
        ("How I Met Your Mother S4, E16 - Something", False),
        ("Season 4 Episode 6: Happily Ever After (a link in a list)", False),
        ("How I Met Your Mother S5, E6", False),
    ],
)
def test_on_episode_page(text, expected):
    assert on_episode_page(text, (4, 6)) is expected
    assert on_episode_page(text, None) is False


def test_the_page_says_when_the_requested_episode_is_already_open():
    browser = _browser({"labels": {}, "current_season": None})
    page = _page()
    page["text"] = "GOOJARA.to How I Met Your Mother S4, E6 - Happily Ever After"
    page = browser._label_series_controls(page)
    assert "You are on the page for Season 4 Episode 6" in page["text"]
    assert "start the video player" in page["text"]


def test_the_requested_season_row_is_read_after_the_scroll_not_before():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.target_episode = (4, 6)
    order = []
    browser._observe_page = Mock(side_effect=lambda *_: order.append("read") or _page())
    browser._scroll_to_target_episode = Mock(side_effect=lambda *_: order.append("scroll") or True)
    browser._clear_interruptions = Mock(return_value=False)
    browser._label_series_controls = Mock(side_effect=lambda page: page)
    browser._with_video_player = Mock(side_effect=lambda page: page)

    browser.observe(screenshot=False)

    assert order == ["read", "scroll", "read"]  # re-read once the row is in view


@pytest.mark.parametrize(
    ("title", "url", "expected"),
    [
        # Tubi: the code is in the title and the URL, but a series page lists every code
        ("Watch Everybody Hates Chris S01:E03 - Everybody Hates Basketball", "", True),
        ("", "https://tubitv.com/tv-shows/200203260/s01-e03-everybody-hates-basketball", True),
        (
            "Watch Everybody Hates Chris Streaming Online",
            "https://tubitv.com/series/300015509/x",
            False,
        ),
        (
            "Watch Everybody Hates Chris S01:E13 - Other",
            "https://tubitv.com/tv-shows/1/s01-e13-x",
            False,
        ),
        ("Watch ... Season 1 Episode 3 - Title", "", True),
    ],
)
def test_on_episode_page_uses_the_title_and_url(title, url, expected):
    listed_everywhere = "S01:E01 - Pilot S01:E02 - Two S01:E03 - Basketball"
    assert on_episode_page(listed_everywhere, (1, 3), title, url) is expected


def test_a_series_page_that_lists_every_code_is_not_an_episode_page():
    body = "Season 1 S01:E01 - Pilot S01:E02 - Two S01:E03 - Basketball"
    assert (
        on_episode_page(body, (1, 3), "Watch Show Streaming Online", "https://t.tv/series/1")
        is False
    )


@pytest.mark.parametrize(
    "goal", ["play season 1 episode 3 of everybody hates chris", "play s01e03"]
)
def test_goal_parsing_for_the_tubi_demo(goal):
    assert parse_episode_goal(goal) == (1, 3)


# ---------- the shipped JavaScript itself (a regex literal in a Python string is easy to get wrong)
def _shipped_row_like():
    import re

    from jev_remote.series import scroll_to_episode_js

    match = re.search(r"const rowLike = (/.*/[a-z]*);", scroll_to_episode_js(4, 6))
    assert match, "rowLike literal not found in the generated JavaScript"
    return match.group(1)


def test_generated_regex_literals_do_not_have_doubled_backslashes():
    from jev_remote.series import relabel_js, scroll_to_episode_js

    for js in (scroll_to_episode_js(4, 6), relabel_js([1])):
        # '\\d' inside a JS regex literal means "backslash, then d": it would never match
        assert "\\\\d" not in js and "\\\\w" not in js


def test_the_shipped_list_detector_matches_goojara_and_tubi_rows():
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (
        f"const r = {_shipped_row_like()};"
        "const rows = ['24 Season 9 Last Forever', '06 Season 4 Happily Ever After',"
        " 'S02:E04 - Everybody Hates a Liar', 'S01:E01 - Pilot'];"
        "const no = ['How I Met Your Mother (2005)', 'Season 4 1 2 3 4 5', 'Movies', 'Sign in'];"
        "console.log(JSON.stringify([rows.map(t => r.test(t)), no.map(t => r.test(t))]));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[[true,true,true,true],[false,false,false,false]]"


def test_no_scrolling_on_the_requested_episodes_own_page():
    # Tubi repeats the code in the breadcrumb/footer; scrolling there hides the video
    browser = _browser(True, target=(2, 4))
    page = {
        "text": "S02:E04 - Everybody Hates A Liar footer",
        "title": "Watch Everybody Hates Chris S02:E04 - Everybody Hates a Liar",
        "url": "https://tubitv.com/tv-shows/200203411/s02-e04-everybody-hates-a-liar",
    }
    assert browser._scroll_to_target_episode(page) is False
    browser.evaluate.assert_not_called()


def test_scrolling_still_happens_on_the_series_list():
    browser = _browser(True, target=(2, 4))
    page = {
        "text": "S02:E01 - Pilot S02:E04 - Everybody Hates a Liar",
        "title": "Watch Everybody Hates Chris Streaming Online",
        "url": "https://tubitv.com/series/300015509/everybody-hates-chris",
    }
    assert browser._scroll_to_target_episode(page) is True
    browser.evaluate.assert_called_once()
