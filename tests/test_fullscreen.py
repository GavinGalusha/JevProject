from unittest.mock import Mock, patch

import pytest

from jev_remote.controller import RemoteController
from jev_remote.safe_browser import StableTargetBrowser


class FullscreenBrowser:
    target = "t"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def enter_fullscreen(self):
        self.calls += 1
        return self.results.pop(0) if self.results else False


class Agent:
    def __init__(self, browser):
        self.browser = browser


def _controller(agent):
    controller = RemoteController("https://example.com")
    controller._agent = agent
    return controller


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    monkeypatch.setenv("JEV_FULLSCREEN_DELAY_S", "0")
    monkeypatch.setenv("JEV_FULLSCREEN_RETRY_S", "0")
    monkeypatch.setattr("jev_remote.controller.time.sleep", lambda _s: None)


def test_fullscreen_is_retried_until_it_takes_effect():
    browser = FullscreenBrowser([False, False, True])
    agent = Agent(browser)
    controller = _controller(agent)

    controller._fullscreen_when_ready(agent)

    assert browser.calls == 3  # tried again each time it did not stick, then stopped


def test_fullscreen_gives_up_after_the_attempt_limit(monkeypatch):
    monkeypatch.setenv("JEV_FULLSCREEN_ATTEMPTS", "3")
    browser = FullscreenBrowser([])
    agent = Agent(browser)
    controller = _controller(agent)

    controller._fullscreen_when_ready(agent)

    assert browser.calls == 3


def test_fullscreen_waits_for_the_delay_before_the_first_attempt(monkeypatch):
    monkeypatch.setenv("JEV_FULLSCREEN_DELAY_S", "10")
    clock = {"now": 0.0}
    monkeypatch.setattr("jev_remote.controller.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "jev_remote.controller.time.sleep", lambda s: clock.__setitem__("now", clock["now"] + s)
    )
    browser = FullscreenBrowser([True])
    agent = Agent(browser)
    controller = _controller(agent)
    seen = []
    original = browser.enter_fullscreen
    browser.enter_fullscreen = lambda: seen.append(clock["now"]) or original()

    controller._fullscreen_when_ready(agent)

    assert seen and seen[0] >= 10.0


def test_a_new_command_cancels_the_pending_fullscreen():
    browser = FullscreenBrowser([True])
    agent = Agent(browser)
    controller = _controller(agent)
    controller._active_job_id = 7  # the next command is already running

    controller._fullscreen_when_ready(agent)

    assert browser.calls == 0


def test_stop_and_lock_cancels_the_pending_fullscreen():
    browser = FullscreenBrowser([True])
    agent = Agent(browser)
    controller = _controller(agent)
    controller._armed = False

    controller._fullscreen_when_ready(agent)

    assert browser.calls == 0


def test_a_replaced_tab_is_never_fullscreened():
    browser = FullscreenBrowser([True])
    old = Agent(browser)
    controller = _controller(Agent(FullscreenBrowser([])))  # a newer agent owns the remote

    controller._fullscreen_when_ready(old)

    assert browser.calls == 0


def _player_browser(fullscreen_after):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "s"
    browser.target = "t"
    states = list(fullscreen_after)
    browser._is_fullscreen = Mock(side_effect=lambda: states.pop(0) if states else True)
    browser._probe_player = Mock(return_value={"node": 5, "src": "https://p.example/e/1"})
    browser._resume_if_paused = Mock()
    browser.call = Mock(return_value={"result": {"value": "ok"}})
    browser.evaluate = Mock(return_value=[0.0, 84.0])
    return browser


def test_enter_fullscreen_uses_the_browser_api_first_and_verifies_it(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = _player_browser([False, True])  # not fullscreen, then fullscreen after the request
    with patch("browser_harness.helpers.cdp"):
        assert browser.enter_fullscreen() is True
    expressions = [c.kwargs.get("expression", "") for c in browser.call.call_args_list]
    assert any("requestFullscreen" in e for e in expressions)
    assert browser.call.call_args_list[-1].kwargs.get("userGesture") is True
    browser._resume_if_paused.assert_called_once()


def test_enter_fullscreen_falls_back_to_the_players_own_button(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = _player_browser([False, False, True])  # API request fails; the button works
    browser._frame_eval = Mock(return_value=[{"x": 850.0, "y": 553.0}])
    with patch("browser_harness.helpers.cdp"):
        assert browser.enter_fullscreen() is True
    clicks = [
        c.kwargs for c in browser.call.call_args_list if c.args[0] == "Input.dispatchMouseEvent"
    ]
    assert clicks and clicks[-1]["x"] == 850.0 and clicks[-1]["y"] == 84.0 + 553.0


def test_enter_fullscreen_reports_false_when_nothing_worked(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = _player_browser([False] * 10)
    browser._is_fullscreen = Mock(return_value=False)
    browser._frame_eval = Mock(return_value=[None])
    with patch("browser_harness.helpers.cdp"):
        assert browser.enter_fullscreen() is False
    browser._resume_if_paused.assert_not_called()


def test_enter_fullscreen_waits_when_the_player_is_not_there_yet():
    browser = _player_browser([False])
    browser._probe_player = Mock(return_value=None)
    with patch("browser_harness.helpers.cdp"):
        assert browser.enter_fullscreen() is False


def test_already_fullscreen_is_success_without_doing_anything():
    browser = _player_browser([True])
    assert browser.enter_fullscreen() is True
    browser.call.assert_not_called()
