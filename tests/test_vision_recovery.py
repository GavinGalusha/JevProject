import json
from unittest.mock import Mock, patch

from jev_remote.vision_recovery import analyze_screenshot


def test_vision_recovery_sends_low_detail_image_and_returns_bounded_advice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("JEV_VISION_MODEL", "gpt-5-nano")
    response = Mock(status_code=200, is_error=False)
    response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "guidance": "Try the visible second result.",
                            "explanation": "The first result left the page unchanged.",
                        }
                    )
                }
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }

    with patch("jev_remote.vision_recovery.CLIENT.post", return_value=response) as post:
        advice = analyze_screenshot(
            "Find the show",
            "jpeg-base64",
            {"actions": [{"kind": "click", "label": "Second result"}]},
            [],
        )

    body = post.call_args.kwargs["json"]
    image = body["messages"][1]["content"][1]["image_url"]
    assert body["model"] == "gpt-5-nano"
    assert image["detail"] == "low"
    assert image["url"] == "data:image/jpeg;base64,jpeg-base64"
    assert advice.guidance == "Try the visible second result."
    assert advice.usage["input_tokens"] == 20
