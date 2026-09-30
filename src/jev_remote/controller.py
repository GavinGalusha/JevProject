from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from .budget import RequestBudget
from .commands import ParsedCommand


class AgentLike(Protocol):
    browser: Any

    def run(self): ...

    def close(self) -> None: ...


log = logging.getLogger("uvicorn.error")

_INPUT_KEYS = ("prompt_tokens", "input_tokens")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens")


def _tokens(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    return next((int(usage[k]) for k in keys if isinstance(usage.get(k), int | float)), 0)


def _cost(tokens_in: int, tokens_out: int) -> float | None:
    """USD cost from JEV_PRICE_INPUT_PER_1M / JEV_PRICE_OUTPUT_PER_1M, or None if unset."""
    try:
        price_in = float(os.environ["JEV_PRICE_INPUT_PER_1M"])
        price_out = float(os.environ["JEV_PRICE_OUTPUT_PER_1M"])
    except (KeyError, ValueError):
        return None
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


AgentFactory = Callable[[str, str], AgentLike]


def _default_agent_factory(url: str, goal: str) -> AgentLike:
    import jev_ultrafast.agent as agent_module
    from jev_ultrafast import Agent

    base_url = os.environ.get("TEXT_MODEL_BASE_URL", "").rstrip("/")
    if base_url == "https://api.openai.com/v1":
        # Jev's stock helper uses an OpenRouter-style reasoning field. Install
        # our direct-OpenAI adapter, which uses Chat Completions parameters.
        from .openai_text import field_text

        agent_module.field_text = field_text
    return Agent(url, goal)


class RemoteController:
    """Own the single Jev tab and expose small, thread-safe operations around it."""

    def __init__(
        self,
        start_url: str,
        agent_factory: AgentFactory = _default_agent_factory,
        budget: RequestBudget | None = None,
    ):
        self.start_url = start_url
        self.budget = budget or RequestBudget()
        self.agent_factory = agent_factory
        self._lock = threading.RLock()
        self._agent: AgentLike | None = None
        self._running_agent: AgentLike | None = None
        self._job_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._armed = True
        self._status: dict[str, Any] = {
            "state": "idle",
            "message": "Ready",
            "command": None,
            "armed": True,
            "updated_at": self._now(),
        }

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def submit(self, command: ParsedCommand) -> dict[str, Any]:
        with self._lock:
            if not self._armed:
                raise RuntimeError(
                    "Remote is stopped and locked. Re-arm it before sending commands."
                )
        if command.kind == "media":
            return self.run_media(command)

        with self._lock:
            if self._job_thread and self._job_thread.is_alive():
                raise RuntimeError("Jev is already working on a command")
            if reason := self.budget.exceeded():
                raise RuntimeError(f"{reason}. Try again later or raise the limit in .env.")
            self._status = {
                "state": "working",
                "message": "Jev is working…",
                "command": command.text,
                "armed": True,
                "updated_at": self._now(),
            }
            self._stop_event.clear()
            self._job_thread = threading.Thread(
                target=self._run_jev,
                args=(command.text,),
                name="jev-command",
                daemon=True,
            )
            self._job_thread.start()
            return dict(self._status)

    def _run_jev(self, goal: str) -> None:
        new_agent: AgentLike | None = None
        try:
            with self._lock:
                previous = self._agent
                url = self._current_url(previous) or self.start_url

            new_agent = self.agent_factory(url, goal)
            with self._lock:
                self._running_agent = new_agent
            if self._stop_event.is_set():
                return
            self._activate(new_agent)
            last_state: dict[str, Any] = {}
            logged = tokens_in = tokens_out = jev_calls = openai_calls = 0
            limit_reason: str | None = None
            log.info("JEV GOAL: %s (start %s)", goal, url)
            for state in new_agent.run():
                if self._stop_event.is_set():
                    return
                last_state = state
                new_jev = len(state.get("decisions") or []) - jev_calls
                new_openai = len(state.get("text_calls") or []) - openai_calls
                jev_calls += new_jev
                openai_calls += new_openai
                self.budget.record(new_jev, new_openai)
                history = state.get("history") or []
                for entry in history[logged:]:
                    usage = entry.get("usage") or {}
                    tokens_in += _tokens(usage, _INPUT_KEYS)
                    tokens_out += _tokens(usage, _OUTPUT_KEYS)
                    text = f" text={entry['text']!r}" if entry.get("text") else ""
                    log.info(
                        "JEV STEP %s: %s [%s]%s conf=%.2f %sms usage=%s",
                        entry.get("step"), entry.get("action"), entry.get("kind"), text,
                        entry.get("confidence") or 0, entry.get("latency_ms"), usage,
                    )
                logged = len(history)
                detail = history[-1].get("action") if history else "Inspecting the page"
                with self._lock:
                    self._status.update(
                        message=detail,
                        steps=len(history),
                        elapsed_ms=state.get("elapsed_ms", 0),
                        updated_at=self._now(),
                    )
                if state.get("status") not in {"done", "blocked"} and (
                    limit_reason := self.budget.exceeded(jev_calls)
                ):
                    log.warning("JEV LIMIT: %s; stopping this command", limit_reason)
                    break

            cost = _cost(tokens_in, tokens_out)
            log.info(
                "JEV DONE: status=%s steps=%s jev_calls=%s openai_calls=%s "
                "tokens_in=%s tokens_out=%s cost=%s",
                last_state.get("status"), logged, jev_calls, openai_calls, tokens_in, tokens_out,
                f"${cost:.4f}" if cost is not None else "n/a (set JEV_PRICE_*_PER_1M)",
            )
            with self._lock:
                if self._stop_event.is_set():
                    return
                old_agent, self._agent = self._agent, new_agent
                new_agent = None
                self._status.update(
                    state="error" if limit_reason else last_state.get("status", "done"),
                    message=(
                        limit_reason
                        or (
                            "Command completed"
                            if last_state.get("status") == "done"
                            else "Jev stopped before confirming the goal"
                        )
                    ),
                    updated_at=self._now(),
                )
            if old_agent:
                old_agent.close()
        except Exception as exc:
            with self._lock:
                if not self._stop_event.is_set():
                    self._status.update(state="error", message=str(exc), updated_at=self._now())
        finally:
            with self._lock:
                if self._running_agent is new_agent:
                    self._running_agent = None
            if new_agent:
                new_agent.close()

    @staticmethod
    def _current_url(agent: AgentLike | None) -> str | None:
        if not agent:
            return None
        try:
            value = agent.browser.evaluate("location.href")
            valid = isinstance(value, str) and value.startswith(("http://", "https://"))
            return value if valid else None
        except Exception:
            return None

    @staticmethod
    def _activate(agent: AgentLike) -> None:
        """Bring Jev's normally-background tab onto the living-room display."""
        try:
            from browser_harness.helpers import cdp

            cdp("Target.activateTarget", targetId=agent.browser.target)
        except Exception:
            # Activation is display polish; it must not discard an otherwise usable run.
            pass

    def run_media(self, command: ParsedCommand) -> dict[str, Any]:
        with self._lock:
            if not self._armed:
                raise RuntimeError(
                    "Remote is stopped and locked. Re-arm it before sending commands."
                )
            if not self._agent:
                raise RuntimeError(
                    "No Jev-controlled media tab exists yet. Run a show command first."
                )

            payload = json.dumps({"action": command.action, "amount": command.amount})
            expression = f"""(async () => {{
              const command = {payload};
              const videos = [...document.querySelectorAll('video')]
                .filter(v => v.getBoundingClientRect().width && v.getBoundingClientRect().height);
              const video = videos.sort((a, b) =>
                b.getBoundingClientRect().width * b.getBoundingClientRect().height -
                a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0];
              if (!video) return {{ok:false, message:'No visible video found on this page'}};
              const action = command.action, amount = command.amount;
              if (action === 'pause') video.pause();
              else if (action === 'play') await video.play();
              else if (action === 'toggle') video.paused ? await video.play() : video.pause();
              else if (action === 'mute') video.muted = true;
              else if (action === 'unmute') video.muted = false;
              else if (action === 'volume') {{
                video.muted = false;
                video.volume = Math.max(0, Math.min(1, video.volume + amount));
              }}
              else if (action === 'seek') {{
                video.currentTime = Math.max(
                  0, Math.min(video.duration || Infinity, video.currentTime + amount)
                );
              }}
              else if (action === 'fullscreen') {{
                (video.requestFullscreen || video.webkitEnterFullscreen)?.call(video);
              }}
              return {{ok:true, paused:video.paused, muted:video.muted, volume:video.volume,
                currentTime:video.currentTime, action}};
            }})()"""
            result = self._agent.browser.call(
                "Runtime.evaluate",
                expression=expression,
                returnByValue=True,
                awaitPromise=True,
                userGesture=True,
            )
            value = result.get("result", {}).get("value") or {}
            if not value.get("ok"):
                raise RuntimeError(value.get("message", "Media command failed"))
            self._status = {
                "state": "done",
                "message": f"Media command: {command.action}",
                "command": command.text,
                "armed": True,
                "media": value,
                "updated_at": self._now(),
            }
            return dict(self._status)

    def kill(self) -> dict[str, Any]:
        """Stop current work, close owned tabs, and reject commands until explicitly re-armed."""
        with self._lock:
            self._armed = False
            self._stop_event.set()
            agents: list[AgentLike] = []
            for candidate in (self._agent, self._running_agent):
                if candidate and all(candidate is not agent for agent in agents):
                    agents.append(candidate)
            self._agent = None
            self._running_agent = None
            self._status = {
                "state": "stopped",
                "message": "STOPPED & LOCKED — all Jev-owned tabs were closed",
                "command": None,
                "armed": False,
                "updated_at": self._now(),
            }
        for agent in agents:
            try:
                agent.close()
            except Exception:
                pass
        return self.status()

    def arm(self) -> dict[str, Any]:
        with self._lock:
            if self._job_thread and self._job_thread.is_alive():
                raise RuntimeError("Wait for the stopped worker to exit before re-arming")
            self._stop_event.clear()
            self._armed = True
            self._status = {
                "state": "idle",
                "message": "Ready",
                "command": None,
                "armed": True,
                "updated_at": self._now(),
            }
            return dict(self._status)

    def close(self) -> None:
        self.kill()
