"""Gateway admission while the turn's provider is at a usage limit (``agent.usage_hold``).

A turn routed to a provider whose plan limit is known to be closed would only burn a request
and fail the same way. With a fallback chain configured the turn runs as usual and the normal
fallback takes over; without one the gateway answers at once, naming the local reset time, and
never calls the provider.

When the user opted in to reruns at the reset (``desktop.quota_resume.enabled``, the same switch
that arms the Desktop/TUI rerun), the refused message is queued durably per profile in
``<HERMES_HOME>/usage_hold_reruns.json`` and re-sent once at the reset as a ``/retry`` from the
original chat. The rules the queue keeps:

* At most once: an entry is claimed (written ``claimed``) before it is dispatched, and a claimed
  entry is never dispatched again, so a crash mid-dispatch loses the rerun instead of doubling it.
* Only a turn whose outcome is known: the held turn never reached the provider. At the reset the
  session must still be the same session id and its transcript tail must still be this held turn's
  boundary row; any newer activity (a reply, ``/new``, a turn on another model) drops the rerun.
* Bounded: one pending rerun per session (a newer held message replaces it, and the rerun's own
  history still carries the older one), ``max_attempts`` consecutive reruns, and nothing queued
  past ``agent.quota_resume.DEFAULT_MAX_WAIT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

QUEUE_FILE = "usage_hold_reruns.json"
QUEUED_NOTICE = "This message is queued and runs automatically once the usage limit resets."
TICK_SECONDS = 20.0
# A claimed entry only remembers the attempt count for the rerun it started.
_CLAIMED_TTL_SECONDS = 3600.0
# The boundary row is written moments after the entry; a wider gap is a different turn.
_BOUNDARY_SLACK_SECONDS = 300.0
_LOCK = threading.Lock()


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _rerun_settings(user_config: Any) -> tuple[bool, int]:
    from agent.quota_resume import RERUN_ENABLED_DEFAULT, RERUN_MAX_ATTEMPTS_DEFAULT
    from utils import is_truthy_value

    desktop = user_config.get("desktop") if isinstance(user_config, dict) else None
    cfg = desktop.get("quota_resume") if isinstance(desktop, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        attempts = max(0, int(cfg.get("max_attempts", RERUN_MAX_ATTEMPTS_DEFAULT)))
    except (TypeError, ValueError):
        attempts = RERUN_MAX_ATTEMPTS_DEFAULT
    return is_truthy_value(cfg.get("enabled"), default=RERUN_ENABLED_DEFAULT), attempts


def _rerunnable(ctx: Any) -> bool:
    """A person's plain-text messaging-chat turn: what ``/retry`` can re-send faithfully. A LOCAL
    route (one-shot, ``chat -q``, TUI attach) has no inbound handler to re-send into and its client
    is gone by the reset; Desktop/TUI sessions own their rerun in ``tui_gateway``."""
    from gateway.config import Platform

    return bool(
        ctx.session_key and ctx.session_id and ctx.message
        and getattr(ctx.source, "platform", None) not in (None, Platform.LOCAL)
        and not ctx.scheduled_heartbeat and not ctx.persist_user_display_kind
        and isinstance(ctx.persist_user_message if ctx.persist_user_message is not None else ctx.message, str)
    )


def queue_rerun(ctx: Any, hold: dict[str, Any], *, now: Optional[float] = None) -> Optional[float]:
    """Queue this held turn to run once at the reset. Returns the rerun time, or None when the
    user has not opted in or a bound refuses it."""
    from agent.quota_resume import DEFAULT_GRACE_SECONDS, DEFAULT_MAX_WAIT_SECONDS

    enabled, max_attempts = _rerun_settings(ctx.user_config)
    if not enabled or not _rerunnable(ctx):
        return None
    now = time.time() if now is None else now
    resume_at = float(hold["until"]) + DEFAULT_GRACE_SECONDS
    if resume_at - now > DEFAULT_MAX_WAIT_SECONDS:
        return None
    path = get_hermes_home() / QUEUE_FILE
    with _LOCK:
        data = _load(path)
        prior = data.get(ctx.session_key) or {}
        # A held rerun is the same request held again: count it, so a wall that keeps moving ends.
        attempt = int(prior.get("attempt", 0)) + 1 if prior.get("state") == "claimed" else 1
        if attempt > max_attempts:
            data.pop(ctx.session_key, None)
            atomic_json_write(path, data)
            return None
        data[ctx.session_key] = {
            "session_key": ctx.session_key, "session_id": ctx.session_id, "state": "pending",
            "resume_at": resume_at, "queued_at": now, "attempt": attempt,
            "provider": str(hold.get("provider") or ""),
        }
        atomic_json_write(path, data)
    logger.info("Usage limit: queued session %s to rerun at %s (attempt %d/%d)", ctx.session_key,
                time.strftime("%H:%M", time.localtime(resume_at)), attempt, max_attempts)
    return resume_at


def held_turn_result(runner: Any, ctx: Any, runtime_kwargs: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The turn's answer while its provider is held, or None to run the turn normally."""
    from agent.usage_hold import active_hold

    hold = active_hold(runtime_kwargs.get("provider"), runtime_kwargs.get("base_url") or "")
    if hold is None or runner._refresh_fallback_model():
        return None
    from agent.turn_failure_copy import limit_reset_copy, provider_label_for, stamp_failure

    rerun_at = queue_rerun(ctx, hold)
    lead = (f"{provider_label_for(hold.get('provider')) or 'This provider'} is at its usage limit, so this "
            f"message was not sent to it. {limit_reset_copy(float(hold['until']))}").strip()
    result = {
        "final_response": lead, "messages": [], "api_calls": 0, "tools": [], "failed": True,
        "completed": False, "error": "provider usage limit hold", "failure_resets_at": float(hold["until"]),
        # The agent never ran, so nothing flushed the user row: the gateway writes it.
        "agent_persisted": False,
    }
    if rerun_at is not None:
        result["failed_turn_notice"] = QUEUED_NOTICE
    logger.info("Usage limit: held turn for session %s (%s until %s), rerun=%s", ctx.session_key,
                hold.get("provider"), time.strftime("%H:%M", time.localtime(float(hold["until"]))),
                rerun_at is not None)
    return stamp_failure(result, "rate_limit", True)


