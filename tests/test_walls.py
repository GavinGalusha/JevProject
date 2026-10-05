from unittest.mock import Mock

from jev_remote.safe_browser import StableTargetBrowser
from jev_remote.walls import (
    WALL_RULES,
    Avoid,
    guard_href,
    host_of,
    normalize_href,
    with_wall_rules,
)

BASE = "https://news.example/articles"


def _guard(href):
    return [1, "link", "x", None, None, None, None, False, None, None, None, None, href, ""]


def _page(extra=None):
    return {
        "url": BASE,
        "text": "Articles",
        "scroll": {"y": 0, "height": 900},
        "guards": {
            "1": _guard("/members"),
            "2": _guard("https://other.example/x"),
            "3": _guard("/ok"),
        },
        "actions": [
            {"id": "e1", "node": 1, "kind": "click", "role": "link", "label": "Members article"},
            {"id": "e2", "node": 2, "kind": "click", "role": "link", "label": "Other site"},
            {"id": "e3", "node": 3, "kind": "click", "role": "link", "label": "Public article"},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
        **(extra or {}),
    }


def test_normalize_href_makes_links_absolute_and_ignores_non_links():
    assert normalize_href("/members#top", BASE) == "https://news.example/members"
    assert normalize_href("https://a.example/p", BASE) == "https://a.example/p"
    for bad in (None, "", "#x", "javascript:void(0)", "mailto:a@b.c"):
        assert normalize_href(bad, BASE) is None
    assert host_of("https://News.Example:8080/x") == "news.example"


def test_guard_href_reads_the_destination_slot():
    assert guard_href(_page(), 1) == "https://news.example/members"
    assert guard_href(_page(), 99) is None


def test_the_link_that_led_to_a_wall_is_hidden_but_other_links_stay():
    avoid = Avoid()
    label = avoid.remember(
        {"label": "Members article", "href": "https://news.example/members", "from_url": BASE},
        "https://news.example/login",
    )
    page = _page()
    dropped = avoid.apply(page)

    assert label == "Members article" and dropped == ["Members article"]
    assert [a["label"] for a in page["actions"]] == [
        "Other site",
        "Public article",
        "Wait for the page to update",
    ]


def test_a_wall_on_another_site_hides_every_link_to_that_site():
    avoid = Avoid()
    avoid.remember(
        {"label": "Other site", "href": "https://other.example/x", "from_url": BASE},
        "https://other.example/signin",
    )
    page = _page()
    page["guards"]["3"] = _guard("https://other.example/different-page")
    assert set(avoid.apply(page)) == {"Other site", "Public article"}


def test_a_wall_on_the_same_site_does_not_hide_the_whole_site():
    avoid = Avoid()
    avoid.remember(
        {"label": "Members article", "href": "https://news.example/members", "from_url": BASE},
        "https://news.example/login",
    )
    assert avoid.sites == set()


def test_a_link_without_a_destination_is_hidden_by_its_label():
    avoid = Avoid()
    avoid.remember({"label": "Play", "href": None, "from_url": BASE}, BASE)
    page = _page()
    page["actions"][0]["label"] = "Play"
    page["guards"]["1"] = _guard(None)
    assert avoid.apply(page) == ["Play"]


def test_wall_rules_are_appended_once():
    once = with_wall_rules("find something")
    assert WALL_RULES in once and with_wall_rules(once) == once


# ---------- browser behaviour
def _browser(wall_results, can_go_back=True):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "s"
    results = list(wall_results)
    browser._probe_wall = Mock(side_effect=lambda: results.pop(0) if results else None)
    browser._go_back = Mock(return_value=can_go_back)
    browser._observe_page = Mock(side_effect=lambda *_: _page())
    browser._last_click = {
        "label": "Members article",
        "href": "https://news.example/members",
        "from_url": BASE,
    }
    return browser


LOGIN = {"kind": "login", "reason": "a sign-in form", "transient": False}


def test_a_login_wall_sends_the_browser_back_and_hides_the_link(monkeypatch):
    monkeypatch.delenv("JEV_WALL_BACKTRACK", raising=False)
    browser = _browser([LOGIN, None])

    wall_page = {**_page(), "url": "https://news.example/login", "text": "Sign in to continue"}
    page = browser._handle_walls(wall_page, screenshot=False)
    page = browser._drop_avoided(page)

    browser._go_back.assert_called_once()
    assert page["url"] == BASE
    assert "needed a login" in page["text"] and "Members article" in page["text"]
    assert "Choose a different link" in page["text"]
    assert "Members article" not in [a["label"] for a in page["actions"]]
    assert "Public article" in [a["label"] for a in page["actions"]]


def test_the_hidden_link_stays_hidden_on_later_pages_of_the_same_command(monkeypatch):
    browser = _browser([LOGIN, None])
    browser._handle_walls({**_page(), "url": "https://news.example/login"}, screenshot=False)
    later = browser._drop_avoided(_page())
    assert "Members article" not in [a["label"] for a in later["actions"]]


def test_without_an_earlier_page_the_model_is_told_not_to_touch_the_wall():
    browser = _browser([LOGIN], can_go_back=False)
    page = browser._handle_walls({**_page(), "text": "Sign in"}, screenshot=False)
    assert "no earlier page to go back to" in page["text"]
    assert browser._avoid is None


def test_after_the_wall_limit_jev_stops_trying(monkeypatch):
    monkeypatch.setenv("JEV_MAX_WALLS", "2")
    browser = _browser([LOGIN, None, LOGIN, None, LOGIN])
    browser._walls_hit = 2
    page = browser._handle_walls(_page(), screenshot=False)
    assert "choose BLOCKED" in page["text"]
    browser._go_back.assert_not_called()


def test_a_page_that_is_not_a_wall_is_left_alone():
    browser = _browser([None])
    original = _page()
    assert browser._handle_walls(original, screenshot=False)["text"] == "Articles"
    browser._go_back.assert_not_called()


def test_backing_out_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_WALL_BACKTRACK", "0")
    browser = _browser([LOGIN])
    browser._handle_walls(_page(), screenshot=False)
    browser._probe_wall.assert_not_called()


def test_a_passive_browser_check_that_clears_itself_is_not_treated_as_a_wall(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    checking = {"kind": "captcha", "reason": "a browser check", "transient": True}
    browser = _browser([checking, checking, None])
    browser._observe_page = Mock(return_value={**_page(), "text": "Real content"})
    page = browser._handle_walls({**_page(), "text": "Checking your browser"}, screenshot=False)
    browser._go_back.assert_not_called()
    assert page["text"] == "Real content"  # re-read after the check cleared, not the stale page


def test_a_passive_check_that_never_clears_counts_as_a_wall(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    monkeypatch.setenv("JEV_WALL_WAIT_MS", "0")
    checking = {"kind": "captcha", "reason": "a browser check", "transient": True}
    browser = _browser([checking, None])
    browser._handle_walls(_page(), screenshot=False)
    browser._go_back.assert_called_once()


def test_go_back_uses_the_previous_history_entry(monkeypatch):
    monkeypatch.setattr("jev_remote.safe_browser.time.sleep", lambda _s: None)
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "s"
    browser.evaluate = Mock(return_value="complete")
    entries = [{"id": 11}, {"id": 12}]
    browser.call = Mock(
        side_effect=lambda method, **p: (
            {"currentIndex": 1, "entries": entries} if method == "Page.getNavigationHistory" else {}
        )
    )
    assert browser._go_back() is True
    assert browser.call.call_args.kwargs == {"entryId": 11}

    browser.call = Mock(return_value={"currentIndex": 0, "entries": entries})
    assert browser._go_back() is False
