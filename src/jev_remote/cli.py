from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .chrome import ensure_chrome
from .config import Settings


def configure_text_model() -> None:
    """Translate the familiar OpenAI key name into Jev's provider-neutral name."""
    if openai_key := os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ.setdefault("TEXT_MODEL_API_KEY", openai_key)
        os.environ.setdefault("TEXT_MODEL_BASE_URL", "https://api.openai.com/v1")
        os.environ.setdefault("TEXT_MODEL", "gpt-5-nano")


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    configure_text_model()
    try:
        settings = Settings.from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if os.environ.get("JEV_AUTO_LAUNCH_CHROME", "1").strip().lower() not in {"0", "false", "no"}:
        try:
            print(ensure_chrome())
        except RuntimeError as exc:
            print(f"Chrome launch failed: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
