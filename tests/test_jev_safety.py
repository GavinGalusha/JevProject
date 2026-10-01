import time
from unittest.mock import Mock, patch

import pytest
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import StalePage

from jev_remote.safe_browser import StableTargetBrowser

TARGET_GUARD = [
    7,
    "link",
    "Old page button",
    None,
    None,
    None,
    None,
    False,
    None,
    None,
    None,
    None,
    "https://destination.example/",
    "Volatile surrounding result text",
]

PAGE = {
    "fingerprint": "old-fingerprint",
    "page_key": [123.0, "https://old.example/", 0, 0, 1120, 780, []],
    "marker": "old-marker",
    "guards": {"7": TARGET_GUARD},
    "actions": [
        {
            "id": 42,
            "node": 7,
            "kind": "click",
            "label": "Old page button",
        }
    ],
    "url": "https://old.example/",
    "title": "Old page",
    "text": "Old page button",
}


class ChangedBrowser:
    def __init__(self):
        self.act_calls = 0
        self.observe_calls = 0

    def fresh(self, _page, _action=None):
        return True

    def act(self, _action, _page, text=None):
        self.act_calls += 1
        raise StalePage("Page changed since this decision. Observe again.")

    def observe(self, screenshot=False):
        self.observe_calls += 1
        return {
            **PAGE,
            "fingerprint": "new-fingerprint",
            "page_key": [456.0, "https://new.example/", 0, 0, 1120, 780, []],
            "marker": "new-marker",
            "guards": {"8": "new-target"},
            "actions": [],
            "url": "https://new.example/",
            "title": "New page",
            "text": "New page",
        }


def make_agent(browser):
    agent = Agent.__new__(Agent)
    agent.pending_text = None
    agent.screenshots = False
    agent.record_dir = None
    agent.browser = browser
    agent.state = {
        "browser": browser,
        "goal": "Click the current page button",
        "page": dict(PAGE),
        "decision": None,
        "history": [],
        "status": "ready",
        "plan": ["Click the current page button"],
        "plan_index": 0,
        "decisions": [],
        "text_calls": [],
        "elapsed_ms": 0,
        "started_at": time.perf_counter(),
        "record": False,
    }
    return agent


def decision():
    return {
        "choice": 42,
        "operation": "CLICK",
        "target": "0",
        "confidence": 0.9,
        "probabilities": {42: 0.9},
        "latency_ms": 1,
        "usage": {},
    }


def test_unrelated_page_churn_does_not_invalidate_same_navigation_and_target():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    changed_context = [*TARGET_GUARD[:13], "Different surrounding ads and result text"]
    browser.evaluate = lambda _expression: [[123.0, "https://old.example/"], changed_context]

    assert browser.fresh(PAGE, PAGE["actions"][0]) is True


def test_browser_rejects_action_after_navigation():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.evaluate = lambda _expression: [[456.0, "https://new.example/"], TARGET_GUARD]

    assert browser.fresh(PAGE, PAGE["actions"][0]) is False
    with pytest.raises(StalePage, match="Page changed since this decision"):
        browser.act(PAGE["actions"][0], PAGE)


def test_browser_rejects_action_when_retained_target_guard_changes():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    changed_destination = [*TARGET_GUARD]
    changed_destination[12] = "https://wrong.example/"
    browser.evaluate = lambda _expression: [
        [123.0, "https://old.example/"],
        changed_destination,
    ]

    assert browser.fresh(PAGE, PAGE["actions"][0]) is False
    with pytest.raises(StalePage, match="Page changed since this decision"):
        browser.act(PAGE["actions"][0], PAGE)


def test_observe_removes_click_duplicates_for_editable_fields():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    observed = {
        **PAGE,
        "actions": [
            {"id": "e1", "node": 8, "kind": "fill", "label": "Search"},
            {"id": "e2", "node": 8, "kind": "click", "label": "Open Search"},
            {"id": "e3", "node": 9, "kind": "click", "label": "LinkedIn result"},
        ],
        "text": "Search LinkedIn result",
        "scroll": {"y": 0, "height": 780},
    }

    with patch("jev_remote.safe_browser.Browser.observe", return_value=observed):
        page = browser.observe(screenshot=False)

    assert [(action["kind"], action["label"]) for action in page["actions"]] == [
        ("fill", "Search"),
        ("click", "LinkedIn result"),
    ]


