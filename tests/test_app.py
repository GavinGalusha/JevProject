from fastapi.testclient import TestClient

from jev_remote.app import create_app
from jev_remote.config import Settings


class FakeController:
    def __init__(self):
        self.received = []

    def status(self):
        return {"state": "idle", "message": "Ready"}

    def submit(self, command):
        self.received.append(command)
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
    assert controller.received[0].action == "seek"
    assert controller.received[0].amount == -10


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
