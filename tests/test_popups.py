from unittest.mock import Mock, patch

import pytest

from jev_remote.popups import popup_targets_to_close, site_of
from jev_remote.safe_browser import StableTargetBrowser


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://www.linkedin.com/in/someone", "linkedin.com"),
        ("https://news.bbc.co.uk/story", "bbc.co.uk"),
        ("http://127.0.0.1:8787/", "127.0.0.1"),
        ("about:blank", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_site_of(url, site):
    assert site_of(url) == site


def test_only_cross_site_tabs_opened_by_our_tab_are_closed():
    targets = [
        {"targetId": "mine", "type": "page", "url": "https://ww1.example.com/watch"},
        {"targetId": "ad", "type": "page", "url": "https://ads.tracker.net/x", "openerId": "mine"},
        {
            "targetId": "same",
            "type": "page",
            "url": "https://cdn.example.com/p",
            "openerId": "mine",
        },
        {"targetId": "blank", "type": "page", "url": "about:blank", "openerId": "mine"},
        {"targetId": "chain", "type": "page", "url": "https://evil.org/", "openerId": "ad"},
        {
            "targetId": "user",
            "type": "page",
            "url": "https://mail.google.com/",
            "openerId": "other",
        },
        {
            "targetId": "worker",
            "type": "service_worker",
            "url": "https://x.net/",
            "openerId": "mine",
        },
    ]

    closed = popup_targets_to_close(targets, "mine", "https://ww1.example.com/watch")

    assert {t["targetId"] for t in closed} == {"ad", "chain"}


def _browser(probe_result):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "session"
    browser.evaluate = Mock(return_value=probe_result)
    browser.call = Mock()
    return browser


def test_overlay_with_a_close_control_is_clicked_once():
    browser = _browser(
        {"found": True, "x": 40.0, "y": 50.0, "name": "Dismiss", "summary": "Sign in"}
    )
    with patch("jev_remote.safe_browser.time.sleep"):
        assert browser._dismiss_overlay() is True

    events = [c.kwargs["type"] for c in browser.call.call_args_list]
    assert events == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert all(c.kwargs["x"] == 40.0 and c.kwargs["y"] == 50.0 for c in browser.call.call_args_list)


def test_overlay_without_a_safe_control_gets_escape_not_a_click():
    browser = _browser({"found": True, "x": None, "y": None, "name": None, "summary": "Subscribe"})
    with patch("jev_remote.safe_browser.time.sleep"):
        assert browser._dismiss_overlay() is True

    methods = [c.args[0] for c in browser.call.call_args_list]
    assert methods == ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"]
    assert all(c.kwargs["key"] == "Escape" for c in browser.call.call_args_list)


def test_no_overlay_means_no_input():
    browser = _browser(None)
    assert browser._dismiss_overlay() is False
    browser.call.assert_not_called()


def test_dismissals_are_capped_per_page_session(monkeypatch):
    monkeypatch.setenv("JEV_MAX_POPUP_DISMISSALS", "2")
    browser = _browser({"found": True, "x": 1.0, "y": 1.0, "name": "Close", "summary": "x"})
    with patch("jev_remote.safe_browser.time.sleep"):
        results = [browser._dismiss_overlay() for _ in range(4)]
    assert results == [True, True, False, False]


def test_popup_handling_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_DISMISS_POPUPS", "0")
    browser = _browser({"found": True, "x": 1.0, "y": 1.0, "name": "Close", "summary": "x"})
    assert browser._clear_interruptions() is False
    browser.evaluate.assert_not_called()


def test_popup_check_failure_never_ends_the_run():
    browser = _browser(None)
    browser.evaluate.side_effect = RuntimeError("devtools went away")
    with patch.object(StableTargetBrowser, "_close_popup_tabs", return_value=False):
        assert browser._clear_interruptions() is False


def test_a_kiosk_window_uses_the_real_screen_size_unless_a_fixed_viewport_is_requested(monkeypatch):
    from jev_remote.safe_browser import _use_real_viewport

    monkeypatch.delenv("JEV_KIOSK", raising=False)
    monkeypatch.delenv("JEV_FIXED_VIEWPORT", raising=False)
    assert _use_real_viewport() is True
    monkeypatch.setenv("JEV_FIXED_VIEWPORT", "1")
    assert _use_real_viewport() is False
    monkeypatch.delenv("JEV_FIXED_VIEWPORT")
    monkeypatch.setenv("JEV_KIOSK", "0")
    assert _use_real_viewport() is False