def test_observe_removes_google_apps_only_on_google_pages():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)

    def observed(url):
        return {
            **PAGE,
            "url": url,
            "actions": [
                {"id": "e1", "node": 1, "kind": "click", "label": "Google apps"},
                {"id": "e2", "node": 2, "kind": "click", "label": "LinkedIn result"},
            ],
            "text": "Google apps LinkedIn result",
            "scroll": {"y": 0, "height": 780},
        }

    with patch(
        "jev_remote.safe_browser.Browser.observe",
        return_value=observed("https://www.google.com/search?q=gavin"),
    ):
        google_page = browser.observe(screenshot=False)
    with patch(
        "jev_remote.safe_browser.Browser.observe",
        return_value=observed("https://example.com/"),
    ):
        other_page = browser.observe(screenshot=False)

    assert [action["label"] for action in google_page["actions"]] == ["LinkedIn result"]
    assert [action["label"] for action in other_page["actions"]] == [
        "Google apps",
        "LinkedIn result",
    ]


def test_observe_waits_for_two_stable_post_action_snapshots(monkeypatch):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.after_input = {"kind": "click"}
    monkeypatch.setenv("JEV_PAGE_SETTLE_TIMEOUT_MS", "50")
    monkeypatch.setenv("JEV_PAGE_SETTLE_QUIET_MS", "1")

    def observed(node):
        guard = [*TARGET_GUARD]
        guard[0] = node
        return {
            **PAGE,
            "actions": [
                {
                    "id": "e1",
                    "node": node,
                    "kind": "click",
                    "role": "link",
                    "label": "LinkedIn result",
                }
            ],
            "guards": {str(node): guard},
            "text": "LinkedIn result",
            "scroll": {"y": 0, "height": 780},
        }

    observations = [observed(7), observed(8), observed(8), observed(8)]
    with patch(
        "jev_remote.safe_browser.Browser.observe",
        side_effect=observations,
    ) as upstream_observe:
        page = browser.observe(screenshot=False)

    assert upstream_observe.call_count == 4
    assert page["actions"][0]["node"] == 8


def test_click_uses_a_hit_tested_interior_point_when_center_is_unavailable():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.fresh = Mock(return_value=True)
    browser.evaluate = Mock(
        side_effect=[
            {"ok": True, "x": 125, "y": 80, "position": [0.25, 0.5]},
            True,
        ]
    )
    browser.call = Mock()
    action = {"id": "e1", "node": 7, "kind": "click", "label": "LinkedIn result"}

    result = browser.act(action, PAGE)

    assert result == {"executed": "e1"}
    assert browser.call.call_count == 2
    assert browser.call.call_args_list[0].kwargs["x"] == 125
    assert browser.after_input == action


def test_click_refuses_when_no_point_belongs_to_selected_element():
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.fresh = Mock(return_value=True)
    browser.evaluate = Mock(
        return_value={
            "ok": False,
            "reason": "covered",
            "cover": {"tag": "DIV", "role": "dialog", "label": "Consent"},
        }
    )
    browser.call = Mock()
    action = {"id": "e1", "node": 7, "kind": "click", "label": "LinkedIn result"}

    with pytest.raises(StalePage, match="covered"):
        browser.act(action, PAGE)

    browser.call.assert_not_called()


def test_stale_action_is_consumed_without_history_or_second_input_attempt():
    browser = ChangedBrowser()
    agent = make_agent(browser)
    agent.state["decision"] = decision()
    agent.state["status"] = "predicted"

    with pytest.raises(StalePage):
        agent.command("act", {"fingerprint": "old-fingerprint"})

    assert browser.act_calls == 1
    assert agent.state["decision"] is None
    assert agent.state["history"] == []


def test_tick_discards_stale_decision_and_reobserves_before_next_prediction():
    browser = ChangedBrowser()
    agent = make_agent(browser)

    with patch("jev_ultrafast.agent.choose", return_value=decision()):
        snapshot = agent.command("tick")

    assert browser.act_calls == 1
    assert browser.observe_calls == 1
    assert snapshot["status"] == "ready"
    assert snapshot["decision"] is None
    assert snapshot["history"] == []
    assert snapshot["page"]["fingerprint"] == "new-fingerprint"
    assert snapshot["page"]["url"] == "https://new.example/"


def test_observe_waits_through_a_navigating_document_instead_of_failing(monkeypatch):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _seconds: None)
    outcomes = [StalePage("Document is navigating")] * 3 + [PAGE]

    def observe(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch("jev_remote.safe_browser.Browser.observe", side_effect=observe):
        page = browser.observe(screenshot=False)

    assert page["url"] == PAGE["url"]
    assert outcomes == []


def test_observe_gives_up_if_the_document_never_finishes_navigating(monkeypatch):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    monkeypatch.setenv("JEV_NAVIGATION_WAIT_MS", "30")

    with patch(
        "jev_remote.safe_browser.Browser.observe",
        side_effect=StalePage("Document is navigating"),
    ):
        with pytest.raises(StalePage):
            browser.observe(screenshot=False)
