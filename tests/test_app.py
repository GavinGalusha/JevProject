from fastapi.testclient import TestClient

from jev_remote.app import create_app
from jev_remote.config import Settings


class FakeController:
    def __init__(self):
        self.received = []

    def status(self):
        return {"state": "idle", "message": "Ready"}

    def submit(
        self,
        command,
        start_url=None,
        then_fullscreen=False,
        vision_recovery=False,
        guided=False,
    ):
        self.received.append((command, start_url, then_fullscreen, vision_recovery, guided))
        return {"state": "working", "message": "Accepted"}

    def close(self):
        pass

    def kill(self):
        return {"state": "stopped", "message": "STOPPED", "armed": False}

    def arm(self):
        return {"state": "idle", "message": "Ready", "armed": True}


TOKEN = "a" * 32


def test_command_requires_token():
    client = TestClient(create_app(Settings(token=TOKEN), FakeController()))
    assert client.post("/api/command", json={"text": "pause"}).status_code == 401


def test_command_is_parsed_and_submitted():
    controller = FakeController()
    client = TestClient(create_app(Settings(token=TOKEN), controller))
    response = client.post(
        "/api/command",
        json={"text": "back 10 seconds"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert controller.received[0][0].action == "seek"
    assert controller.received[0][0].amount == -10


def test_public_network_peer_is_rejected():
    client = TestClient(
        create_app(Settings(token=TOKEN), FakeController()),
        client=("8.8.8.8", 50000),
    )
    assert client.get("/api/health").status_code == 403


def test_kill_switch_requires_authentication_and_locks_remote():
    client = TestClient(create_app(Settings(token=TOKEN), FakeController()))
    assert client.post("/api/kill").status_code == 401
    response = client.post("/api/kill", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["armed"] is False


def test_pairing_code_trades_for_token_once():
    app = create_app(Settings(token=TOKEN), FakeController())
    client = TestClient(app)
    code = app.state.pairing.display_code.lower()
    assert client.post("/api/pair", json={"code": "WRONG123"}).status_code == 403
    assert client.post("/api/pair", json={"code": code}).json() == {"token": TOKEN}
    assert client.post("/api/pair", json={"code": code}).status_code == 403


def test_pairing_locks_after_repeated_failures():
    app = create_app(Settings(token=TOKEN), FakeController())
    client = TestClient(app)
    code = app.state.pairing.display_code
    for _ in range(5):
        assert client.post("/api/pair", json={"code": "WRONG123"}).status_code == 403
    assert client.post("/api/pair", json={"code": code}).status_code == 403


def test_saved_commands_roundtrip_and_auth(tmp_path):
    from jev_remote.saved import SavedCommands

    client = TestClient(
        create_app(Settings(token=TOKEN), FakeController(), SavedCommands(tmp_path / "s.json"))
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/saved").status_code == 401
    assert client.get("/api/saved", headers=headers).json() == []
    item = client.post(
        "/api/saved",
        headers=headers,
        json={"name": "Wootly", "text": "Play {show} on Wootly", "fullscreen": True,
              "start_url": "https://example.com/"},
    ).json()
    assert item["fullscreen"] is True
    assert client.get("/api/saved", headers=headers).json() == [item]
    bad = client.post(
        "/api/saved", headers=headers, json={"name": "x", "text": "y", "start_url": "file:///etc"}
    )
    assert bad.status_code == 422
    assert client.delete(f"/api/saved/{item['id']}", headers=headers).status_code == 200
    assert client.delete(f"/api/saved/{item['id']}", headers=headers).status_code == 404


def test_command_passes_start_url_and_fullscreen():
    controller = FakeController()
    client = TestClient(create_app(Settings(token=TOKEN), controller))
    client.post(
        "/api/command",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": "Play a show", "start_url": "https://example.com/", "fullscreen": True},
    )
    assert controller.received[0][1:] == ("https://example.com/", True, False, False)


def test_command_passes_vision_recovery_mode():
    controller = FakeController()
    client = TestClient(create_app(Settings(token=TOKEN), controller))
    client.post(
        "/api/command",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": "Play a show", "vision_recovery": True},
    )
    assert controller.received[0][3] is True


def test_command_passes_guided_mode():
    controller = FakeController()
    client = TestClient(create_app(Settings(token=TOKEN), controller))
    client.post(
        "/api/command",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": "Play a show", "guided": True},
    )
    assert controller.received[0][4] is True
