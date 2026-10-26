"""Drive a memory provider through a real ``AIAgent`` so its hooks fire in the host's own order.

Provider tests that call hooks by hand tend to invent an order the host never uses (for example
``queue_prefetch`` between ``on_turn_start`` and ``prefetch``), so a provider whose recall is blank
in a live session still passes. This harness runs real turns instead: the real ``AIAgent`` facade,
the real ``MemoryManager`` (initialize kwargs, background sync executor, prefetch fencing) and the
real turn loop, with only the model faked. It records every hook call and every request the model
received, so a test can assert what the provider saw and what actually reached the model.

Usage from a provider's tests (any ``MemoryProvider`` instance)::

    from tests.plugins.memory.provider_harness import ProviderHarness

    def test_recall_reaches_the_model():
        with ProviderHarness(MyProvider(), platform="telegram") as h:
            h.turn("my cat is called Miso")
            h.turn("what is my cat called?")
        assert "Miso" in h.requests[-1]          # recalled text was in the model's input
        assert h.prefetched[-1]                  # prefetch returned something on turn 2

Run it under the suite's isolated ``HERMES_HOME`` (``scripts/run_tests.sh``); the harness writes
``memory.provider: <name>`` to that home's ``config.yaml``.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

HOOKS = (
    "initialize", "on_turn_start", "prefetch", "sync_turn", "queue_prefetch", "on_pre_compress",
    "on_session_switch", "on_session_end", "on_memory_write", "on_delegation", "shutdown",
)


class ProviderHarness:
    """Context manager: build a real agent around *provider*, run turns, tear down on exit.

    ``events`` is ``[(hook, kwargs_or_args), ...]`` in call order, ``prefetched`` holds each
    ``prefetch`` return value, ``requests`` is the flattened text of each model request,
    ``init_kwargs`` is what ``initialize`` received.
    """

    def __init__(self, provider: Any, *, platform: str = "cli", session_id: str = "harness-session",
                 reply: str = "ok", **agent_kwargs: Any) -> None:
        self.provider = provider
        self.platform = platform
        self.session_id = session_id
        self.reply = reply
        self.agent_kwargs = agent_kwargs
        self.events: list[tuple[str, Any]] = []
        self.prefetched: list[str] = []
        self.requests: list[str] = []
        self.init_kwargs: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.agent = None
        self._patches: list[Any] = []

    # -- recording -------------------------------------------------------------------------
    def _record(self, hook: str, fn):
        @functools.wraps(fn)  # keeps the signature: the manager inspects it (turn_author, metadata...)
        def wrapper(*args, **kwargs):
            if hook == "initialize":
                self.init_kwargs = dict(kwargs)
            self.events.append((hook, kwargs or args))
            result = fn(*args, **kwargs)
            if hook == "prefetch":
                self.prefetched.append(result or "")
            return result
        return wrapper

    def _install_recorders(self) -> None:
        for hook in HOOKS:
            fn = getattr(self.provider, hook, None)
            if callable(fn):
                setattr(self.provider, hook, self._record(hook, fn))

    def _fake_client(self):
        harness = self

        class _Completions:
            def create(self, **kw):
                harness.requests.append(json.dumps(kw.get("messages") or [], default=str))
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=harness.reply, tool_calls=None, reasoning=None,
                                            reasoning_content=None),
                    finish_reason="stop")], usage=None, model="harness-model")

        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    # -- lifecycle -------------------------------------------------------------------------
    def __enter__(self) -> ProviderHarness:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        home.mkdir(parents=True, exist_ok=True)
        # JSON is valid YAML; the suite's isolated home is the harness's own.
        (home / "config.yaml").write_text(json.dumps({"memory": {"provider": self.provider.name}}), encoding="utf-8")

        self._install_recorders()
        client = self._fake_client()
        self._patches = [
            patch("agent.agent_init.resolve_memory_provider", lambda *_a, **_k: self.provider),
            patch("agent.process_bootstrap.OpenAI", lambda **_kw: client),
            patch("model_tools.get_tool_definitions", lambda *a, **k: []),
        ]
        for p in self._patches:
            p.start()
        from run_agent import AIAgent

        self.agent = AIAgent(
            model="harness-model", api_key="harness-key", base_url="http://127.0.0.1:9/v1",
            platform=self.platform, session_id=self.session_id, quiet_mode=True,
            skip_context_files=True, skip_memory=False, max_iterations=2,
            **self.agent_kwargs,
        )
        self.agent._disable_streaming = True
        return self

    def turn(self, user_message: str) -> str:
        """Run one real turn, then wait for the background sync/prefetch work it queued."""
        result = self.agent.run_conversation(user_message, conversation_history=list(self.history))
        self.history = list(result.get("messages") or self.history)
        manager = self.agent._memory_manager
        if manager is not None:
            manager.flush_pending(timeout=10)
        return result.get("final_response") or ""

    def end_session(self) -> None:
        """A real session boundary (``/new``-style): ``on_session_end`` with the transcript."""
        self.agent.commit_memory_session(list(self.history))
        manager = self.agent._memory_manager
        if manager is not None:
            manager.flush_pending(timeout=10)

    def __exit__(self, *exc) -> None:
        try:
            if self.agent is not None:
                self.agent.close()
        finally:
            for p in reversed(self._patches):
                p.stop()

    # -- reading ---------------------------------------------------------------------------
    @property
    def hook_order(self) -> list[str]:
        return [hook for hook, _ in self.events]
