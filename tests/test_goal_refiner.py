import json
from unittest.mock import Mock, patch

from jev_remote.goal_refiner import refine_goal


def test_refine_goal_uses_strict_schema_and_returns_bounded_plan(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.delenv("JEV_GUIDED_MODEL", raising=False)
    response = Mock()
    response.status_code = 200
    response.is_error = False
    response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "refined_goal": "Find and open the requested result",
                            "requirements": ["Match the requested title"],
                            "success_criteria": ["The result page is open"],
                            "constraints": ["Do not create an account"],
                            "needs_clarification": False,
                            "clarifying_question": None,
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 20},
    }

    with patch("jev_remote.goal_refiner.CLIENT.post", return_value=response) as post:
        plan = refine_goal("Find Apollo 11", "https://example.com")

    body = post.call_args.kwargs["json"]
    assert body["model"] == "gpt-5-mini"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert plan.refined_goal == "Find and open the requested result"
    assert plan.requirements == ("Match the requested title",)
    assert "Original user request: Find Apollo 11" in plan.execution_goal("Find Apollo 11")
