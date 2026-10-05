from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .chrome import ensure_chrome
from .config import Settings
from .pairing import lan_ip
from .tls import ensure_certificate


def configure_text_model() -> None:
    """Translate the familiar OpenAI key name into Jev's provider-neutral name."""
    if openai_key := os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ.setdefault("TEXT_MODEL_API_KEY", openai_key)
        os.environ.setdefault("TEXT_MODEL_BASE_URL", "https://api.openai.com/v1")
        os.environ.setdefault("TEXT_MODEL", "gpt-5-mini")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Jev living-room remote")
    protocol = parser.add_mutually_exclusive_group()
    protocol.add_argument(
        "--http",
        dest="https",
        action="store_false",
        help="serve plain HTTP for temporary LAN testing (phone microphone unavailable)",
    )
    protocol.add_argument(
        "--https",
        dest="https",
        action="store_true",
        help="serve HTTPS, overriding JEV_HTTPS (the default)",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="also load .env.NAME on top of .env (for example --profile demo)",
    )
    parser.set_defaults(https=None)
    return parser.parse_args(argv)


def load_environment(profile: str | None = None, directory: Path | None = None) -> Path | None:
    """Load .env, then (optionally) .env.<profile> over it. Returns the profile file used."""
    directory = directory or Path.cwd()
    load_dotenv(directory / ".env")
    if not profile:
        return None
    if not profile.replace("-", "").replace("_", "").isalnum():
        raise SystemExit(f"Invalid profile name {profile!r}: use letters, digits, - and _")
    overlay = directory / f".env.{profile}"
    if not overlay.is_file():
        raise SystemExit(
            f"Profile {profile!r} not found: create {overlay.name} "
            f"(copy {overlay.name}.example if there is one)."
        )
    load_dotenv(overlay, override=True)
    return overlay


def https_enabled(cli_override: bool | None = None) -> bool:
    if cli_override is not None:
        return cli_override
    return os.environ.get("JEV_HTTPS", "1").strip().lower() not in {"0", "false", "no"}


def serve_host(settings: Settings) -> str:
    return lan_ip() if settings.host in {"0.0.0.0", ""} else settings.host


def print_pairing(pairing, settings: Settings, scheme: str = "http") -> None:
    import qrcode

    base = f"{scheme}://{serve_host(settings)}:{settings.port}"
    qr = qrcode.QRCode(border=1)
    qr.add_data(f"{base}/#pair={pairing.display_code}")
    qr.make()
    print("\nPair your phone: scan this with its camera (same Wi-Fi).\n")
    qr.print_ascii(invert=True)
    print(f"\nOr open {base} and enter the pairing code: {pairing.display_code}")
    if scheme == "https":
        print(
            "Your phone will warn that the certificate is not trusted (it is self-signed).\n"
            "Choose Advanced / Show details, then proceed. This is what lets the microphone work."
        )
    print("The code works once and expires in 15 minutes.\n")
    print("Ready. Each command's progress will print below.\n", flush=True)


RESTART_EXIT_CODE = 75  # the server asks its supervisor for a fresh process
SUPERVISED_ENV = "JEV_SUPERVISED"
RESTARTED_ENV = "JEV_RESTARTED"


def supervise(argv: list[str]) -> None:
    """Run the server as a child process and start a fresh one whenever it asks to restart.

    The child shares this console and working directory, so `.env`, the pairing QR code and
    Ctrl+C behave exactly as before. Any other exit code is passed straight through.
    """
    restarted = False
    while True:
        env = {**os.environ, SUPERVISED_ENV: "1"}
        if restarted:
            env[RESTARTED_ENV] = "1"
        child = subprocess.Popen([sys.executable, "-m", "jev_remote.cli", *argv], env=env)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=10)
            except Exception:
                child.kill()
            raise SystemExit(130) from None
        if code != RESTART_EXIT_CODE:
            raise SystemExit(code)
        print("\n↻  Restarting the Jev server…\n", flush=True)
        restarted = True


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(SUPERVISED_ENV) != "1":
        supervise(argv)
        return
    serve(argv)


def serve(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    overlay = load_environment(args.profile)
    if overlay:
        print(f"Profile loaded: {overlay.name} (over .env)")
    configure_text_model()
    try:
        settings = Settings.from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    restarted = os.environ.get(RESTARTED_ENV) == "1"
    if restarted:
        # The browser was closed on purpose; the next command reopens it.
        print("Server restarted. Chrome stays closed until your next command.")
    elif os.environ.get("JEV_AUTO_LAUNCH_CHROME", "1").strip().lower() not in {"0", "false", "no"}:
        try:
            print(ensure_chrome())
        except RuntimeError as exc:
            print(f"Chrome launch failed: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    app = create_app(settings)
    tls: dict[str, str] = {}
    scheme = "http"
    if https_enabled(args.https):
        cert, key = ensure_certificate(serve_host(settings))
        tls = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        scheme = "https"
    print_pairing(app.state.pairing, settings, scheme)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, **tls))
    restart = {"requested": False}

    def request_restart() -> None:
        restart["requested"] = True
        # Let the HTTP response reach the phone before the server shuts down.
        threading.Timer(1.0, lambda: setattr(server, "should_exit", True)).start()

    # Restarting needs the supervisor; a server started directly can only close the browser.
    app.state.request_restart = request_restart if os.environ.get(SUPERVISED_ENV) == "1" else None
    server.run()
    if restart["requested"]:
        raise SystemExit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
