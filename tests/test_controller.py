from jev_remote.commands import parse_command
from jev_remote.controller import RemoteController


class FakeBrowser:
    target = "target-1"

    def evaluate(self, _expression):
        return "https://example.com"


class FakeAgent:
    def __init__(self):
        self.browser = FakeBrowser()
        self.closed = False

    def close(self):
        self.closed = True


def test_kill_closes_owned_tab_and_blocks_commands():
    controller = RemoteController("https://example.com")
    agent = FakeAgent()
    controller._agent = agent

    result = controller.kill()

    assert result["state"] == "stopped"
    assert result["armed"] is False
    assert agent.closed is True

    try:
        controller.submit(parse_command("pause"))
    except RuntimeError as exc:
        assert "stopped and locked" in str(exc)
    else:
        raise AssertionError("Locked controller accepted a command")


def test_rearm_allows_commands_again():
    controller = RemoteController("https://example.com")
    controller.kill()
    result = controller.arm()
    assert result["state"] == "idle"
    assert result["armed"] is True
