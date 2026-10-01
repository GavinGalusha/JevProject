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
    assert "currently shows Season 5" in wrong and "click the 'Season 4' button once" in wrong
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
    assert "click the 'Season 4' button once" in page["text"]


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
        ("Series How I Met Your Mother S4 E6: Happily Ever After", True),
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
    browser._scroll_to_target_episode = Mock(side_effect=lambda: order.append("scroll") or True)
    browser._clear_interruptions = Mock(return_value=False)
    browser._label_series_controls = Mock(side_effect=lambda page: page)
    browser._with_video_player = Mock(side_effect=lambda page: page)

    browser.observe(screenshot=False)

    assert order == ["read", "scroll", "read"]  # re-read once the row is in view
