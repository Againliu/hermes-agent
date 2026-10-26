"""Owner-write permission in a group chat: evaluated as a real DM by the receiving bot, never
borrowed from a group-only grant, and never granted where other members share the session."""

import contextvars

import pytest

import gateway.run as gateway_run
from gateway.channel_memory import bind_channel_memory, channel_memory_binding
from gateway.config import Platform
from gateway.session import SessionSource
from tools import memory_tool as mt
from tools.memory_tool_channel import CHANNEL_MEMORY, ChannelMemoryBinding


class _Runner:
    """Admits a DM only under the receiving bot's profile home, as the gateway does when a
    shared bot routes a group into another profile (the runtime profile has no allowlist)."""

    def __init__(self, bot_home="/homes/receiving-bot"):
        self.bot_home = bot_home
        self.active_home = None

    def _under_authorization_profile(self, source, check):
        previous, self.active_home = self.active_home, getattr(source, "_authorization_profile_home", None)
        try:
            return check()
        finally:
            self.active_home = previous

    def _principal_authorized(self, source, *, allow_adapter_delegation):
        if allow_adapter_delegation and source.role_authorized is True:
            return True  # what gateway/authz_mixin.py::_principal_authorized does with the flag
        return source.chat_type == "dm" and source.user_id == "owner" and self.active_home == self.bot_home


def _group(user_id, **extra):
    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="group", user_id=user_id, **extra)
    source._authorization_profile_home = "/homes/receiving-bot"
    return source


def test_owner_is_authorized_by_the_receiving_bot_not_the_runtime_profile():
    assert channel_memory_binding(_Runner(), _group("owner"), {}).owner_writes is True


def test_group_role_grant_does_not_count_as_dm_access():
    # A member admitted to the guild channel only by DISCORD_ALLOWED_ROLES cannot DM the bot by
    # default, so it must not write the owner's files either.
    assert channel_memory_binding(_Runner(), _group("member", role_authorized=True), {}).owner_writes is False
