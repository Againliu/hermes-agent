"""Invariant: ``kanban_show`` returns a bounded run history.

Events were already capped, but every run's summary/error/metadata came back
whole, so one 50 KB worker summary (or a card retried for days) landed
verbatim in the orchestrator's context on every ``kanban_show``.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def card(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="long-lived card", assignee="coder")
    return tid


def _add_runs(tid, summaries, *, metadata=None):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        for i, summary in enumerate(summaries):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, "
                "started_at, ended_at) VALUES (?, 'coder', 'done', 'crashed', ?, ?, ?, ?)",
                (tid, summary, json.dumps(metadata) if metadata else None, 1000 + i, 1001 + i))
        conn.commit()


def test_show_keeps_newest_runs_and_counts_the_rest(card):
    from hermes_cli import kanban_db_context as kbctx
    from tools import kanban_tools as kt
    total = kbctx._CTX_MAX_PRIOR_ATTEMPTS + 5
    _add_runs(card, [f"attempt {i:02d}" for i in range(total)])
    out = json.loads(kt._handle_show({"task_id": card}))
    assert [r["summary"] for r in out["runs"]] == [
        f"attempt {i:02d}" for i in range(5, total)]
    assert out["runs_omitted"] == 5


def test_show_caps_a_huge_run_summary_and_metadata(card):
    from hermes_cli import kanban_db_context as kbctx
    from tools import kanban_tools as kt
    huge = "s" * 50_000
    _add_runs(card, [huge], metadata={"log": "m" * 50_000})
    raw = kt._handle_show({"task_id": card})
    assert huge not in raw
    run = json.loads(raw)["runs"][0]
    assert run["summary"].startswith("s" * kbctx._CTX_MAX_FIELD_BYTES + "… [truncated,")
    assert isinstance(run["metadata"], str) and "[truncated," in run["metadata"]
    assert len(raw) < 20_000  # worker_context renders the same run, also capped


def test_small_run_metadata_stays_structured(card):
    from tools import kanban_tools as kt
    _add_runs(card, ["ok"], metadata={"changed_files": ["a.py"]})
    run = json.loads(kt._handle_show({"task_id": card}))["runs"][0]
    assert run["metadata"] == {"changed_files": ["a.py"]}
    assert run["summary"] == "ok"
