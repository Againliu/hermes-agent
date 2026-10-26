"""Bind a group chat's channel memory for one gateway turn (see tools/memory_tool_channel.py).

Who may write the owner's MEMORY.md / USER.md from a group: a sender the gateway would also
let DM the agent. Those files already load in every DM that sender can open, so the write
crosses no boundary. Anyone else (a member of an allowlisted group who could not DM the
agent) writes the chat's channel memory instead; this is the cross-user write that used to
land in the owner's files and then load in the owner's private DMs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.session_identity import replace_source
from tools.memory_tool_channel import (
    CHANNEL_MEMORY, ChannelMemoryBinding, channel_char_limit, channel_memory_config, safe_scope)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_DM_CHAT_TYPES = frozenset({"dm", "direct", "private", ""})


def _scope_key(source: Any, mode: str) -> Optional[str]:
    """Chat key: a thread shares its parent chat's notes; ``workspace`` widens the scope to
    the Slack team / Discord guild when the platform reports one."""
    chat = (source.parent_chat_id if source.thread_id and source.parent_chat_id else None) or source.chat_id
    workspace = getattr(source, "scope_id", None)
    if mode == "workspace" and workspace:
        return str(workspace)
    if not chat:
        return None
    return f"{workspace}_{chat}" if workspace else str(chat)


def _sender_can_dm(runner: Any, source: Any) -> bool:
    """Would the gateway admit this sender in a DM? Asked of the bot that received the group
    message (a shared bot may route the group into a profile with no allowlist of its own).
    Fails closed: an authorization error must not reopen the owner's files to a group member."""
    if not source.user_id:
        return False
    dm_source = replace_source(source, chat_type="dm", chat_id=str(source.user_id), thread_id=None,
                               parent_chat_id=None, chat_name=None)
    try:
        return bool(runner._under_authorization_profile(
            dm_source, lambda: runner._principal_authorized(dm_source, allow_adapter_delegation=True)))
    except Exception:
        logger.warning("channel memory: DM authorization check failed; treating sender as a group member",
                       exc_info=True)
        return False


def channel_memory_binding(runner: Any, source: Any, config: dict[str, Any]) -> Optional[ChannelMemoryBinding]:
    """This turn's binding, or None for DMs, local sessions and when channel memory is off."""
    from gateway.config import Platform

    cfg = channel_memory_config(config)
    if not is_truthy_value(cfg.get("enabled"), default=True):
        return None
    if source.platform == Platform.LOCAL or str(source.chat_type or "").lower() in _DM_CHAT_TYPES:
        return None
    key = _scope_key(source, str(cfg.get("scope") or "chat").lower())
    if key is None:
        return None
    owner_writes = (is_truthy_value(cfg.get("owner_writes_from_groups"), default=True)
                    and _sender_can_dm(runner, source))
    return ChannelMemoryBinding(scope=safe_scope(source.platform.value, key), owner_writes=owner_writes,
                                char_limit=channel_char_limit(config))


def bind_channel_memory(runner: Any, source: Any) -> None:
    """Set (or clear) the binding for the current turn. Always written, so a queued DM turn
    handled by the same task never inherits a previous group turn's binding."""
    from gateway.run import _load_gateway_config

    CHANNEL_MEMORY.set(channel_memory_binding(runner, source, _load_gateway_config() or {}))
