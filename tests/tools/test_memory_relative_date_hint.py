"""A memory write that names a time relative to now gets a hint to restate it as an absolute date:
the entry outlives the conversation that anchored "in 3 weeks"."""

import json
from datetime import date

import pytest

from tools.memory_tool import MemoryStore
from tools.registry import registry


def _dispatch(store, **args):
    return json.loads(registry.dispatch("memory", args, store=store))


@pytest.mark.parametrize("content", [
    "Passport renewal is due in 3 weeks",
    "Dentist appointment next Tuesday",
    "Moved to Lisbon two months ago",
])
def test_relative_time_on_a_saved_entry_asks_for_an_absolute_date(content, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store = MemoryStore()

    single = _dispatch(store, action="add", target="memory", content=content)
    batch = _dispatch(store, target="user", operations=[{"action": "add", "content": content}])

    for result in (single, batch):
        assert result["success"] is True
        assert date.today().isoformat() in result["date_hint"]
    assert content in store._entries_for("memory")  # a hint, never a refusal


def test_absolute_dates_and_rejected_writes_carry_no_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store = MemoryStore(memory_char_limit=60, user_char_limit=60)

    assert "date_hint" not in _dispatch(store, action="add", target="memory", content="Passport renewal due 2026-11-16")
    rejected = _dispatch(store, action="add", target="memory", content="Call the bank tomorrow " + "x" * 60)
    assert rejected["success"] is False and "date_hint" not in rejected
