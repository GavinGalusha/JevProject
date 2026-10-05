from unittest.mock import Mock, patch

import pytest

from jev_remote.popups import link_sites
from jev_remote.safe_browser import StableTargetBrowser

OWN = {"targetId": "own", "type": "page", "url": "https://www.google.com/search?q=x"}


def _tab(target_id, url, **extra):
    return {"targetId": target_id, "type": "page", "url": url, **extra}


def _browser(href, before=("own",), role="link", player=False, goal=""):
    browser = StableTargetBrowser.__new__(StableTargetBrowser)
    browser.session = "s"
    browser.target = "own"
    browser.goal_text = goal
    browser._last_click = {
        "label": "Result",
        "href": href,
        "from_url": OWN["url"],
        "role": role,
        "player": player,
    }
    browser._tabs_before_click = set(before)
    browser.call = Mock()
    browser._wait_ready = Mock()
    browser._wait_off_google = Mock()
    return browser


def _run(browser, targets):
    with patch("browser_harness.helpers.cdp") as cdp:
        cdp.side_effect = lambda method, **_p: (
            {"targetInfos": targets} if method == "Target.getTargets" else {}
        )
        result = browser._close_popup_tabs()
    return result, cdp


@pytest.mark.parametrize(
    ("href", "sites"),
    [
        ("https://tubitv.com/tv-shows/1/x", {"tubitv.com"}),
        (
            "https://www.google.com/url?q=https%3A%2F%2Ftubitv.com%2Fx&sa=U",
            {"google.com", "tubitv.com"},
        ),
        ("/relative", set()),
        (None, set()),
    ],
)
def test_link_sites_includes_redirect_targets(href, sites):
    assert link_sites(href) == (sites if href and href.startswith("http") else set())


def test_a_link_that_opens_in_a_new_tab_is_followed_in_the_jev_tab():
    browser = _browser("https://tubitv.com/tv-shows/200203411/s02-e04-x")
    new = _tab("new", "https://tubitv.com/tv-shows/200203411/s02-e04-x")  # noopener: no openerId

    changed, cdp = _run(browser, [OWN, new])

    assert changed is True
    assert browser.call.call_args.args[0] == "Page.navigate"
    assert browser.call.call_args.kwargs["url"] == new["url"]
    closed = [c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"]
    assert closed == ["new"]
    assert browser._tabs_before_click is None


def test_a_google_redirect_link_is_followed_to_its_real_destination():
    href = "https://www.google.com/url?q=https%3A%2F%2Ftubitv.com%2Fseries%2F1&sa=U"
    browser = _browser(href)
    changed, _ = _run(browser, [OWN, _tab("new", "https://tubitv.com/series/1")])
    assert changed is True and browser.call.call_args.kwargs["url"] == "https://tubitv.com/series/1"


def test_an_ad_popup_to_an_unrelated_site_is_not_followed_but_still_closed():
    browser = _browser("https://site.example/play")
    ad = _tab("ad", "https://ads.tracker.net/x", openerId="own")

    changed, cdp = _run(browser, [OWN, ad])

    assert changed is True  # closed as a popup
    browser.call.assert_not_called()  # never navigated the Jev tab to the ad
    closed = [c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"]
    assert closed == ["ad"]


def test_a_control_with_no_destination_never_pulls_in_a_new_tab():
    browser = _browser(None)  # e.g. a play button
    popup = _tab("p", "https://web.wootly.ch/moon", openerId="own")
    _, cdp = _run(browser, [OWN, popup])
    browser.call.assert_not_called()
    assert [
        c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"
    ] == ["p"]


def test_a_new_tab_that_is_still_blank_is_looked_at_again_on_the_next_read():
    browser = _browser("https://tubitv.com/series/1")
    changed, _ = _run(browser, [OWN, _tab("new", "about:blank")])
    assert changed is False and browser._tabs_before_click == {"own"}  # still waiting for it


def test_tabs_that_existed_before_the_click_are_left_alone():
    browser = _browser("https://tubitv.com/series/1", before=("own", "old"))
    changed, _ = _run(browser, [OWN, _tab("old", "https://tubitv.com/series/1")])
    assert changed is False


def test_following_new_tabs_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_FOLLOW_NEW_TABS", "0")
    browser = _browser("https://tubitv.com/series/1")
    changed, _ = _run(browser, [OWN, _tab("new", "https://tubitv.com/series/1")])
    assert changed is False
    browser.call.assert_not_called()


def test_a_link_to_the_same_site_is_not_treated_as_a_new_tab_navigation():
    # a Google page opening another ordinary Google page is not "going to the site I asked for"
    browser = _browser("https://www.google.com/search?q=everybody+hates+chris")
    changed, _ = _run(browser, [OWN, _tab("new", "https://www.google.com/maps")])
    assert changed is False
    browser.call.assert_not_called()


GOAL = "Search Google for Everybody Hates Chris season 2 episode 4 on Tubi and play it"


def test_a_google_video_card_that_opens_the_site_named_in_the_goal_is_followed():
    # the real case: the card's own link points back at Google, the new tab is tubitv.com
    browser = _browser("https://www.google.com/search?q=x&ved=1", goal=GOAL)
    new = _tab("new", "https://tubitv.com/tv-shows/200203411/s02-e04-everybody-hates-a-liar")

    changed, cdp = _run(browser, [OWN, new])

    assert changed is True and browser.call.call_args.kwargs["url"] == new["url"]


def test_the_same_tab_is_an_ad_when_the_goal_never_names_that_site():
    browser = _browser("https://www.google.com/search?q=x", goal="play Moana")
    ad = _tab("ad", "https://tubitv.com/x", openerId="own")
    changed, cdp = _run(browser, [OWN, ad])
    browser.call.assert_not_called()
    assert [
        c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"
    ] == ["ad"]


def test_google_outbound_wrappers_are_followed_and_we_wait_for_the_redirect():
    browser = _browser("https://www.google.com/search?q=x", goal="")
    changed, _ = _run(browser, [OWN, _tab("new", "https://www.google.com/goto?url=CAESfAH")])
    assert changed is True
    browser._wait_off_google.assert_called_once()


def test_nothing_is_followed_after_clicking_the_video_player_even_if_the_site_is_named():
    browser = _browser(None, role="button", player=True, goal="play it on wootly")
    popup = _tab("p", "https://web.wootly.ch/moon", openerId="own")
    changed, cdp = _run(browser, [OWN, popup])
    browser.call.assert_not_called()
    assert [
        c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"
    ] == ["p"]


def test_only_link_clicks_can_be_followed_by_name_not_buttons():
    browser = _browser("https://www.google.com/search", role="button", goal=GOAL)
    changed, _ = _run(browser, [OWN, _tab("new", "https://tubitv.com/series/1", openerId="own")])
    browser.call.assert_not_called()


@pytest.mark.parametrize(
    ("goal", "url", "expected"),
    [
        ("... on Tubi and play it", "https://tubitv.com/x", True),
        ("play it on YouTube", "https://www.youtube.com/watch?v=1", True),
        ("play Moana", "https://tubitv.com/x", False),
        ("play it on tubi", "https://ads.tracker.net/x", False),
        ("go on", "https://on.example/", False),  # too short to mean anything
    ],
)
def test_the_goal_naming_a_site(goal, url, expected):
    from jev_remote.popups import goal_names_site

    assert goal_names_site(goal, url) is expected
