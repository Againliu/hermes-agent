"""Fallback review contracts: a failed outage probe returns to the backup at once, only the real
primary route re-benches the primary, and effort bookkeeping belongs to the route that owns it."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import _leaving_primary


def test_failed_outage_probe_skips_the_healthy_pool_wait():
    from agent.turn_recovery import _pool_blocks_eager_fallback

    agent = SimpleNamespace(_primary_probe_pending=True, _credential_pool=object())
    with patch("run_agent._pool_may_recover_from_rate_limit", return_value=True, create=True), \
            patch("agent.conversation_loop._ra", return_value=SimpleNamespace(
                _pool_may_recover_from_rate_limit=lambda pool: True)):
        assert _pool_blocks_eager_fallback(agent, FailoverReason.overloaded) is False
        agent._primary_probe_pending = False
        assert _pool_blocks_eager_fallback(agent, FailoverReason.rate_limit) is True


def test_a_fallback_on_the_primarys_provider_is_not_the_primary():
    primary = {"provider": "openrouter", "model": "primary/model"}
    on_backup = SimpleNamespace(_fallback_activated=True, provider="openrouter", model="backup/model",
                                _primary_runtime=primary)
    assert _leaving_primary(on_backup) is False
    on_primary = SimpleNamespace(_fallback_activated=True, provider="openrouter", model="primary/model",
                                 _primary_runtime=primary)
    assert _leaving_primary(on_primary) is True


def test_shared_snapshot_installer_leaves_fallback_bookkeeping_alone():
    """A voice route restore must not consume the surrounding fallback's bound effort."""
    from agent.route_binding import reinstall_runtime_snapshot

    agent = SimpleNamespace(reasoning_config={"effort": "low"}, _fallback_bound_reasoning={"effort": "low"})
    rt = {"reasoning_config": {"effort": "low"}, "model": "m", "provider": "p"}
    with patch("agent.agent_runtime_helpers._apply_primary_runtime_fields"), \
            patch("agent.turn_recovery_codex.reset_codex_reasoning_replay"), \
            patch("agent.agent_runtime_helpers._restore_runtime_capabilities"), \
            patch("agent.agent_runtime_helpers._rebuild_primary_client"), \
            patch("agent.chat_completion_helpers._reset_stale_streak"), \
            patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"):
        agent.api_mode, agent.provider = "chat_completions", "p"
        agent.context_compressor = SimpleNamespace(update_model=lambda **k: None)
        rt.update(use_prompt_caching=False, compressor_model="m", compressor_context_length=1,
                  compressor_base_url="", compressor_api_key="", compressor_provider="p")
        reinstall_runtime_snapshot(agent, rt)
    assert agent._fallback_bound_reasoning == {"effort": "low"}



def test_a_backup_rate_limit_keeps_the_primary_probe_owed():
    from agent.fallback_cooldown import arm_primary_cooldown

    agent = SimpleNamespace(_fallback_activated=True, provider="zai", model="glm-5.2",
                            _primary_runtime={"provider": "openrouter", "model": "primary/model"},
                            _primary_outage_cooldown=True, _primary_probe_pending=False,
                            _rate_limited_until=0)
    arm_primary_cooldown(agent, FailoverReason.rate_limit)
    assert agent._primary_outage_cooldown is True
