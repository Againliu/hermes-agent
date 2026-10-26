"""Managed-worker turns in the gateway's memory-aware admission (``gateway/admission_memory.py``).

A managed session (a bypass/config-only session, a kanban card) starts a worker process instead of
running in the gateway, so it never passes the in-process message handler's memory hold. It waits
here, before the worker spawns: a kanban card as background work, any other managed session as a
person. A Stop while it waits falls through to the spawn path, which records the interruption.
"""

from __future__ import annotations

from typing import Optional


async def hold_managed_for_memory(authority, ref, row, policy) -> Optional[str]:
    """None to spawn the worker; else the refusal that answers the turn (memory never freed)."""
    from gateway.admission_memory import BACKGROUND, PERSON, get_gate, wait_async
    from gateway.session_context_refs import refused_turn_result
    from agent.i18n import t

    generation = row.get('generation')

    def still_wanted() -> bool:
        stops = getattr(authority, 'pending_stops', {})
        return stops.get(ref.session_id) != generation and authority.executing.get(ref.session_id) == generation

    kind = BACKGROUND if policy.kanban_json is not None else PERSON
    if await wait_async(kind, on_hold=lambda: None, still_wanted=still_wanted) != 'timeout':
        return None
    refusal = t('gateway.busy.memory_gave_up', minutes=max(1, round(get_gate().settings.max_wait_seconds / 60)))
    authority.pending_results[row['admission_id']] = {'result': refused_turn_result(row, refusal), 'usage': {}}
    return refusal
