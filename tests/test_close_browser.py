import io
import json
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_controller import BlockingAgent, FinishingAgent, wait_for_state

from jev_remote import chrome, cli
from jev_remote.app import create_app
from jev_remote.commands import parse_command
from jev_remote.config import Settings
from jev_remote.controller import RemoteController

TOKEN = "a" * 32


# ---------- supervisor
class FakeChild:
    def __init__(self, code):
        self.code = code
        self.terminated = False
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if isinstance(self.code, BaseException) and self.waits == 1:
            raise self.code  # Ctrl+C arrives while waiting; the cleanup wait then succeeds
        return 0 if isinstance(self.code, BaseException) else self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass


def _spawner(monkeypatch, codes):
    spawned = []
    children = iter(FakeChild(c) for c in codes)

    def popen(command, env=None, **_kwargs):
        spawned.append((command, env))
        return next(children)

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return spawned


def test_supervisor_restarts_the_server_while_it_asks_for_a_restart(monkeypatch):
    spawned = _spawner(monkeypatch, [cli.RESTART_EXIT_CODE, cli.RESTART_EXIT_CODE, 0])

    with pytest.raises(SystemExit) as stopped:
        cli.supervise(["--http"])

    assert stopped.value.code == 0
    assert len(spawned) == 3
    assert spawned[0][0][-2:] == ["jev_remote.cli", "--http"]
    assert [env.get("JEV_RESTARTED") for _, env in spawned] == [None, "1", "1"]
    assert all(env["JEV_SUPERVISED"] == "1" for _, env in spawned)


def test_supervisor_does_not_loop_on_a_crash(monkeypatch):
    spawned = _spawner(monkeypatch, [2])
    with pytest.raises(SystemExit) as stopped:
        cli.supervise([])
    assert stopped.value.code == 2 and len(spawned) == 1


def test_ctrl_c_stops_the_child_and_exits(monkeypatch):
    child = FakeChild(KeyboardInterrupt())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: child)
    with pytest.raises(SystemExit) as stopped:
        cli.supervise([])
    assert child.terminated is True and stopped.value.code == 130


def test_main_supervises_unless_it_is_already_the_supervised_child(monkeypatch):
    supervise, serve = Mock(), Mock()
    monkeypatch.setattr(cli, "supervise", supervise)
    monkeypatch.setattr(cli, "serve", serve)

    monkeypatch.delenv("JEV_SUPERVISED", raising=False)
    cli.main(["--http"])
    supervise.assert_called_once_with(["--http"])
    serve.assert_not_called()

    monkeypatch.setenv("JEV_SUPERVISED", "1")
    cli.main(["--http"])
    serve.assert_called_once_with(["--http"])


# ---------- API endpoint
class FakeController:
    def __init__(self):
        self.closed_browser = 0

    def status(self):
        return {"state": "idle", "message": "Ready"}

    def close(self):
        pass

    def close_browser(self):
        self.closed_browser += 1
        return {"state": "idle", "message": "Browser closed. Your next command reopens it."}


def _client(restart=None):
    controller = FakeController()
    app = create_app(Settings(token=TOKEN), controller)
    app.state.request_restart = restart
    return TestClient(app), controller


def test_close_browser_requires_the_token():
    client, controller = _client()
    assert client.post("/api/close-browser").status_code == 401
    assert controller.closed_browser == 0


def test_close_browser_restarts_the_server_when_a_supervisor_is_present(monkeypatch):
    monkeypatch.delenv("JEV_RESTART_ON_CLOSE", raising=False)
    restart = Mock()
    client, controller = _client(restart)
    response = client.post("/api/close-browser", headers={"Authorization": f"Bearer {TOKEN}"})
    body = response.json()
    assert response.status_code == 200 and controller.closed_browser == 1
    assert body["restarting"] is True and "Restarting" in body["message"]
    restart.assert_called_once()


def test_close_browser_alone_when_there_is_no_supervisor():
    client, _ = _client(restart=None)
    body = client.post("/api/close-browser", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert "restarting" not in body and "reopens" in body["message"]


def test_the_restart_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_RESTART_ON_CLOSE", "0")
    restart = Mock()
    client, _ = _client(restart)
    body = client.post("/api/close-browser", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    restart.assert_not_called() and "restarting" not in body


# ---------- controller
def _controller(closer, factory=lambda url, goal: FinishingAgent()):
    return RemoteController("https://start.example/", agent_factory=factory, browser_closer=closer)


def test_close_browser_quits_chrome_and_leaves_the_remote_armed():
    closer = Mock(return_value=True)
    controller = _controller(closer)
    controller.submit(parse_command("find something"))
    wait_for_state(controller, "done", timeout=3.0)
    agent = controller._agent

    status = controller.close_browser()

    closer.assert_called_once()
    assert agent.closed is True and controller._agent is None
    assert status["state"] == "idle" and status["armed"] is True
    assert "reopens" in status["message"]
    # and the remote still works afterwards
    controller.submit(parse_command("find something else"))
    assert wait_for_state(controller, "done", timeout=3.0)["message"] == "Command completed"


def test_close_browser_cancels_a_command_in_progress():
    agent = BlockingAgent()
    controller = _controller(Mock(return_value=True), factory=lambda url, goal: agent)
    controller.submit(parse_command("something slow"))
    wait_for_state(controller, "working", timeout=3.0)

    status = controller.close_browser()

    deadline = time.monotonic() + 2  # the cancelled job closes its own tab a moment later
    while not agent.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert agent.closed is True
    assert status["state"] == "idle" and controller._active_job_id is None


def test_close_browser_reports_when_nothing_was_open_and_when_it_fails():
    status = _controller(Mock(return_value=False)).close_browser()
    assert status["message"] == "No browser was open."

    failing = _controller(Mock(side_effect=RuntimeError("Chrome did not quit in time")))
    status = failing.close_browser()
    assert status["state"] == "error" and "did not quit" in status["message"]


def test_a_locked_remote_stays_locked_after_closing_the_browser():
    controller = _controller(Mock(return_value=True))
    controller.kill()
    status = controller.close_browser()
    assert status["state"] == "stopped" and status["armed"] is False


# ---------- quit_chrome
def test_quit_chrome_refuses_a_browser_that_is_not_on_this_pc():
    with pytest.raises(RuntimeError, match="not on this PC"):
        chrome.quit_chrome("http://10.0.0.5:9222")


def test_quit_chrome_returns_false_when_no_chrome_is_running(monkeypatch):
    monkeypatch.setattr(chrome, "cdp_alive", lambda url: False)
    assert chrome.quit_chrome("http://127.0.0.1:9222") is False


def test_quit_chrome_sends_browser_close_and_waits_for_the_port_to_go(monkeypatch):
    alive = iter([True, True, False])  # running, still up once after the request, then gone
    monkeypatch.setattr(chrome, "cdp_alive", lambda url: next(alive))
    monkeypatch.setattr(chrome.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        chrome.urllib.request,
        "urlopen",
        lambda *a, **k: io.StringIO(json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"})),
    )
    sent = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, timeout=None):
            return "{}"

    import websockets.sync.client as websocket_client

    monkeypatch.setattr(websocket_client, "connect", lambda url, **k: FakeSocket())

    assert chrome.quit_chrome("http://127.0.0.1:9222") is True
    assert sent == [{"id": 1, "method": "Browser.close"}]
