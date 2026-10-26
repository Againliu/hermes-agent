"""Platform admins answer another member's approval under the ``approvals.approvers: []`` opt-in.

Discord buttons and relay prompt answers honour ``allow_admin_from`` / ``group_allow_admin_from``
the same way the text ``/approve`` does, without ``require_admin_for_exec_approval`` turned on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_approval_requester_binding import pending_from, write_approvers


@pytest.fixture(autouse=True)
def _clean_queues():
    from tools import approval as approval_mod

    approval_mod._gateway_queues.clear()
    yield
    approval_mod._gateway_queues.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(("clicker", "allowed"), [("22", True), ("33", False)])
async def test_discord_admin_answers_with_require_admin_off(clicker, allowed, monkeypatch):
    from tests.gateway.test_discord_exec_approval_content import _capture_channel
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    write_approvers([])
    entry = pending_from("11", "Alice", "discord:555", platform="discord")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***", extra={"allow_admin_from": ["22"]}))
    sent = _capture_channel(adapter)
    await adapter.send_exec_approval(chat_id="555", command="rm -rf build", session_key="discord:555")
    view = sent["view"]
    view._finalize_embed = AsyncMock()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=int(clicker), display_name="Bob", roles=[]),
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
        message=SimpleNamespace(embeds=[]), channel_id=555)

    await view._resolve(interaction, "once", None, "platform.discord.approval.resolved_once")

    assert entry.event.is_set() is allowed
    if not allowed:
        interaction.response.send_message.assert_awaited_once_with(
            "Only Alice can answer this approval.", ephemeral=True)

