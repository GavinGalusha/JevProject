import threading
import time

from jev_remote.commands import parse_command
from jev_remote.controller import RemoteController
from jev_remote.goal_refiner import GoalPlan
from jev_remote.vision_recovery import RecoveryAdvice


class FakeBrowser:
    target = "target-1"

    def evaluate(self, _expression):
        return "https://example.com"

    def call(self, method, **_params):
        assert method == "Page.captureScreenshot"
        return {"data": "jpeg-base64"}


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


def wait_for_state(controller, expected, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = controller.status()
        if status["state"] == expected:
            return status
        time.sleep(0.01)
    raise AssertionError(f"Controller never reached {expected!r}: {controller.status()}")


class BlockingAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self):
        self.release.wait(2)
        yield {"status": "done", "history": [], "decisions": [], "text_calls": []}

    def close(self):
        super().close()
        self.release.set()


class FinishingAgent(FakeAgent):
    def __init__(self, final="done"):
        super().__init__()
        self.final = final

    def run(self):
        yield {"status": self.final, "history": [], "decisions": [], "text_calls": []}


class StaleThenDoneAgent(FakeAgent):
    def run(self):
        decisions = []
        page = {"url": "https://example.com/search", "title": "Search"}
        for index in range(3):
            decisions.append(
                {
                    "operation": "CLICK",
                    "target": str(index),
                    "choice": index,
                    "model": "jev-latest",
                    "latency_ms": 10,
                    "confidence": 0.8,
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                }
            )
            yield {
                "status": "ready",
                "page": page,
                "history": [],
                "decisions": list(decisions),
                "text_calls": [],
            }
        decisions.append(
            {
                "operation": "DONE",
                "choice": "DONE",
                "model": "jev-latest",
                "latency_ms": 10,
                "confidence": 0.9,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            }
        )
        yield {
            "status": "done",
            "page": page,
            "history": [],
            "decisions": list(decisions),
            "text_calls": [],
        }


def test_watchdog_reports_stall_closes_agent_and_releases_remote():
    agent = BlockingAgent()
    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda _url, _goal: agent,
        command_timeout=1,
        stall_timeout=0.03,
        watchdog_interval=0.005,
    )

    controller.submit(parse_command("Find Apollo 11"))
    status = wait_for_state(controller, "error")

    assert status["error_code"] == "timeout"
    assert "without progress" in status["message"]
    assert agent.closed is True
    replacement = FinishingAgent()
    controller.agent_factory = lambda _url, _goal: replacement
    controller.submit(parse_command("Find Apollo 12"))
    assert wait_for_state(controller, "done")["command"] == "Find Apollo 12"


def test_duplicate_submission_does_not_start_a_second_agent():
    agent = BlockingAgent()
    created = 0
    factory_called = threading.Event()

    def factory(_url, _goal):
        nonlocal created
        created += 1
        factory_called.set()
        return agent

    controller = RemoteController("https://example.com", agent_factory=factory)
    first = controller.submit(parse_command("Find Apollo 11"))
    assert factory_called.wait(1)
    duplicate = controller.submit(parse_command("  find apollo 11  "))

    assert duplicate["duplicate"] is True
    assert duplicate["job_id"] == first["job_id"]
    assert created == 1
    controller.kill()


def test_blocked_agent_is_reported_as_error_not_success():
    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda _url, _goal: FinishingAgent("blocked"),
    )

    controller.submit(parse_command("Find something unavailable"))
    status = wait_for_state(controller, "error")

    assert "could not make progress" in status["message"]
    assert controller._running_agent is None


def test_discarded_predictions_do_not_consume_completed_action_limit(capsys):
    from jev_remote.budget import RequestBudget

    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda _url, _goal: StaleThenDoneAgent(),
        budget=RequestBudget(
            max_steps_per_command=2,
            max_jev_per_hour=0,
            max_openai_per_hour=0,
        ),
        max_stale_decisions=6,
    )

    controller.submit(parse_command("Find it"))
    status = wait_for_state(controller, "done")

    output = capsys.readouterr().out
    assert "prediction not executed" in output
    assert "model  4  DONE" in output
    assert status["state"] == "done"


def test_stale_prediction_loop_stops_with_specific_error():
    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda _url, _goal: StaleThenDoneAgent(),
        max_stale_decisions=2,
    )

    controller.submit(parse_command("Find it"))
    status = wait_for_state(controller, "error")

    assert "2 consecutive predictions" in status["message"]


class RecoveringAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.runs = 0
        self.state = {"goal": "Find it", "status": "ready", "decision": None}

    def run(self):
        self.runs += 1
        final = "blocked" if self.runs == 1 else "done"
        self.state["status"] = final
        yield {
            "status": final,
            "page": {
                "url": "https://example.com/results",
                "title": "Results",
                "text": "Try another result",
                "actions": [{"kind": "click", "label": "Second result"}],
            },
            "history": [],
            "decisions": [{}] * self.runs,
            "text_calls": [],
        }


def test_vision_recovery_resumes_blocked_agent_once():
    agent = RecoveringAgent()
    calls = []

    def analyze(goal, screenshot, page, history):
        calls.append((goal, screenshot, page, history))
        return RecoveryAdvice(
            guidance="Try the visible Second result control instead of repeating the first result.",
            explanation="The first result did not change the page.",
            model="gpt-5-nano",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda _url, _goal: agent,
        vision_analyzer=analyze,
    )

    controller.submit(parse_command("Find it"), vision_recovery=True)
    status = wait_for_state(controller, "done")

    assert agent.runs == 2
    assert len(calls) == 1
    assert calls[0][1] == "jpeg-base64"
    assert "Second result control" in agent.state["goal"]
    assert status["vision_recoveries"] == 1


def test_guided_mode_refines_goal_before_agent_starts(capsys):
    received = []
    plan = GoalPlan(
        refined_goal="Find and open the requested result",
        requirements=("Use the supplied title",),
        success_criteria=("The requested result is open",),
        constraints=("Do not sign up",),
        needs_clarification=False,
        clarifying_question=None,
        model="gpt-5-mini",
        usage={"input_tokens": 10, "output_tokens": 5},
    )

    def factory(_url, goal):
        received.append(goal)
        return FinishingAgent()

    controller = RemoteController(
        "https://example.com",
        agent_factory=factory,
        goal_refiner=lambda goal, url: plan,
    )
    controller.submit(parse_command("Find Apollo 11"), guided=True)
    status = wait_for_state(controller, "done")

    assert "Original user request: Find Apollo 11" in received[0]
    assert "Success criteria:" in received[0]
    assert status["guided_plans"] == 1
    output = capsys.readouterr().out
    assert "GUIDED PLAN (gpt-5-mini)" in output
    assert "objective: Find and open the requested result" in output
    assert "success criteria:" in output


def test_guided_mode_stops_for_clarification_before_opening_tab():
    plan = GoalPlan(
        refined_goal="Open a result",
        requirements=(),
        success_criteria=(),
        constraints=(),
        needs_clarification=True,
        clarifying_question="Which result should I open?",
        model="gpt-5-mini",
        usage={},
    )
    created = []
    controller = RemoteController(
        "https://example.com",
        agent_factory=lambda url, goal: created.append((url, goal)),
        goal_refiner=lambda goal, url: plan,
    )

    controller.submit(parse_command("Open it"), guided=True)
    status = wait_for_state(controller, "error")

    assert created == []
    assert "Which result should I open?" in status["message"]
