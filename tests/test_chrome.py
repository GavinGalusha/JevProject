from pathlib import Path

import pytest

from jev_remote import chrome


def test_ensure_chrome_skips_launch_when_already_running(monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(chrome, "cdp_alive", lambda url: True)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert "already running" in chrome.ensure_chrome()


def test_ensure_chrome_launches_with_port_and_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setenv("JEV_CHROME_PROFILE", str(tmp_path / "profile"))
    states = iter([False, True])
    monkeypatch.setattr(chrome, "cdp_alive", lambda url: next(states))
    launched = []
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    assert "Launched" in chrome.ensure_chrome(timeout=2)
    assert "--remote-debugging-port=9333" in launched[0]
    assert any(a.startswith("--user-data-dir=") for a in launched[0])
    assert Path(tmp_path / "profile").is_dir()


def test_ensure_chrome_refuses_remote_endpoint(monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://10.0.0.5:9222")
    monkeypatch.setattr(chrome, "cdp_alive", lambda url: False)
    with pytest.raises(RuntimeError):
        chrome.ensure_chrome()
