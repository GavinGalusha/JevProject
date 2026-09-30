from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from .budget import RequestBudget
from .commands import ParsedCommand
from .console import Heartbeat, say


class AgentLike(Protocol):
    browser: Any

    def run(self): ...

    def close(self) -> None: ...


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

    def submit(
        self,
        command: ParsedCommand,
        start_url: str | None = None,
        then_fullscreen: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._armed:
                raise RuntimeError(
                    "Remote is stopped and locked. Re-arm it before sending commands."
                )
        if command.kind == "media":
            return self.run_media(command)
        if start_url:
            start_url = start_url.strip()
            if urlparse(start_url).scheme not in {"http", "https"}:
                raise ValueError("Start URL must begin with http:// or https://")

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
                args=(command.text, start_url or None, then_fullscreen),
                name="jev-command",
                daemon=True,
            )
            self._job_thread.start()
            return dict(self._status)

    def _run_jev(
        self, goal: str, start_url: str | None = None, then_fullscreen: bool = False
    ) -> None:
        new_agent: AgentLike | None = None
        started = time.monotonic()
        progress = {"steps": 0, "what": "opening the browser tab"}

        def describe() -> str:
            return f"{progress['steps']} steps so far, last: {progress['what']} ({elapsed()}s)"

        def elapsed() -> str:
            return f"{time.monotonic() - started:.0f}"

        try:
            with self._lock:
                previous = self._agent
                url = start_url or self._current_url(previous) or self.start_url

            say(f"▶  NEW COMMAND: {goal}", "cyan", rule=True)
            say(f"   starting at {url}", "dim")
            with Heartbeat(describe):
                new_agent = self.agent_factory(url, goal)
                with self._lock:
                    self._running_agent = new_agent
                if self._stop_event.is_set():
                    say("■  Stopped before it began (STOP & LOCK)", "yellow")
                    return
                self._activate(new_agent)
                progress["what"] = "reading the page"
                last_state: dict[str, Any] = {}
                logged = tokens_in = tokens_out = jev_calls = openai_calls = 0
                limit_reason: str | None = None
                for state in new_agent.run():
                    if self._stop_event.is_set():
                        say("■  Stopped by STOP & LOCK", "yellow")
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
                        kind = str(entry.get("kind") or "?").upper()
                        typed = f'  ← "{entry["text"]}"' if entry.get("text") else ""
                        same = entry.get("page_changed") is False
                        moved = "  (page unchanged)" if same else ""
                        say(
                            f"   step {entry.get('step'):>2}  {kind:<6} {entry.get('action')}"
                            f"{typed}{moved}  [{entry.get('latency_ms')}ms,"
                            f" conf {(entry.get('confidence') or 0):.2f}]"
                        )
                        progress["what"] = f"{kind} {entry.get('action')}"
                    logged = len(history)
                    progress["steps"] = logged
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
                        break

            cost = _cost(tokens_in, tokens_out)
            cost_text = f"${cost:.4f}" if cost is not None else "cost n/a (set JEV_PRICE_*_PER_1M)"
            summary = (
                f"{logged} steps · {jev_calls} TypeSafe + {openai_calls} OpenAI calls · "
                f"{tokens_in:,} in / {tokens_out:,} out tokens · {cost_text} · {elapsed()}s"
            )
            final = last_state.get("status")
            if limit_reason:
                say(f"⚠  LIMIT HIT — {limit_reason}. Command stopped.", "yellow")
                say(f"   {summary}", "dim")
            elif final == "done":
                say("✔  DONE — goal confirmed on the page", "green")
                say(f"   {summary}", "dim")
            else:
                say("✖  FINISHED WITHOUT CONFIRMING THE GOAL (Jev got stuck or was blocked)", "red")
                say(f"   {summary}", "dim")
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
            if then_fullscreen and final == "done" and not limit_reason:
                self._auto_fullscreen()
        except Exception as exc:
            with self._lock:
                stopped = self._stop_event.is_set()
                if not stopped:
                    self._status.update(state="error", message=str(exc), updated_at=self._now())
            if not stopped:
                say(f"✖  ERROR after {elapsed()}s — {type(exc).__name__}: {exc}", "red")
        finally:
            with self._lock:
                if self._running_agent is new_agent:
                    self._running_agent = None
            if new_agent:
                new_agent.close()

    def _auto_fullscreen(self) -> None:
        try:
            time.sleep(1.0)  # let the player finish loading
            self.run_media(ParsedCommand("media", "fullscreen", "fullscreen"))
            say("⛶  Fullscreen requested", "green")
        except Exception as exc:
            say(f"⚠  Fullscreen failed — {exc}", "yellow")

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
              if (!video && command.action === 'fullscreen') {{
                // Embedded players live in an <iframe>; fullscreen the biggest visible one.
                const frames = [...document.querySelectorAll('iframe')]
                  .filter(f => f.getBoundingClientRect().width && f.getBoundingClientRect().height);
                const frame = frames.sort((a, b) =>
                  b.getBoundingClientRect().width * b.getBoundingClientRect().height -
                  a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0];
                if (frame) {{
                  await frame.requestFullscreen();
                  return {{ok:true, action:'fullscreen', target:'iframe'}};
                }}
              }}
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
            say(f"▶  MEDIA: {command.text} → {command.action} ✔ (no API calls)", "green")
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
        say("■  STOP & LOCK — tabs closed, remote locked until re-armed", "yellow", rule=True)
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
            say("●  RE-ARMED — accepting commands again", "green")
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
