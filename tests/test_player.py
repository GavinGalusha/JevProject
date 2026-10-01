from unittest.mock import Mock, patch

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


def _player_probe(playing=False):
    return {
        "node": 41,
        "guard": ["g"] * 14,
        "tag": "iframe",
        "playing": playing,
        "rect": {"x": 0, "y": 84, "w": 900, "h": 500},
    }


def _labels(page):
    return [a["label"] for a in page["actions"]]


def test_after_a_click_that_did_not_start_playback_the_player_is_offered_again():
    browser = _browser(_player_probe())
    browser._player_clicks = 1
    browser._wait_until_playing = Mock(return_value=False)

    page = browser._with_video_player(_page())

    assert PLAYER_LABEL in _labels(page)
    assert "has not started playing yet" in page["text"]


def test_once_playing_the_player_is_not_offered_so_it_cannot_be_paused():
    browser = _browser(_player_probe())
    browser._player_clicks = 2
    browser._wait_until_playing = Mock(return_value=True)

    page = browser._with_video_player(_page())

    assert PLAYER_LABEL not in _labels(page)
    assert "now playing" in page["text"]


def test_a_video_element_that_is_already_playing_is_reported_without_a_click():
    browser = _browser(_player_probe(playing=True))
    page = browser._with_video_player(_page())
    assert PLAYER_LABEL not in _labels(page) and "now playing" in page["text"]


def test_after_the_click_limit_the_page_is_refreshed_before_giving_up():
    from jev_remote.player import MAX_PLAYER_CLICKS

    browser = _browser(_player_probe())
    browser._player_clicks = MAX_PLAYER_CLICKS
    browser._wait_until_playing = Mock(return_value=False)

    page = browser._with_video_player(_page())

    assert browser._reload_requested is True
    assert browser._player_refreshes == 1
    assert "was refreshed" in page["text"]
    assert PLAYER_LABEL in _labels(page)


def test_the_player_is_given_up_on_once_refreshes_are_used_up():
    from jev_remote.player import MAX_PLAYER_CLICKS, MAX_PLAYER_REFRESHES

    browser = _browser(_player_probe())
    browser._player_clicks = MAX_PLAYER_CLICKS
    browser._player_refreshes = MAX_PLAYER_REFRESHES
    browser._wait_until_playing = Mock(return_value=False)

    page = browser._with_video_player(_page())

    assert browser._reload_requested is False
    assert PLAYER_LABEL not in _labels(page)
    assert "did not start playing" in page["text"]


