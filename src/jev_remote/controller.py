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
from .goal_refiner import GoalPlan, refine_goal
from .player import is_playback_goal, with_playback_rules
from .vision_recovery import RecoveryAdvice, analyze_screenshot


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


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _safe_url(value: Any) -> str:
    """Show navigation without leaking query-string tokens or fragments to the terminal."""
    if not isinstance(value, str):
        return "unknown URL"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return value[:200]
    return parsed._replace(query="", fragment="").geturl()[:300]


def _decision_target_label(decision: dict[str, Any]) -> str | None:
    """Recover the selected visible label without dumping the full model request."""
    operation = str(decision.get("operation") or "").lower()
    target = decision.get("target")
    try:
        criteria = decision["request"]["questions"][f"{operation}_target"]["criteria"]
        candidate = criteria.get(str(target), criteria.get(target))
        label = candidate.get("element") if isinstance(candidate, dict) else None
        return str(label)[:300] if label else None
    except (KeyError, TypeError, AttributeError):
        return None


AgentFactory = Callable[[str, str], AgentLike]
VisionAnalyzer = Callable[
    [str, str, dict[str, Any], list[dict[str, Any]]], RecoveryAdvice
]
GoalRefiner = Callable[[str, str | None], GoalPlan]


def _default_agent_factory(url: str, goal: str) -> AgentLike:
    import jev_ultrafast.agent as agent_module
    from jev_ultrafast import Agent

    from .safe_browser import StableTargetBrowser

    # Keep the audited Jev action machinery, but avoid page-wide false invalidations on dynamic
    # sites. Agent.__init__ resolves Browser from its module globals when the instance is created.
    agent_module.Browser = StableTargetBrowser
    StableTargetBrowser.expect_player = is_playback_goal(goal)

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
        command_timeout: float = 180.0,
        stall_timeout: float = 45.0,
        watchdog_interval: float = 0.5,
        vision_analyzer: VisionAnalyzer = analyze_screenshot,
        goal_refiner: GoalRefiner = refine_goal,
        max_stale_decisions: int | None = None,
    ):
        self.start_url = start_url
        self.budget = budget or RequestBudget()
        self.agent_factory = agent_factory
        self._lock = threading.RLock()
        self._agent: AgentLike | None = None
        self._running_agent: AgentLike | None = None
        self._job_thread: threading.Thread | None = None
        self._job_serial = 0
        self._active_job_id: int | None = None
        self._job_cancel_event: threading.Event | None = None
        self._job_started_at = 0.0
        self._last_progress_at = 0.0
        self._stop_event = threading.Event()
        self._armed = True
        self.command_timeout = command_timeout
        self.stall_timeout = stall_timeout
        self.watchdog_interval = watchdog_interval
        self.vision_analyzer = vision_analyzer
        self.goal_refiner = goal_refiner
        self.max_stale_decisions = (
            _int_env("JEV_MAX_STALE_DECISIONS", 6)
            if max_stale_decisions is None
            else max(0, max_stale_decisions)
        )
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
            result = dict(self._status)
            if result.get("state") == "working" and self._job_started_at:
                result["elapsed_ms"] = round((time.monotonic() - self._job_started_at) * 1000)
            return result

    def submit(
        self,
        command: ParsedCommand,
        start_url: str | None = None,
        then_fullscreen: bool = False,
        vision_recovery: bool = False,
        guided: bool = False,
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
            if self._active_job_id is not None:
                active_text = self._status.get("command", "").strip().casefold()
                if active_text == command.text.strip().casefold():
                    duplicate = dict(self._status)
                    duplicate["duplicate"] = True
                    return duplicate
                active = self._status.get("command") or "another command"
                raise RuntimeError(f"Jev is already working on: {active!r}")
            if reason := self.budget.exceeded():
                raise RuntimeError(f"{reason}. Try again later or raise the limit in .env.")
            self._job_serial += 1
            job_id = self._job_serial
            cancel_event = threading.Event()
            now = time.monotonic()
            self._active_job_id = job_id
            self._job_cancel_event = cancel_event
            self._job_started_at = now
            self._last_progress_at = now
            modes = [
                name
                for enabled, name in ((guided, "guided"), (vision_recovery, "vision"))
                if enabled
            ]
            self._status = {
                "state": "working",
                "message": "Jev is working…",
                "command": command.text,
                "armed": True,
                "job_id": job_id,
                "steps": 0,
                "mode": "+".join(modes) if modes else "structured",
                "updated_at": self._now(),
            }
            self._stop_event.clear()
            self._job_thread = threading.Thread(
                target=self._run_jev,
                args=(
                    job_id,
                    cancel_event,
                    command.text,
                    start_url or None,
                    then_fullscreen,
                    vision_recovery,
                    guided,
                ),
                name="jev-command",
                daemon=True,
            )
            self._job_thread.start()
            threading.Thread(
                target=self._watch_job,
                args=(job_id, cancel_event),
                name=f"jev-watchdog-{job_id}",
                daemon=True,
            ).start()
            return dict(self._status)

    def _run_jev(
        self,
        job_id: int,
        cancel_event: threading.Event,
        goal: str,
        start_url: str | None = None,
        then_fullscreen: bool = False,
        vision_recovery: bool = False,
        guided: bool = False,
    ) -> None:
        new_agent: AgentLike | None = None
        adopted = False
        started = time.monotonic()
        progress = {"steps": 0, "what": "opening the browser tab"}
        logged = tokens_in = tokens_out = jev_calls = openai_calls = 0
        decision_logged = text_logged = vision_recoveries = guided_plans = 0
        stale_retries = consecutive_stale = 0
        execution_goal = goal

        def describe() -> str:
            return f"{progress['steps']} steps so far, last: {progress['what']} ({elapsed()}s)"

        def elapsed() -> str:
            return f"{time.monotonic() - started:.0f}"

        try:
            with self._lock:
                previous = self._agent
                url = start_url or self._current_url(previous) or self.start_url

            say(f"▶  NEW COMMAND: {goal}", "cyan", rule=True)
            modes = [
                name
                for enabled, name in ((guided, "guided"), (vision_recovery, "vision"))
                if enabled
            ]
            say(f"   starting at {_safe_url(url)}", "dim")
            say(
                f"   mode={'+'.join(modes) if modes else 'structured'} · "
                f"action cap={self.budget.max_steps or 'off'} · "
                f"stale-decision cap={self.max_stale_decisions or 'off'}",
                "dim",
            )
            models = [
                f"TypeSafe={os.environ.get('TYPESAFE_MODEL', 'jev-latest')}",
                f"text={os.environ.get('TEXT_MODEL', 'gpt-5-mini')}",
            ]
            if guided:
                models.append(f"guided={os.environ.get('JEV_GUIDED_MODEL', 'gpt-5-mini')}")
            if vision_recovery:
                models.append(f"vision={os.environ.get('JEV_VISION_MODEL', 'gpt-5-nano')}")
            say(f"   models: {' · '.join(models)}", "dim")
            with Heartbeat(describe):
                if guided:
                    progress["what"] = "refining the goal with OpenAI"
                    with self._lock:
                        if self._active_job_id != job_id or cancel_event.is_set():
                            return
                        self._last_progress_at = time.monotonic()
                        self._status.update(
                            message="Guided mode is refining the goal…",
                            updated_at=self._now(),
                        )
                    plan = self.goal_refiner(goal, start_url)
                    self.budget.record(openai_calls=1)
                    openai_calls += 1
                    guided_plans += 1
                    tokens_in += _tokens(plan.usage, _INPUT_KEYS)
                    tokens_out += _tokens(plan.usage, _OUTPUT_KEYS)
                    if plan.needs_clarification:
                        self._log_goal_plan(plan, goal)
                        raise ValueError(
                            f"Guided mode needs clarification: {plan.clarifying_question}"
                        )
                    execution_goal = plan.execution_goal(goal)
                    self._log_goal_plan(plan, goal)
                    with self._lock:
                        if self._active_job_id != job_id or cancel_event.is_set():
                            return
                        self._last_progress_at = time.monotonic()
                        self._status.update(
                            message="Guided plan ready; Jev is opening the page…",
                            guided_plans=1,
                            updated_at=self._now(),
                        )
                execution_goal = with_playback_rules(execution_goal)
                new_agent = self.agent_factory(url, execution_goal)
                with self._lock:
                    if self._active_job_id != job_id or cancel_event.is_set():
                        return
                    self._running_agent = new_agent
                    self._last_progress_at = time.monotonic()
                if self._stop_event.is_set() or cancel_event.is_set():
                    say("■  Stopped before it began (STOP & LOCK)", "yellow")
                    return
                self._activate(new_agent)
                progress["what"] = "reading the page"
                last_state: dict[str, Any] = {}
                limit_reason: str | None = None
                recovery_error: str | None = None
                stale_reason: str | None = None
                while True:
                    for state in new_agent.run():
                        if self._stop_event.is_set() or cancel_event.is_set():
                            say("■  Stopped by STOP & LOCK", "yellow")
                            return
                        last_state = state
                        decisions = state.get("decisions") or []
                        text_calls = state.get("text_calls") or []
                        new_decisions = decisions[decision_logged:]
                        new_text_calls = text_calls[text_logged:]
                        new_jev, new_openai = len(new_decisions), len(new_text_calls)
                        jev_calls += new_jev
                        openai_calls += new_openai
                        self.budget.record(new_jev, new_openai)
                        for index, decision in enumerate(new_decisions, decision_logged + 1):
                            usage = decision.get("usage") or {}
                            used_in = _tokens(usage, _INPUT_KEYS)
                            used_out = _tokens(usage, _OUTPUT_KEYS)
                            tokens_in += used_in
                            tokens_out += used_out
                            target = decision.get("target")
                            target_label = _decision_target_label(decision)
                            target_text = f" target={target}" if target is not None else ""
                            if target_label:
                                target_text += f" {target_label!r}"
                            target_confidence = decision.get("target_confidence")
                            target_confidence_text = (
                                f", target conf {target_confidence:.2f}"
                                if isinstance(target_confidence, int | float)
                                else ""
                            )
                            say(
                                f"   model {index:>2}  {decision.get('operation', '?')}"
                                f"{target_text} choice={decision.get('choice', '?')} "
                                f"[model {decision.get('model', '?')}, "
                                f"{decision.get('latency_ms', '?')}ms, "
                                f"op conf {(decision.get('confidence') or 0):.2f}"
                                f"{target_confidence_text}, "
                                f"{used_in:,} in/{used_out:,} out]",
                                "dim",
                            )
                        decision_logged = len(decisions)
                        for index, helper in enumerate(new_text_calls, text_logged + 1):
                            usage = helper.get("usage") or {}
                            used_in = _tokens(usage, _INPUT_KEYS)
                            used_out = _tokens(usage, _OUTPUT_KEYS)
                            tokens_in += used_in
                            tokens_out += used_out
                            say(
                                f"   text  {index:>2}  field={helper.get('field', '?')!r} "
                                f"value={helper.get('value', '')!r} "
                                f"[model {helper.get('model', '?')}, "
                                f"{helper.get('latency_ms', '?')}ms, "
                                f"{used_in:,} in/{used_out:,} out]",
                                "dim",
                            )
                        text_logged = len(text_calls)
                        history = state.get("history") or []
                        actions_before = logged
                        for entry in history[logged:]:
                            kind = str(entry.get("kind") or "?").upper()
                            typed = f'  ← "{entry["text"]}"' if entry.get("text") else ""
                            same = entry.get("page_changed") is False
                            moved = "  (page unchanged)" if same else ""
                            say(
                                f"   step {entry.get('step'):>2}  {kind:<6} "
                                f"{entry.get('action')}{typed}{moved}  "
                                f"[{entry.get('latency_ms')}ms, "
                                f"conf {(entry.get('confidence') or 0):.2f}]"
                            )
                            progress["what"] = f"{kind} {entry.get('action')}"
                        logged = len(history)
                        if logged > actions_before:
                            consecutive_stale = 0
                            latest_url = _safe_url(history[-1].get("url"))
                            say(
                                f"      page: {latest_url} · changed="
                                f"{history[-1].get('page_changed')}",
                                "dim",
                            )
                        elif new_jev and state.get("status") == "ready":
                            consecutive_stale += new_jev
                            stale_retries += new_jev
                            page = state.get("page") or {}
                            say(
                                f"   ↻ prediction not executed: page changed before input; "
                                f"re-observed {_safe_url(page.get('url'))} "
                                f"({consecutive_stale} consecutive)",
                                "yellow",
                            )
                        progress["steps"] = logged
                        detail = history[-1].get("action") if history else "Inspecting the page"
                        with self._lock:
                            if self._active_job_id != job_id:
                                return
                            self._last_progress_at = time.monotonic()
                            self._status.update(
                                message=detail,
                                steps=len(history),
                                stale_rejections=stale_retries,
                                elapsed_ms=state.get("elapsed_ms", 0),
                                updated_at=self._now(),
                            )
                        if (
                            self.max_stale_decisions
                            and consecutive_stale >= self.max_stale_decisions
                        ):
                            stale_reason = (
                                f"Jev made {consecutive_stale} consecutive predictions, but the "
                                "page changed before any could execute"
                            )
                            say(f"⚠  STALE-PAGE LOOP — {stale_reason}", "yellow")
                            break
                        if state.get("status") not in {"done", "blocked"} and (
                            limit_reason := self.budget.exceeded(logged)
                        ):
                            break

                    final = last_state.get("status")
                    should_recover = (
                        vision_recovery
                        and vision_recoveries == 0
                        and (final == "blocked" or stale_reason is not None)
                        and not limit_reason
                    )
                    if not should_recover:
                        break
                    if limit_reason := self.budget.exceeded(logged):
                        break
                    try:
                        progress["what"] = "asking OpenAI for visual recovery advice"
                        with self._lock:
                            if self._active_job_id != job_id:
                                return
                            self._last_progress_at = time.monotonic()
                            self._status.update(
                                message="Jev stalled; analyzing one screenshot…",
                                vision_recoveries=1,
                                updated_at=self._now(),
                            )
                        screenshot = self._capture_screenshot(new_agent)
                        page = last_state.get("page") or {}
                        history = last_state.get("history") or []
                        advice = self.vision_analyzer(execution_goal, screenshot, page, history)
                        self._resume_with_vision_advice(new_agent, execution_goal, advice)
                        usage = advice.usage
                        tokens_in += _tokens(usage, _INPUT_KEYS)
                        tokens_out += _tokens(usage, _OUTPUT_KEYS)
                        openai_calls += 1
                        vision_recoveries += 1
                        consecutive_stale = 0
                        stale_reason = None
                        self.budget.record(openai_calls=1)
                        say(
                            f"◉  VISION RECOVERY ({advice.model}) — {advice.explanation}",
                            "cyan",
                        )
                        with self._lock:
                            if self._active_job_id != job_id:
                                return
                            self._last_progress_at = time.monotonic()
                            self._status.update(
                                message="Vision supplied a different approach; Jev is retrying…",
                                updated_at=self._now(),
                            )
                    except Exception as exc:
                        recovery_error = self._friendly_error(exc)
                        say(f"⚠  VISION RECOVERY FAILED — {recovery_error}", "yellow")
                        break

            cost = _cost(tokens_in, tokens_out)
            cost_text = f"${cost:.4f}" if cost is not None else "cost n/a (set JEV_PRICE_*_PER_1M)"
            summary = (
                f"{logged} steps · {jev_calls} TypeSafe + {openai_calls} OpenAI calls · "
                f"{guided_plans} guided plans · "
                f"{vision_recoveries} vision recoveries · {stale_retries} stale retries · "
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
                if (
                    self._stop_event.is_set()
                    or cancel_event.is_set()
                    or self._active_job_id != job_id
                ):
                    return
                old_agent, self._agent = self._agent, new_agent
                adopted = True
                self._running_agent = None
                self._active_job_id = None
                self._job_cancel_event = None
                self._status.update(
                    state="done" if final == "done" and not limit_reason else "error",
                    message=(
                        limit_reason
                        or (
                            f"Vision recovery failed: {recovery_error}"
                            if recovery_error
                            else None
                        )
                        or stale_reason
                        or (
                            "Command completed"
                            if last_state.get("status") == "done"
                            else (
                                "Jev could not make progress or confirm the goal. "
                                "Review the open tab and try a more specific command."
                            )
                        )
                    ),
                    updated_at=self._now(),
                )
            if old_agent:
                self._close_agent(old_agent, "previous browser tab")
            if then_fullscreen and final == "done" and not limit_reason:
                self._auto_fullscreen()
        except Exception as exc:
            with self._lock:
                stopped = self._stop_event.is_set()
                current = self._active_job_id == job_id
                message = self._friendly_error(exc)
                if not stopped and current:
                    self._active_job_id = None
                    self._job_cancel_event = None
                    if self._running_agent is new_agent:
                        self._running_agent = None
                    self._status.update(
                        state="error",
                        message=message,
                        error_code="execution_failed",
                        retryable=True,
                        updated_at=self._now(),
                    )
            if not stopped and current:
                say(f"✖  ERROR after {elapsed()}s — {type(exc).__name__}: {exc}", "red")
        finally:
            with self._lock:
                if self._running_agent is new_agent:
                    self._running_agent = None
            if new_agent and not adopted:
                self._close_agent(new_agent, "failed browser tab")

    def _capture_screenshot(self, agent: AgentLike) -> str:
        self._activate(agent)
        result = agent.browser.call(
            "Page.captureScreenshot",
            format="jpeg",
            quality=60,
            captureBeyondViewport=False,
        )
        screenshot = result.get("data") if isinstance(result, dict) else None
        if not isinstance(screenshot, str) or not screenshot:
            raise RuntimeError("Chrome returned no screenshot for vision recovery")
        return screenshot

    @staticmethod
    def _resume_with_vision_advice(
        agent: AgentLike, original_goal: str, advice: RecoveryAdvice
    ) -> None:
        state = getattr(agent, "state", None)
        if not isinstance(state, dict) or state.get("status") not in {"ready", "blocked"}:
            raise RuntimeError("This Jev version cannot resume a blocked task with vision advice")
        state["goal"] = (
            f"{original_goal}\n\n"
            "Advisory recovery context from a screenshot (not a new goal and not proof): "
            f"{advice.guidance}\n"
            "Re-observe the structured page state, choose only an offered operation and target, "
            "and do not repeat an action that left the page unchanged."
        )
        state["decision"] = None
        state["status"] = "ready"

    @staticmethod
    def _log_goal_plan(plan: GoalPlan, original_goal: str) -> None:
        usage = plan.usage
        say(f"◇  GUIDED PLAN ({plan.model})", "cyan", rule=True)
        say(f"   original: {original_goal}")
        say(f"   objective: {plan.refined_goal}")
        for heading, values in (
            ("requirements", plan.requirements),
            ("success criteria", plan.success_criteria),
            ("constraints", plan.constraints),
        ):
            say(f"   {heading}:", "bold")
            if values:
                for value in values:
                    say(f"     • {value}")
            else:
                say("     • none", "dim")
        say(f"   needs clarification: {plan.needs_clarification}")
        if plan.clarifying_question:
            say(f"   question: {plan.clarifying_question}")
        say(
            f"   usage: {_tokens(usage, _INPUT_KEYS):,} input / "
            f"{_tokens(usage, _OUTPUT_KEYS):,} output tokens",
            "dim",
        )
        say("─" * 64, "dim")

    def _watch_job(self, job_id: int, cancel_event: threading.Event) -> None:
        while not cancel_event.wait(self.watchdog_interval):
            with self._lock:
                if self._active_job_id != job_id:
                    return
                now = time.monotonic()
                total = now - self._job_started_at
                stalled = now - self._last_progress_at
                if total >= self.command_timeout:
                    reason = (
                        f"Task stopped after {self.command_timeout:g}s. "
                        "It ran longer than the command deadline. Review the screen, then retry."
                    )
                elif stalled >= self.stall_timeout:
                    reason = (
                        f"Task stopped after {self.stall_timeout:g}s without progress. "
                        "The stalled tab was closed. Review the screen, then retry."
                    )
                else:
                    continue
                cancel_event.set()
                agent = self._running_agent
                self._running_agent = None
                self._active_job_id = None
                self._job_cancel_event = None
                self._status.update(
                    state="error",
                    message=reason,
                    error_code="timeout",
                    retryable=True,
                    updated_at=self._now(),
                )
            say(f"✖  WATCHDOG — {reason}", "red")
            if agent:
                self._close_agent(agent, "stalled browser tab")
            return

    @staticmethod
    def _close_agent(agent: AgentLike, label: str) -> None:
        try:
            agent.close()
        except Exception as exc:
            say(f"⚠  Could not close {label} — {exc}", "yellow")

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        detail = str(exc).strip()
        lowered = detail.casefold()
        if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
            return "A model or browser request timed out. Review the screen, then try again."
        if "401" in detail or "403" in detail or "api key" in lowered:
            return "A model provider rejected its API key. Check the keys in .env and restart Jev."
        if "connection" in lowered or "connect" in lowered:
            return (
                "Jev could not reach the model provider or browser. "
                "Check the connection and retry."
            )
        return detail or f"{type(exc).__name__} while running the command"

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
            if self._job_cancel_event:
                self._job_cancel_event.set()
            agents: list[AgentLike] = []
            for candidate in (self._agent, self._running_agent):
                if candidate and all(candidate is not agent for agent in agents):
                    agents.append(candidate)
            self._agent = None
            self._running_agent = None
            self._active_job_id = None
            self._job_cancel_event = None
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
