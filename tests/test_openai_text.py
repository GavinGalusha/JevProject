import os
from unittest.mock import patch

from jev_remote.cli import configure_text_model
from jev_remote.openai_text import field_text


def test_openai_key_is_mapped_to_jev_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("TEXT_MODEL", raising=False)
    configure_text_model()
    assert os.environ["TEXT_MODEL_API_KEY"] == "secret"
    assert os.environ["TEXT_MODEL_BASE_URL"] == "https://api.openai.com/v1"
    assert os.environ["TEXT_MODEL"] == "gpt-5-nano"


def test_openai_helper_uses_low_cost_chat_parameters(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL", "gpt-5-nano")
    response = {
        "choices": [{"message": {"content": '{"text":"How I Met Your Mother"}'}}],
        "usage": {"total_tokens": 24},
    }
    with patch("jev_remote.openai_text._post", return_value=response) as post:
        value, details = field_text({"goal": "Find the show"})

    body = post.call_args.args[0]
    assert body["model"] == "gpt-5-nano"
    assert body["reasoning_effort"] == "minimal"
    assert body["response_format"] == {"type": "json_object"}
    assert value == "How I Met Your Mother"
    assert details["usage"]["total_tokens"] == 24
