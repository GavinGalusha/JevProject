from unittest.mock import Mock

from jev_remote.done_guard import NOTE_VERIFY_DONE, guard_choose, min_done_confidence


def _decision(operation, confidence, usage=None):
    return {
        "operation": operation,
        "choice": operation if operation == "DONE" else "e5",
        "confidence": confidence,
        "usage": usage or {"input_tokens": 100, "output_tokens": 10},
    }


STATE = {"url": "https://x.example/", "text": "Flights  Zurich  London  Search"}


def test_a_confident_done_is_accepted_without_a_second_call():
    choose = Mock(return_value=_decision("DONE", 0.95))
    result = guard_choose(choose)(STATE, "goal", [])
    assert result["operation"] == "DONE" and choose.call_count == 1


def test_a_shaky_done_gets_one_nudge_and_the_new_answer_stands():
    choose = Mock(side_effect=[_decision("DONE", 0.39), _decision("CLICK", 0.8)])
    result = guard_choose(choose)(STATE, "goal", [])

    assert result["operation"] == "CLICK" and result["done_rechecked"] is True
    assert choose.call_count == 2
    nudged_state = choose.call_args_list[1].args[0]
    assert NOTE_VERIFY_DONE in nudged_state["text"] and nudged_state["text"].startswith("Flights")
    assert "[Jev]" not in STATE["text"]  # the original page is not modified
    assert result["usage"] == {"input_tokens": 200, "output_tokens": 20}  # both calls counted


def test_if_the_model_still_says_done_after_the_nudge_it_is_accepted():
    choose = Mock(side_effect=[_decision("DONE", 0.39), _decision("DONE", 0.45)])
    result = guard_choose(choose)(STATE, "goal", [])
    assert result["operation"] == "DONE" and choose.call_count == 2  # never loops


def test_other_operations_are_never_second_guessed():
    for operation in ("CLICK", "BLOCKED", "WAIT"):
        choose = Mock(return_value=_decision(operation, 0.2))
        assert guard_choose(choose)(STATE, "goal", [])["operation"] == operation
        assert choose.call_count == 1


def test_the_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JEV_MIN_DONE_CONFIDENCE", "0")
    choose = Mock(return_value=_decision("DONE", 0.1))
    guard_choose(choose)(STATE, "goal", [])
    assert choose.call_count == 1


def test_threshold_defaults_and_bad_values(monkeypatch):
    monkeypatch.delenv("JEV_MIN_DONE_CONFIDENCE", raising=False)
    assert min_done_confidence() == 0.6
    monkeypatch.setenv("JEV_MIN_DONE_CONFIDENCE", "nonsense")
    assert min_done_confidence() == 0.6
    monkeypatch.setenv("JEV_MIN_DONE_CONFIDENCE", "0.75")
    assert min_done_confidence() == 0.75


VERIFIED_STATE = {
    "url": "https://tubitv.com/tv-shows/1/s02-e04-x",
    "text": "[Jev] The video player is now playing. The playback goal is complete. "
    "[verified: Season 2 Episode 4]\nEverybody Hates Chris",
}


def test_a_proven_playing_episode_finishes_without_asking_the_model(monkeypatch):
    monkeypatch.setenv("JEV_AUTO_DONE_ON_PLAYING", "1")
    choose = Mock()
    result = guard_choose(choose)(VERIFIED_STATE, "goal", [])
    assert result["operation"] == "DONE" and result["choice"] == "DONE"
    assert result["confidence"] == 1.0 and result["model"] == "verified-playing"
    choose.assert_not_called()


def test_auto_finish_is_off_by_default(monkeypatch):
    monkeypatch.delenv("JEV_AUTO_DONE_ON_PLAYING", raising=False)
    choose = Mock(return_value=_decision("CLICK", 0.9))
    assert guard_choose(choose)(VERIFIED_STATE, "goal", [])["operation"] == "CLICK"
    choose.assert_called_once()


def test_auto_finish_needs_the_verified_marker(monkeypatch):
    monkeypatch.setenv("JEV_AUTO_DONE_ON_PLAYING", "1")
    unverified = {"text": "[Jev] The video player is now playing. The playback goal is complete."}
    wrong = {
        "text": "[Jev] A video is playing, but this page is not Season 2 Episode 4. NOT complete."
    }
    for state in (unverified, wrong, {"text": "nothing"}):
        choose = Mock(return_value=_decision("CLICK", 0.9))
        assert guard_choose(choose)(state, "goal", [])["operation"] == "CLICK"
        choose.assert_called_once()
