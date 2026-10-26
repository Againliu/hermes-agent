"""The provider harness drives hooks in the host's real order, and catches a provider that only
works under a hand-written order (the "blank between on_turn_start and prefetch" class)."""
from __future__ import annotations

from agent.memory_provider import MemoryProvider
from tests.plugins.memory.provider_harness import ProviderHarness


class _NotesProvider(MemoryProvider):
    """Toy store: sync_turn keeps user lines, queue_prefetch prepares recall for the next turn."""

    def __init__(self):
        self.notes: list[str] = []
        self._ready = ""

    @property
    def name(self) -> str:
        return "harness-notes"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        self.notes.append(user_content)

    def queue_prefetch(self, query, *, session_id=""):
        self._ready = "\n".join(f"- {n}" for n in self.notes)

    def prefetch(self, query, *, session_id=""):
        return self._ready


class _BlanksOnTurnStart(_NotesProvider):
    """Resets its recall cache in on_turn_start. Passes a test that calls on_turn_start ->
    queue_prefetch -> prefetch by hand; blank in every real session."""

    @property
    def name(self) -> str:
        return "harness-blanks"

    def on_turn_start(self, turn_number, message, **kwargs):
        self._ready = ""


def test_recall_from_turn_one_reaches_the_model_on_turn_two():
    with ProviderHarness(_NotesProvider(), platform="telegram") as h:
        h.turn("my cat is called Miso")
        h.turn("what is my cat called?")
        h.end_session()

    assert "Miso" in h.prefetched[-1]
    assert "Miso" in h.requests[-1]
    assert h.init_kwargs["platform"] == "telegram"
    order = h.hook_order
    assert order[0] == "initialize" and order[-1] == "shutdown"
    second_turn = order[order.index("queue_prefetch"):]  # turn 1's queue_prefetch, then turn 2
    assert second_turn.index("on_turn_start") < second_turn.index("prefetch")
    assert "on_session_end" in order


def test_harness_catches_a_provider_that_blanks_between_turn_start_and_prefetch():
    with ProviderHarness(_BlanksOnTurnStart()) as h:
        h.turn("my cat is called Miso")
        h.turn("what is my cat called?")

    assert h.prefetched[-1] == ""
    assert "Miso" not in h.requests[-1].split("what is my cat called?")[-1]