# ----------------------------------------------------------------- rerun at the reset


def _session_entry(runner: Any, session_key: str) -> Any:
    store = runner.session_store
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries.get(session_key)


def _tail_is_held_turn(transcript: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
    rows = [r for r in transcript if r.get("role") in {"user", "assistant", "tool"}]
    if len(rows) < 2 or rows[-2].get("role") != "user":
        return False
    tail = rows[-1]
    if tail.get("role") != "assistant" or str(tail.get("content") or "").strip() != QUEUED_NOTICE:
        return False
    stamp = tail.get("timestamp")
    queued_at = float(entry.get("queued_at") or 0)
    return not isinstance(stamp, (int, float)) or queued_at - 5 <= stamp <= queued_at + _BOUNDARY_SLACK_SECONDS


async def _rerun_target(runner: Any, entry: dict[str, Any]) -> tuple[str, Any, Any]:
    """``(verdict, adapter, source)``: ``run``, ``wait`` (adapter offline: keep it pending) or a
    drop reason (the turn identity no longer holds). Every identity check runs after the last
    await, so nothing can start a newer turn between the checks and the dispatch."""
    key = entry["session_key"]
    session = _session_entry(runner, key)
    source = runner._restored_source(session) if session is not None else None
    if source is None:
        return "no session or origin to reply to", None, None
    async with runner._async_profile_scope_for_source(source):
        transcript = await runner.async_session_store.load_transcript(session.session_id)
    session = _session_entry(runner, key)
    if session is None or session.session_id != entry.get("session_id"):
        return "session changed", None, None
    if runner._is_session_running(key):
        return "a newer turn is running", None, None
    if not _tail_is_held_turn(transcript or [], entry):
        return "the conversation moved on", None, None
    if runner._session_key_for_source(source) != key:
        # A re-sent /retry from this origin would open another session (per-delivery webhook
        # routes): it could only answer "no previous message", never rerun this turn.
        return "the origin no longer routes to this session", None, None
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return "wait", None, None
    if not runner._resume_owner_authorized(key, source):
        return "sender no longer authorized", None, None
    return "run", adapter, source


def _claim(path: Path, key: str, queued_at: Any) -> bool:
    """Flip one pending entry to claimed on disk; False when it changed or is gone (at most once)."""
    with _LOCK:
        data = _load(path)
        entry = data.get(key)
        if not entry or entry.get("state") != "pending" or entry.get("queued_at") != queued_at:
            return False
        entry.update(state="claimed", claimed_at=time.time())
        atomic_json_write(path, data)
    return True


def _drop(path: Path, key: str, queued_at: Any) -> None:
    with _LOCK:
        data = _load(path)
        if (data.get(key) or {}).get("queued_at") == queued_at:
            data.pop(key, None)
            atomic_json_write(path, data)


def _due(path: Path, now: float) -> list[dict[str, Any]]:
    """Pending entries at or past their rerun time; expired bookkeeping is pruned on the way."""
    from agent.quota_resume import DEFAULT_MAX_WAIT_SECONDS

    with _LOCK:
        data = _load(path)
        live = {
            k: v for k, v in data.items()
            if (v.get("state") == "claimed" and now - float(v.get("claimed_at") or 0) < _CLAIMED_TTL_SECONDS)
            or (v.get("state") == "pending" and now - float(v.get("queued_at") or 0) < DEFAULT_MAX_WAIT_SECONDS * 2)
        }
        if live != data:
            atomic_json_write(path, live)
    return [v for v in live.values() if v.get("state") == "pending" and float(v.get("resume_at") or 0) <= now]


async def rerun_due(runner: Any, homes: list[Path], *, now: Optional[float] = None) -> int:
    """One pass over every served profile's queue: re-send each due, still-valid held turn."""
    from gateway.platforms.event import MessageEvent, MessageType

    sent = 0
    for home in homes:
        path = Path(home) / QUEUE_FILE
        if not path.exists():
            continue
        for entry in _due(path, time.time() if now is None else now):
            verdict, adapter, source = await _rerun_target(runner, entry)
            if verdict == "wait":
                continue
            if verdict != "run":
                logger.info("Usage limit: dropping the queued rerun for %s: %s", entry["session_key"], verdict)
                _drop(path, entry["session_key"], entry.get("queued_at"))
                continue
            if not _claim(path, entry["session_key"], entry.get("queued_at")):
                continue
            logger.info("Usage limit reset: re-sending the held message for %s", entry["session_key"])
            await adapter.handle_message(MessageEvent(text="/retry", message_type=MessageType.TEXT, source=source))
            sent += 1
    return sent


async def rerun_watcher(runner: Any) -> None:
    """Supervised loop: reruns held turns at their reset across every served profile."""
    from gateway.run_idle_exit import _served_homes

    while not runner._shutdown_event.is_set():
        try:
            await rerun_due(runner, _served_homes(runner))
        except Exception:
            logger.warning("Usage-limit rerun pass failed", exc_info=True)
        try:
            await asyncio.wait_for(runner._shutdown_event.wait(), timeout=TICK_SECONDS)
        except TimeoutError:
            pass
