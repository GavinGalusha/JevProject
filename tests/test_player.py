from unittest.mock import Mock

import pytest

from jev_remote.player import PLAYBACK_RULES, PLAYER_LABEL, with_playback_rules
from jev_remote.safe_browser import StableTargetBrowser


@pytest.mark.parametrize(
    "goal",
    [
        "Find The Office and play season 3, episode 5",
        "watch the first trending movie",
        "Search for Moana, pick the second server, and start playback",
    ],
)
def test_playback_goals_get_the_player_rule(goal):
    result = with_playback_rules(goal)
    assert result.startswith(goal)
    assert PLAYBACK_RULES in result
    assert with_playback_rules(result) == result  # idempotent


@pytest.mark.parametrize(
    "goal",
    [
        "Find the Wikipedia page for Apollo 11 and stop when it is open",
        "Find one-way flights from Zurich to London",
        "find gavin galusha on linkedin and click on one of his companies",
    ],
)
def test_other_goals_are_left_alone(goal):
    assert with_playback_rules(goal) == goal


def _browser(probe_result):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "session"
    browser.evaluate = Mock(return_value=probe_result)
    return browser


def _page():
    return {
        "url": "https://site.example/ep",
        "text": "Direct Links",
        "scroll": {"y": 0, "height": 900},
        "guards": {"3": ["guard-3"]},
        "marker": "marker",
        "actions": [
            {"id": "e1", "node": 3, "kind": "click", "role": "link", "label": "Wootly DVD"},
            {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }


def test_player_is_added_as_a_real_control_before_scroll_and_wait():
    probe = {"node": 41, "guard": ["g"] * 14, "tag": "iframe", "rect": {"x": 0, "y": 80}}
    page = _page()
    before = page["marker"]

    result = _browser(probe)._with_video_player(page)

    assert [a["id"] for a in result["actions"]] == ["e1", "e_player", "scroll_down", "wait"]
    player = result["actions"][1]
    assert player["label"] == PLAYER_LABEL and player["kind"] == "click" and player["node"] == 41
    assert result["guards"]["41"] == ["g"] * 14
    assert result["marker"] == before  # the page-wide freshness marker must not change


def test_no_player_leaves_the_page_untouched():
    page = _page()
    assert _browser(None)._with_video_player(page)["actions"] == _page()["actions"]


def test_player_option_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_PLAYER_ACTION", "0")
    browser = _browser({"node": 41, "guard": [], "tag": "iframe", "rect": {}})
    assert len(browser._with_video_player(_page())["actions"]) == 3
    browser.evaluate.assert_not_called()


def test_probe_failure_never_breaks_observation():
    browser = _browser(None)
    browser.evaluate.side_effect = RuntimeError("document changed")
    assert len(browser._with_video_player(_page())["actions"]) == 3


def test_playback_runs_wait_for_a_player_that_loads_late(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    probe = {"node": 41, "guard": ["g"] * 14, "tag": "iframe", "rect": {}}
    browser = _browser(None)
    browser.evaluate = Mock(side_effect=[None, None, None, probe])
    browser.expect_player = True

    result = browser._with_video_player(_page())

    assert any(a["label"] == PLAYER_LABEL for a in result["actions"])


def test_waiting_for_the_player_happens_once_per_page_and_only_for_playback(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    monkeypatch.setenv("JEV_PLAYER_WAIT_MS", "0")
    browser = _browser(None)
    browser.expect_player = True
    browser._with_video_player(_page())
    calls_after_first = browser.evaluate.call_count
    browser._with_video_player(_page())
    assert browser.evaluate.call_count == calls_after_first + 1  # no second wait on the same URL

    other = _browser(None)  # not a playback run: one probe, no waiting
    other._with_video_player(_page())
    assert other.evaluate.call_count == 1


def test_default_agent_factory_marks_playback_runs():
    from jev_remote.player import is_playback_goal

    assert is_playback_goal(with_playback_rules("play Moana"))
    assert not is_playback_goal("find the Wikipedia page for Apollo 11")
