"""An approval nobody answered settles for every shared viewer with ``reason: timeout``.

Canonical gateways (the TUI's and Desktop's default route) show approvals as shared controls; before
this the expired card vanished with no reason, so no surface could say the command did not run.
"""
import threading
from types import SimpleNamespace

from gateway.session_pending_controls import PendingControls
from tools import approval as mod
from tools import approval_gateway_wait as wait_mod

SESSION_KEY = "pending-controls-timeout"
APPROVAL = {"command": "rm -rf build", "description": "d", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]}


def _controls():
    published = []
    events = SimpleNamespace(lock=threading.RLock(),
                             publish=lambda sid, payload, event_type: published.append((event_type, payload)))
    return PendingControls(events), published


def _wait(monkeypatch, notify, state):
    mod._gateway_queues.clear()
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: None)
    # The deadline check itself is the only thing faked: the wait ends the way its clock would end it.
    monkeypatch.setattr(wait_mod, "_poll_event", lambda event, key, *, interrupt_log: state())
    return wait_mod._await_gateway_decision(SESSION_KEY, notify, dict(APPROVAL))


def test_expired_approval_settles_with_timeout_reason_and_keeps_earlier_hooks(monkeypatch):
    controls, published = _controls()
    chat_notice = []

    def notify(data):
        # A messaging chat armed its own timeout notice first (run_turn_runner_approval_settle).
        mod.register_gateway_settle(SESSION_KEY, data["request_id"], chat_notice.append)
        controls.register("s", SESSION_KEY, 7, data)

    result = _wait(monkeypatch, notify, lambda: "timeout")

    assert result["resolved"] is False
    settled = [payload for kind, payload in published if kind == "approval.settled"]
    assert settled == [{"prompt_id": settled[0]["prompt_id"], "reason": "timeout", "execution_generation": 7}]
    assert chat_notice == ["timeout"] and controls.pending == {}


def test_answered_approval_settles_without_a_timeout_reason(monkeypatch):
    controls, published = _controls()

    def notify(data):
        controls.register("s", SESSION_KEY, 7, data)
        mod.resolve_gateway_approval(SESSION_KEY, "once")

    _wait(monkeypatch, notify, lambda: "set")

    assert not any(payload.get("reason") == "timeout" for _, payload in published)
