from jev_remote.cli import https_enabled, load_environment, parse_args


def test_protocol_cli_flags_override_environment(monkeypatch):
    monkeypatch.setenv("JEV_HTTPS", "1")
    assert parse_args(["--http"]).https is False
    assert https_enabled(parse_args(["--http"]).https) is False

    monkeypatch.setenv("JEV_HTTPS", "0")
    assert parse_args(["--https"]).https is True
    assert https_enabled(parse_args(["--https"]).https) is True


def test_protocol_defaults_to_environment(monkeypatch):
    assert parse_args([]).https is None
    monkeypatch.setenv("JEV_HTTPS", "0")
    assert https_enabled() is False
    monkeypatch.setenv("JEV_HTTPS", "1")
    assert https_enabled() is True


def test_profile_overlay_loads_on_top_of_env_only_when_asked(monkeypatch, tmp_path):
    import pytest

    (tmp_path / ".env").write_text("JEV_START_URL=https://home.example/\nJEV_KEEP=1\n")
    (tmp_path / ".env.demo").write_text("JEV_START_URL=https://www.google.com/\n")
    for name in ("JEV_START_URL", "JEV_KEEP"):
        monkeypatch.delenv(name, raising=False)

    # no profile: the home setup is exactly what .env says
    assert load_environment(None, tmp_path) is None
    import os

    assert os.environ["JEV_START_URL"] == "https://home.example/"

    # with the profile: overridden where it says so, everything else untouched
    assert load_environment("demo", tmp_path) == tmp_path / ".env.demo"
    assert os.environ["JEV_START_URL"] == "https://www.google.com/"
    assert os.environ["JEV_KEEP"] == "1"

    with pytest.raises(SystemExit, match="not found"):
        load_environment("missing", tmp_path)
    with pytest.raises(SystemExit, match="Invalid profile name"):
        load_environment("../etc", tmp_path)


def test_profile_flag_is_parsed():
    assert parse_args(["--profile", "demo"]).profile == "demo"
    assert parse_args([]).profile is None
