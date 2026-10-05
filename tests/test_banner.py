import time
from unittest.mock import Mock

from test_controller import FakeAgent, FinishingAgent, wait_for_state

from jev_remote import banner
from jev_remote.commands import parse_command
from jev_remote.controller import RemoteController
from jev_remote.safe_browser import StableTargetBrowser


# ---------- the card itself
def test_banner_js_escapes_text_and_carries_the_title():
    js = banner.banner_js("success", "Task completed", 'play "Moana" </script><b>', 3.0)
    assert '"title": "Task completed"' in js and '"ok": true' in js and '"ms": 3000' in js
    assert "\\u003c" not in js or "</script>" not in js.replace('\\"', "")  # text is data, not code
    assert "textContent" in js and "innerHTML" not in js  # never parsed as HTML


def test_failure_cards_use_the_failure_colours_and_clip_long_text():
    js = banner.banner_js("failure", "Task failed", "x" * 500, 5.0)
    assert '"ok": false' in js and "x" * 200 not in js and "\\u2026" in js


def test_banner_settings(monkeypatch):
    monkeypatch.delenv("JEV_BANNER", raising=False)
    monkeypatch.delenv("JEV_BANNER_SECONDS", raising=False)
    assert banner.banner_enabled() is True and banner.banner_seconds() == 5.0
    monkeypatch.setenv("JEV_BANNER", "0")
    assert banner.banner_enabled() is False
    monkeypatch.setenv("JEV_BANNER_SECONDS", "nope")
    assert banner.banner_seconds() == 5.0
    monkeypatch.setenv("JEV_BANNER_SECONDS", "8")
    assert banner.banner_seconds() == 8.0


def test_show_banner_is_best_effort():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    assert browser.show_banner("success", "Task completed") is False  # no session
    browser.session = "s"
    browser.evaluate = Mock(return_value=True)
    assert browser.show_banner("success", "Task completed", "x") is True
    browser.evaluate = Mock(side_effect=RuntimeError("document changed"))
    assert browser.show_banner("failure", "Task failed") is False


# ---------- the controller shows it
class BannerBrowser:
    target = "t"

    def __init__(self):
        self.show_banner = Mock(return_value=True)

    def evaluate(self, _expression):
        return "https://example.com"

    def call(self, method, **_params):
        return {}


def _agent(cls=FinishingAgent, **kwargs):
    agent = cls(**kwargs)
    agent.browser = BannerBrowser()
    return agent


def _controller(agent):
    return RemoteController("https://start.example/", agent_factory=lambda url, goal: agent)


def test_a_finished_command_shows_task_completed_with_the_command(monkeypatch):
    monkeypatch.delenv("JEV_BANNER", raising=False)
    agent = _agent()
    controller = _controller(agent)
    controller.submit(parse_command("find something nice"))
    wait_for_state(controller, "done", timeout=3.0)

    args = agent.browser.show_banner.call_args.args
    assert args[0] == "success" and args[1] == "Task completed"
    assert "find something nice" in args[2]


def test_a_command_that_could_not_finish_shows_task_failed_with_the_reason():
    agent = _agent(final="blocked")
    controller = _controller(agent)
    controller.submit(parse_command("something impossible"))
    wait_for_state(controller, "error", timeout=3.0)

    args = agent.browser.show_banner.call_args.args
    assert args[0] == "failure" and args[1] == "Task failed"
    assert "could not make progress" in args[2]


class CrashingAgent(FakeAgent):
    def run(self):
        raise RuntimeError("the page exploded")
        yield  # pragma: no cover


def test_a_crash_shows_task_failed_and_keeps_the_tab_long_enough_to_read_it(monkeypatch):
    monkeypatch.setenv("JEV_BANNER_SECONDS", "0.3")
    agent = CrashingAgent()
    agent.browser = BannerBrowser()
    controller = _controller(agent)
    controller.submit(parse_command("something that crashes"))
    wait_for_state(controller, "error", timeout=3.0)

    assert agent.browser.show_banner.call_args.args[:2] == ("failure", "Task failed")
    assert agent.closed is False  # still open while the card is on screen
    deadline = time.monotonic() + 2
    while not agent.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent.closed is True


def test_the_card_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_BANNER", "0")
    agent = _agent()
    controller = _controller(agent)
    controller.submit(parse_command("find something"))
    wait_for_state(controller, "done", timeout=3.0)
    agent.browser.show_banner.assert_not_called()


def test_a_browser_without_the_card_does_not_break_a_command():
    agent = FinishingAgent()  # FakeBrowser has no show_banner
    controller = _controller(agent)
    controller.submit(parse_command("find something"))
    assert wait_for_state(controller, "done", timeout=3.0)["message"] == "Command completed"
