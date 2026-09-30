import pytest

from jev_remote.commands import parse_command


@pytest.mark.parametrize(
    ("text", "action", "amount"),
    [
        ("pause", "pause", None),
        ("PLAY", "play", None),
        ("volume up", "volume", 0.1),
        ("quieter", "volume", -0.1),
        ("forward", "seek", 30.0),
        ("back 15 seconds", "seek", -15.0),
        ("fullscreen", "fullscreen", None),
    ],
)
def test_media_commands(text, action, amount):
    parsed = parse_command(text)
    assert parsed.kind == "media"
    assert parsed.action == action
    assert parsed.amount == amount


def test_complex_play_request_is_not_mistaken_for_media_play():
    parsed = parse_command("Play How I Met Your Mother season 3 episode 5")
    assert parsed.kind == "jev"
    assert parsed.text.startswith("Play How")


def test_empty_command_is_rejected():
    with pytest.raises(ValueError):
        parse_command("   ")
