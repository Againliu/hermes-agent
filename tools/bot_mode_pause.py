"""Bot Mode pause: a bot whose user paused it stops working on its own.

The flag is ``ui_meta['hermes-bots'].paused`` in the bot's own ``profile.yaml``: the Bot Mode
metadata the Desktop already writes (``profiles.configure``) and syncs to every desktop, read
here from the profile home, so each profile's pause stays its own (profiles are islands).

While paused:

- its scheduled jobs do not fire. Each recurring slot that falls due is consumed (its
  ``next_run_at`` advances) instead of run, so a resume never bursts the missed runs; a due
  one-shot is held and meets the scheduler's ordinary one-shot grace after the resume.
- a teammate's ``message_agent`` delivery is refused with :data:`RECIPIENT_PAUSED`
  (``gateway/session_bot.py``), a definite no-send the sender is told about.

The user can still open and chat with a paused bot: pause stops the work the bot does on its
own (schedules, teammate traffic), not the human's own conversation. ``hermes pause`` (the
global emergency stop) is the switch that holds every turn. Hidden stays display-only.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from tools.bot_failure_reasons import RECIPIENT_PAUSED

__all__ = ["RECIPIENT_PAUSED", "hold_paused_profile_jobs", "is_bot_paused"]

# Profile homes whose pause was logged already: once per engagement, re-armed on resume.
_logged_lock = threading.Lock()
_logged_homes: set[str] = set()


def is_bot_paused(home: str | os.PathLike) -> bool:
    """True when the profile at ``home`` is a paused Bot Mode bot. A missing or unreadable
    ``profile.yaml`` is not paused: pause is an opt-in the user set, never a failure mode."""
    from tools.bot_mode_probe import _bots_meta, _read_yaml_dict

    meta = _bots_meta(_read_yaml_dict(Path(home) / "profile.yaml", "paused"))
    return bool(meta) and meta.get("paused") is True


def _note_paused(home: Path, paused: bool, logger: logging.Logger) -> None:
    key = str(home)
    with _logged_lock:
        first = paused and key not in _logged_homes
        if paused:
            _logged_homes.add(key)
        else:
            _logged_homes.discard(key)
    if first:
        logger.info("Cron dispatch held for %s: its bot is paused (due slots are skipped, not run)", home)


def hold_paused_profile_jobs(due_jobs: list, logger: logging.Logger) -> list:
    """The due jobs the tick may run for the profile in scope: all of them, or none while that
    profile's bot is paused. Called under the tick lock with the profile's home and cron store
    bound (``_profile_cron_scope``); a paused profile's recurring jobs are advanced past their
    due slot so the resume finds nothing overdue."""
    from cron.constants import is_recurring
    from cron.jobs import advance_next_runs
    from cron.scheduler_gateway_gate import _note_fire_refused
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    paused = is_bot_paused(home)
    _note_paused(home, paused, logger)
    if not paused or not due_jobs:
        return due_jobs
    # Never handed to the dispatcher: drop the due scan's pending-slot mark (else the next scan
    # "restores" the slot as a lost dispatch) and stamp why, as the headless gate does.
    _note_fire_refused([str(job["id"]) for job in due_jobs], "Skipped: this bot is paused")
    advance_next_runs([job["id"] for job in due_jobs if is_recurring(job)])
    return []
