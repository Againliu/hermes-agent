"""In a shared chat, an exec approval is answered by the user whose message raised it.

Another admitted member's /approve (or a button click, which runs the same check) gets
"Only <requester> can answer this approval." and the command stays pending.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _runner(extra=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(
        enabled=True, token="***", extra=extra or {})})
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._pending_approvals = {}
    runner._running_agents = {}
    runner._background_tasks = set()
    return runner


def _event(text, user_id, user_name):
    # thread_id makes this a shared thread session: everyone in it maps to one session key.
    return MessageEvent(text=text, message_id="m1", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="-100group", chat_type="group", thread_id="t1",
        user_id=user_id, user_name=user_name))


def _pending_from(user_id, user_name, session_key, approvers=None):
    """Enqueue an approval the way the agent thread does, inside the requester's turn."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval as approval_mod
    from tools.approval_gateway_wait import _ApprovalEntry

    tokens = set_session_vars(platform="telegram", user_id=user_id, user_name=user_name, session_key=session_key)
    try:
        entry = _ApprovalEntry({"command": "rm -rf build", **({"approvers": approvers} if approvers else {})})
    finally:
        clear_session_vars(tokens)
    approval_mod._gateway_queues.setdefault(session_key, []).append(entry)
    return entry


@pytest.fixture(autouse=True)
def _clean_queues():
    from tools import approval as approval_mod

    approval_mod._gateway_queues.clear()
    yield
    approval_mod._gateway_queues.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(("clicker", "extra", "approvers", "allowed"), [
    ("bob", None, None, False),                                   # another member
    ("alice", None, None, True),                                  # the requester
    ("bob", None, ["bob"], True),                                 # approvals.approvers
    ("bob", None, ["*"], True),                                   # explicit "anyone"
    ("bob", {"group_allow_admin_from": ["bob"]}, None, True),     # platform admin
])
async def test_only_requester_or_approver_answers_a_shared_chat_approval(clicker, extra, approvers, allowed):
    runner = _runner(extra)
    event = _event("/approve", clicker, clicker.title())
    session_key = runner._session_key_for_source(event.source)
    assert session_key == runner._session_key_for_source(_event("x", "alice", "Alice").source)
    entry = _pending_from("alice", "Alice", session_key, approvers)

    reply = await runner._handle_approve_command(event)

    assert entry.event.is_set() is allowed
    if not allowed:
        assert reply == "Only Alice can answer this approval."
        assert entry.result is None