def test_reloading_resets_the_click_count_and_waits_for_the_player_again(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = _browser("complete")
    browser.call = Mock()
    browser._player_clicks = 4
    browser._player_waited_url = "https://site.example/ep"

    browser._reload_page()

    assert browser.call.call_args.args[0] == "Page.reload"
    assert browser._player_clicks == 0 and browser._player_waited_url is None


def test_observe_reloads_when_asked_and_reads_the_fresh_page(monkeypatch):
    browser = _browser(None)
    browser._observe_page = Mock(side_effect=[_page(), _page()])
    browser._clear_interruptions = Mock(return_value=False)
    browser._reload_page = Mock()

    def wants_reload(page):
        if browser._reload_page.call_count == 0:
            browser._reload_requested = True
        return page

    browser._with_video_player = Mock(side_effect=wants_reload)

    browser.observe(screenshot=False)

    browser._reload_page.assert_called_once()
    assert browser._observe_page.call_count == 2  # once before, once after the refresh


def test_the_play_button_inside_the_frame_is_mapped_to_page_coordinates():
    browser = _browser(None)
    # node rect in the page, then no same-origin access, so the cross-origin frame is asked
    browser.evaluate = Mock(side_effect=[[0, 84, 900, 500], None])
    with patch("browser_harness.helpers.cdp") as cdp:
        cdp.side_effect = [
            {"targetInfos": [{"type": "iframe", "targetId": "T", "url": "https://p.example/e/1"}]},
            {"sessionId": "S"},
            {"result": {"value": {"x": 450.0, "y": 250.0}}},
        ]
        point = browser._play_button_point(41, "https://p.example/e/1")

    assert point == (450.0, 334.0)  # frame coordinates + the iframe's page offset


def test_a_play_button_outside_the_frame_is_ignored():
    browser = _browser(None)
    browser.evaluate = Mock(side_effect=[[0, 84, 900, 500], {"x": 5000.0, "y": 10.0}])
    assert browser._play_button_point(41, "https://p.example/e/1") is None


def test_no_pixel_check_before_the_first_click():
    browser = _browser(_player_probe())
    browser._wait_until_playing = Mock(return_value=True)
    page = browser._with_video_player(_page())
    browser._wait_until_playing.assert_not_called()
    assert PLAYER_LABEL in _labels(page) and "[Jev]" not in page["text"]


def test_frames_change_detects_motion_and_stillness(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    rect = {"x": 0, "y": 84, "w": 900, "h": 500}

    still = _browser(None)
    still.call = Mock(return_value={"data": "same-frame"})
    assert still._frames_change(rect) is False

    moving = _browser(None)
    moving.call = Mock(side_effect=[{"data": "a"}, {"data": "b"}, {"data": "c"}])
    assert moving._frames_change(rect) is True

    broken = _browser(None)
    broken.call = Mock(side_effect=RuntimeError("tab closed"))
    assert broken._frames_change(rect) is False


def test_videos_playing_needs_an_advancing_clock():
    from jev_remote.player import videos_playing

    moving = ([{"paused": False, "ended": False, "t": 1.0}], [{"paused": False, "t": 1.7}])
    assert videos_playing(*moving) is True
    stuck = ([{"paused": False, "ended": False, "t": 1.0}], [{"paused": False, "t": 1.0}])
    assert videos_playing(*stuck) is False  # buffering or frozen
    paused = ([{"paused": True, "t": 1.0}], [{"paused": True, "t": 1.0}])
    assert videos_playing(*paused) is False
    assert videos_playing([], []) is False


def test_player_state_prefers_the_real_video_clock_over_pixels(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = _browser(None)
    reads = [[{"paused": False, "ended": False, "t": 2.0}], [{"paused": False, "t": 2.8}]]
    browser._read_videos = Mock(side_effect=reads)
    browser._frames_change = Mock(return_value=False)  # pixels say still; the clock wins

    assert browser._player_is_playing(_player_probe()) is True
    browser._frames_change.assert_not_called()


def test_a_frame_with_no_readable_video_is_judged_by_its_pixels(monkeypatch):
    # A video nested out of reach must not read as "not playing": that would make Jev pause it.
    browser = _browser(None)
    browser._read_videos = Mock(return_value=[])
    browser._frames_change = Mock(return_value=True)
    assert browser._player_is_playing(_player_probe()) is True

    browser._frames_change = Mock(return_value=False)
    assert browser._player_is_playing(_player_probe()) is False


def test_pixels_are_only_the_fallback_when_the_video_cannot_be_read():
    browser = _browser(None)
    browser._read_videos = Mock(return_value=None)
    browser._frames_change = Mock(return_value=True)
    assert browser._player_is_playing(_player_probe()) is True

    browser._read_videos = Mock(side_effect=RuntimeError("target gone"))
    browser._frames_change = Mock(return_value=False)
    assert browser._player_is_playing(_player_probe()) is False


def test_an_accidental_fullscreen_is_left_before_the_player_is_offered_again():
    browser = _browser({**_player_probe(), "fullscreen": True})
    browser._player_clicks = 1
    browser._wait_until_playing = Mock(return_value=False)
    browser.call = Mock()

    with patch("jev_remote.safe_browser.time.sleep"):
        page = browser._with_video_player(_page())

    assert PLAYER_LABEL in _labels(page)
    assert "exitFullscreen" in browser.call.call_args.kwargs["expression"]


def test_a_slow_starting_video_is_given_time_before_it_counts_as_not_started(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    clock = iter(range(0, 1000))
    monkeypatch.setattr("jev_remote.safe_browser.time.monotonic", lambda: next(clock) * 0.5)
    browser = _browser(None)
    browser._player_last_click_at = 0.0
    browser._player_is_playing = Mock(side_effect=[False, False, False, True])
    browser._video_loading = Mock(return_value=False)

    assert browser._wait_until_playing(_player_probe()) is True
    assert browser._player_is_playing.call_count == 4  # kept looking within the grace period


def test_waiting_gives_up_after_the_grace_period_when_nothing_is_loading(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    clock = iter(range(0, 1000))
    monkeypatch.setattr("jev_remote.safe_browser.time.monotonic", lambda: next(clock) * 1.0)
    monkeypatch.setenv("JEV_PLAYER_LOAD_WAIT_MS", "3000")
    browser = _browser(None)
    browser._player_last_click_at = 0.0
    browser._player_is_playing = Mock(return_value=False)
    browser._video_loading = Mock(return_value=False)

    assert browser._wait_until_playing(_player_probe()) is False


def test_a_buffering_video_is_waited_for_much_longer(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    clock = iter(range(0, 1000))
    monkeypatch.setattr("jev_remote.safe_browser.time.monotonic", lambda: next(clock) * 1.0)
    monkeypatch.setenv("JEV_PLAYER_LOAD_WAIT_MS", "3000")
    monkeypatch.setenv("JEV_PLAYER_BUFFER_WAIT_MS", "20000")
    browser = _browser(None)
    browser._player_last_click_at = 0.0
    seen = []

    def playing_eventually(_found):
        seen.append(1)
        return len(seen) >= 10  # starts well after the 3s grace period

    browser._player_is_playing = playing_eventually
    browser._video_loading = Mock(return_value=True)

    assert browser._wait_until_playing(_player_probe()) is True


def test_video_loading_means_told_to_play_but_not_buffered_yet():
    browser = _browser(None)
    browser._read_videos = Mock(return_value=[{"paused": False, "ended": False, "ready": 1}])
    assert browser._video_loading(_player_probe()) is True
    browser._read_videos = Mock(return_value=[{"paused": False, "ended": False, "ready": 4}])
    assert browser._video_loading(_player_probe()) is False
    browser._read_videos = Mock(return_value=[{"paused": True, "ended": False, "ready": 0}])
    assert browser._video_loading(_player_probe()) is False
    browser._read_videos = Mock(side_effect=RuntimeError("gone"))
    assert browser._video_loading(_player_probe()) is False
