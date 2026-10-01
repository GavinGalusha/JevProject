import time
from unittest.mock import Mock

from test_controller import BlockingAgent, FinishingAgent, wait_for_state

from jev_remote.commands import parse_command
from jev_remote.controller import RemoteController


def _controller(factory, **kwargs):
    return RemoteController("https://start.example/", agent_factory=factory, **kwargs)


def _run(controller, text="find something", **kwargs):
    controller.submit(parse_command(text), **kwargs)
    return wait_for_state(controller, "done", timeout=3.0)


def test_chrome_is_checked_before_the_tab_is_opened_for_every_command():
    order = []
    ensurer = Mock(side_effect=lambda: order.append("chrome") or "Chrome already running")

    def factory(url, goal):
        order.append("agent")
        return FinishingAgent()

    controller = _controller(factory, chrome_ensurer=ensurer)
    _run(controller, "first")
    _run(controller, "second")

    assert order == ["chrome", "agent", "chrome", "agent"]


def test_a_closed_chrome_is_reopened_and_the_command_still_runs():
    ensurer = Mock(return_value="Launched Chrome (profile /x) at http://127.0.0.1:9222")
    controller = _controller(lambda url, goal: FinishingAgent(), chrome_ensurer=ensurer)

    status = _run(controller)

    assert status["message"] == "Command completed"
    ensurer.assert_called_once()


def test_if_chrome_cannot_be_reopened_the_error_says_so_and_the_remote_stays_usable():
    ensurer = Mock(side_effect=RuntimeError("Cannot auto-launch Chrome"))
    controller = _controller(lambda url, goal: FinishingAgent(), chrome_ensurer=ensurer)

    controller.submit(parse_command("find something"))
    status = wait_for_state(controller, "error", timeout=3.0)
    assert "Chrome could not be reopened" in status["message"]

    ensurer.side_effect = None
    ensurer.return_value = "Chrome already running"
    assert _run(controller, "try again")["message"] == "Command completed"


def test_test_doubles_never_launch_a_real_chrome():
    controller = _controller(lambda url, goal: FinishingAgent())
    assert controller.chrome_ensurer() is None


def test_each_new_command_starts_from_the_start_page_not_the_last_page(monkeypatch):
    monkeypatch.delenv("JEV_CONTINUE_FROM_CURRENT_PAGE", raising=False)
    urls = []

    def factory(url, goal):
        urls.append(url)
        return FinishingAgent()

    controller = _controller(factory)
    _run(controller, "first")  # FakeBrowser reports https://example.com as its current page
    _run(controller, "second")
    _run(controller, "third", start_url="https://other.example/")

    assert urls == ["https://start.example/", "https://start.example/", "https://other.example/"]


def test_continuing_from_the_current_page_is_still_available(monkeypatch):
    monkeypatch.setenv("JEV_CONTINUE_FROM_CURRENT_PAGE", "1")
    urls = []

    def factory(url, goal):
        urls.append(url)
        return FinishingAgent()

    controller = _controller(factory)
    _run(controller, "first")
    _run(controller, "second")

    assert urls == ["https://start.example/", "https://example.com"]


def test_the_old_tab_is_retired_as_soon_as_a_new_command_starts():
    first = FinishingAgent()
    second = BlockingAgent()
    agents = iter([first, second])
    controller = _controller(lambda url, goal: next(agents))

    _run(controller, "first")
    assert first.closed is False  # kept after a finished command so it can be reviewed

    controller.submit(parse_command("second"))
    deadline = time.monotonic() + 2
    while not first.closed and time.monotonic() < deadline:
        time.sleep(0.01)

    assert first.closed is True  # closed while the second command is still running
    assert controller.status()["state"] == "working"
    second.release.set()
    wait_for_state(controller, "done", timeout=3.0)


def test_after_an_unsuccessful_command_the_next_one_runs_normally():
    agents = iter([FinishingAgent(final="blocked"), FinishingAgent(final="done")])
    controller = _controller(lambda url, goal: next(agents))

    controller.submit(parse_command("something that cannot work"))
    failed = wait_for_state(controller, "error", timeout=3.0)
    assert "could not make progress" in failed["message"]

    assert _run(controller, "something that can")["message"] == "Command completed"


def test_after_a_failed_start_the_next_command_runs_normally():
    calls = {"n": 0}

    def factory(url, goal):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("browser exploded")
        return FinishingAgent()

    controller = _controller(factory)
    controller.submit(parse_command("first"))
    wait_for_state(controller, "error", timeout=3.0)

    assert _run(controller, "second")["message"] == "Command completed"


class RecordingAgent(FinishingAgent):
    def __init__(self, log, name):
        super().__init__()
        self.log, self.name = log, name

    def close(self):
        self.log.append(f"close {self.name}")
        super().close()


def test_every_command_starts_clean_in_a_fixed_order(monkeypatch):
    monkeypatch.delenv("JEV_REFRESH_PER_COMMAND", raising=False)
    log = []
    agents = iter([RecordingAgent(log, "old"), RecordingAgent(log, "new")])

    def factory(url, goal):
        log.append("open tab")
        return next(agents)

    controller = _controller(
        factory,
        chrome_ensurer=lambda: log.append("chrome") or "Chrome already running",
        connection_reset=lambda: log.append("reset connection"),
    )
    _run(controller, "first")
    log.clear()
    _run(controller, "second")

    # the previous tab (video, fullscreen) goes first, then a fresh connection, then Chrome
    assert log == ["close old", "reset connection", "chrome", "open tab"]


def test_a_stale_browser_connection_is_refreshed_and_the_command_retried_once():
    attempts = {"n": 0}
    reset = Mock()

    def factory(url, goal):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")
        return FinishingAgent()

    controller = _controller(factory, connection_reset=reset)

    assert _run(controller)["message"] == "Command completed"
    assert attempts["n"] == 2
    assert reset.call_count == 2  # once at the start of the command, once for the retry


def test_other_errors_are_not_retried():
    attempts = {"n": 0}

    def factory(url, goal):
        attempts["n"] += 1
        raise ValueError("bad start url")

    controller = _controller(factory)
    controller.submit(parse_command("find something"))
    wait_for_state(controller, "error", timeout=3.0)

    assert attempts["n"] == 1


def test_the_per_command_refresh_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_REFRESH_PER_COMMAND", "0")
    reset = Mock()
    controller = _controller(lambda url, goal: FinishingAgent(), connection_reset=reset)
    _run(controller, "first")
    _run(controller, "second")
    reset.assert_not_called()


def test_a_tab_that_is_already_gone_does_not_raise_a_warning(capsys):
    class GoneAgent(FinishingAgent):
        def close(self):
            raise RuntimeError("{'code': -32602, 'message': 'No target with given id found'}")

    RemoteController._close_agent(GoneAgent(), "previous browser tab")
    out = capsys.readouterr().out
    assert "already closed" in out and "Could not close" not in out
