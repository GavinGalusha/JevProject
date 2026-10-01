from jev_remote.cli import https_enabled, parse_args


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
