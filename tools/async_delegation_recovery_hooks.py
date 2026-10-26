"""``subagent_stop`` for children whose owning process died before their unit joined.

A background unit fires ``subagent_stop`` at its join; an owner killed before then never fires it, so a plugin
pairing ``subagent_start``/``subagent_stop`` kept the child "running" forever. Restart recovery
(``recover_abandoned_delegations``) replays the stop from the identity persisted at dispatch: a child the unit had
already recorded reports its real result, the rest ``child_status="unknown"``. Every recovered stop carries
``recovered=True``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def recovered_stop_payloads(
    task: dict[str, Any], parent_session_id: Optional[str], results: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """One ``subagent_stop`` kwargs dict per child the unit started (rows dispatched without
    ``task_children`` started none that recovery can name)."""
    from tools.delegate_tool_results import subagent_stop_outcome_fields

    recorded = {r.get("task_index"): r for r in results or () if isinstance(r, dict)}
    payloads = []
    for key, identity in sorted((task.get("task_children") or {}).items(), key=lambda kv: int(kv[0])):
        index = int(key)
        entry = {"model": task.get("model"), "cost_status": "unknown",
                 **(recorded.get(index) or {"task_index": index, "status": "unknown"})}
        payloads.append({
            "parent_session_id": parent_session_id, "parent_turn_id": "", "task_index": index, **identity,
            **subagent_stop_outcome_fields(entry), "recovered": True,
        })
    return payloads


def fire_recovered_stops(payloads: list[dict[str, Any]]) -> None:
    """Invoke ``subagent_stop`` per payload; runs after the recovery transaction commits, under the
    ledger owner's profile scope (recovery callers bind it), so the profile's own plugins receive them."""
    if not payloads:
        return
    from hermes_cli.plugins import invoke_hook

    for payload in payloads:
        try:
            invoke_hook("subagent_stop", **payload)
        except Exception:
            logger.debug("recovered subagent_stop hook invocation failed", exc_info=True)
