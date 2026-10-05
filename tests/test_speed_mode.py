from pathlib import Path
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from jev_remote import fast_text
from jev_remote.safe_browser import StableTargetBrowser, _wait_s

ROOT = Path(__file__).resolve().parent.parent
SPEED_KNOBS = (
    "JEV_NAV_POLL_MS",
    "JEV_SCROLL_SETTLE_MS",
    "JEV_POPUP_SETTLE_MS",
    "JEV_PLAYER_SAMPLE_MS",
    "JEV_PLAYER_POLL_MS",
    "JEV_PLAYER_WAIT_POLL_MS",
    "JEV_FULLSCREEN_POLL_MS",
    "JEV_FULLSCREEN_VERIFY_MS",
    "JEV_WAIT_UNTIL_CHANGE_MS",
    "JEV_FAST_TEXT",
    "JEV_FAST_PLAYER_WAIT",
)


@pytest.fixture(autouse=True)
def home_pipeline(monkeypatch):
    for name in SPEED_KNOBS:
        monkeypatch.delenv(name, raising=False)


def _sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda s: seen.append(round(s, 3)))
    return seen


def _browser():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "s"
    return browser


# ---------- without the profile, every wait is exactly what it was
def test_defaults_are_the_old_hard_coded_values():
    assert _wait_s("JEV_NAV_POLL_MS", 250) == 0.25
    assert _wait_s("JEV_PLAYER_SAMPLE_MS", 600) == 0.6
    assert _wait_s("JEV_PLAYER_POLL_MS", 500) == 0.5
    assert _wait_s("JEV_SCROLL_SETTLE_MS", 250) == 0.25
    assert _wait_s("JEV_POPUP_SETTLE_MS", 350) == 0.35


def test_fullscreen_check_by_default_is_one_second_then_one_look(monkeypatch):
    sleeps = _sleeps(monkeypatch)
    browser = _browser()
    browser._is_fullscreen = Mock(return_value=False)
    assert browser._wait_for_fullscreen() is False
    assert sleeps == [1.0] and browser._is_fullscreen.call_count == 1


def test_fullscreen_check_in_speed_mode_returns_the_moment_it_takes(monkeypatch):
    monkeypatch.setenv("JEV_FULLSCREEN_POLL_MS", "100")
    sleeps = _sleeps(monkeypatch)
    browser = _browser()
    browser._is_fullscreen = Mock(side_effect=[False, False, True])
    assert browser._wait_for_fullscreen() is True
    assert sleeps == [0.1, 0.1, 0.1]  # 0.3 s instead of a fixed 1 s


def test_fullscreen_check_gives_up_after_the_verify_window(monkeypatch):
    monkeypatch.setenv("JEV_FULLSCREEN_POLL_MS", "250")
    sleeps = _sleeps(monkeypatch)
    browser = _browser()
    browser._is_fullscreen = Mock(return_value=False)
    assert browser._wait_for_fullscreen() is False
    assert sum(sleeps) == pytest.approx(1.0) and browser._is_fullscreen.call_count == 4


def test_the_video_clock_gap_defaults_to_600ms_and_shrinks_in_speed_mode(monkeypatch):
    first = [{"paused": False, "ended": False, "t": 1.0, "d": 1500.0}]
    second = [{"paused": False, "ended": False, "t": 1.4, "d": 1500.0}]

    def check():
        sleeps = _sleeps(monkeypatch)
        browser = _browser()
        browser._read_videos = Mock(side_effect=[first, second])
        assert browser._player_is_playing({"node": 1}) is True
        return sleeps

    assert check() == [0.6]
    monkeypatch.setenv("JEV_PLAYER_SAMPLE_MS", "300")
    assert check() == [0.3]


# ---------- WAIT holds until the page changes (speed mode only)
def test_wait_until_change_is_off_by_default():
    browser = _browser()
    browser.evaluate = Mock()
    browser._wait_for_change({"marker": "m"})
    browser.evaluate.assert_not_called()


def test_wait_until_change_returns_as_soon_as_the_page_changes(monkeypatch):
    monkeypatch.setenv("JEV_WAIT_UNTIL_CHANGE_MS", "1500")
    sleeps = _sleeps(monkeypatch)
    browser = _browser()
    browser.evaluate = Mock(side_effect=["m", "m", "m2"])
    browser._wait_for_change({"marker": "m"})
    assert browser.evaluate.call_count == 3 and sleeps == [0.05, 0.05]


def test_wait_until_change_is_bounded(monkeypatch):
    monkeypatch.setenv("JEV_WAIT_UNTIL_CHANGE_MS", "60")
    _sleeps(monkeypatch)
    clock = iter(x * 0.03 for x in range(1000))
    monkeypatch.setattr("jev_remote.safe_browser.time.monotonic", lambda: next(clock))
    browser = _browser()
    browser.evaluate = Mock(return_value="same")
    browser._wait_for_change({"marker": "same"})  # returns instead of waiting forever
    assert browser.evaluate.call_count < 10


# ---------- typing without the model
@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        (
            "Search Google for Everybody Hates Chris season 2 episode 4 on Tubi and play it",
            "Everybody Hates Chris season 2 episode 4 on Tubi",
        ),
        ("Search YouTube for lofi hip hop radio and play the first video", "lofi hip hop radio"),
        (
            "play season 2 episode 4 of everybody hates chris",
            "season 2 episode 4 of everybody hates chris",
        ),
        (
            "find gavin galusha on linkedin and click on one of his companies",
            "gavin galusha on linkedin",
        ),
        (
            "Find the Wikipedia page for Apollo 11 and stop when it is open",
            "the Wikipedia page for Apollo 11",
        ),
    ],
)
def test_search_query_comes_from_the_users_own_words(goal, expected):
    assert fast_text.search_query(goal) == expected


@pytest.mark.parametrize("goal", ["", "play", "Original user request: play x\n\nRefined: y"])
def test_unclear_goals_are_left_to_the_model(goal):
    assert fast_text.search_query(goal) is None


def test_rules_appended_to_the_goal_are_ignored():
    goal = "play Moana\n\nPlayback rules: click the video player.\n\nWall rules: never sign in."
    assert fast_text.search_query(goal) == "Moana"


def test_only_search_boxes_skip_the_model():
    real = Mock(return_value=("model text", {"model": "gpt", "latency_ms": 1400, "usage": {}}))
    wrapped = fast_text.wrap_field_text(real)

    value, info = wrapped(
        {"goal": "Search Google for Moana and play it", "field": {"label": "Search"}}
    )
    assert value == "Moana" and info["latency_ms"] == 0 and real.call_count == 0

    value, _ = wrapped({"goal": "Search Google for Moana", "field": {"label": "Email address"}})
    assert value == "model text" and real.call_count == 1  # not a search field: ask the model

    wrapped({"goal": "", "field": {"label": "Search"}})
    assert real.call_count == 2  # no clear query: ask the model


# ---------- the demo profile only names settings that exist
def test_every_setting_in_the_demo_profile_exists_in_the_code():
    source = "\n".join(p.read_text() for p in (ROOT / "src" / "jev_remote").glob("*.py"))
    values = dotenv_values(ROOT / ".env.demo.example")
    assert values, "the demo profile is empty or unreadable"
    missing = [key for key in values if key not in source]
    assert not missing, f"unknown settings in .env.demo.example: {missing}"
