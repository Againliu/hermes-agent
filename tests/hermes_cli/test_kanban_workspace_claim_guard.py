"""Invariant: two workers never write the same workspace directory (#128259).

``_claim_and_open_run`` performed the claim CAS with no workspace-busy check,
so two cards on one ``dir`` workspace (decomposed siblings inherit the root's)
ran concurrently in one directory. The claim guard refuses the second claim
while another live running task holds the same directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def _running_holder(conn, workspace_path, *, worker="live", ttl_seconds=None):
    """A claimed (running) task holding ``workspace_path``. ``worker``: ``live``
    (this process stands in for the spawned worker), ``dead`` (the recorded
    worker exited) or ``none`` (a CLI claim: no worker recorded)."""
    tid = kb.create_task(
        conn, title="holder", assignee="coder",
        workspace_kind="dir", workspace_path=workspace_path,
    )
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id(), ttl_seconds=ttl_seconds) is not None
    if worker != "none":
        kbd._set_worker_pid(conn, tid, os.getpid() if worker == "live" else _dead_pid())
    return tid


def _ready_contender(conn, workspace_path, kind="dir"):
    return kb.create_task(
        conn, title="contender", assignee="coder",
        workspace_kind=kind, workspace_path=workspace_path,
    )


def test_second_claim_refused_while_workspace_busy(conn, tmp_path):
    ws = str(tmp_path / "ws")
    _running_holder(conn, ws, worker="live")

    contender = _ready_contender(conn, ws)
    assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is None
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (contender,)).fetchone()["status"] == "ready"
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (contender,),
    ).fetchone()
    assert event["kind"] == "claim_rejected"
    assert "workspace_busy" in (event["payload"] or "")


def test_workspace_busy_normalizes_trailing_slash(conn, tmp_path):
    _running_holder(conn, str(tmp_path / "ws") + "/", worker="live")

    contender = _ready_contender(conn, str(tmp_path / "ws"))
    assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is None


def test_dead_holder_does_not_block_claim(conn, tmp_path):
    ws = str(tmp_path / "ws")
    _running_holder(conn, ws, worker="dead")

    contender = _ready_contender(conn, ws)
    assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is not None


def test_distinct_workspaces_claim_fine(conn, tmp_path):
    _running_holder(conn, str(tmp_path / "ws-a"), worker="live")

    contender = _ready_contender(conn, str(tmp_path / "ws-b"))
    assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is not None


def test_cli_claim_without_worker_still_holds_workspace(conn, tmp_path):
    """``hermes kanban claim`` (and the dispatcher between claim and spawn)
    records no worker PID; the unexpired claim still owns the directory."""
    ws = str(tmp_path / "ws")
    _running_holder(conn, ws, worker="none")
    assert kb.claim_task(conn, _ready_contender(conn, ws), claimer=kb._claimer_id()) is None


def test_expired_workerless_claim_does_not_block(conn, tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    _running_holder(conn, ws, worker="none", ttl_seconds=1)
    real_time = kb.time.time
    monkeypatch.setattr(kb.time, "time", lambda: real_time() + 5)
    assert kb.claim_task(conn, _ready_contender(conn, ws), claimer=kb._claimer_id()) is not None


def test_worktree_contender_on_a_busy_repo_root_still_claims(conn, tmp_path):
    """A ``worktree`` path names the repo each card gets its own
    ``.worktrees/<id>`` from, never a shared directory."""
    repo = str(tmp_path / "repo")
    _running_holder(conn, repo, worker="live")
    contender = _ready_contender(conn, repo, kind="worktree")
    assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is not None


def test_repeated_refusals_record_one_event(conn, tmp_path):
    ws = str(tmp_path / "ws")
    _running_holder(conn, ws, worker="live")
    contender = _ready_contender(conn, ws)
    for _ in range(3):
        assert kb.claim_task(conn, contender, claimer=kb._claimer_id()) is None
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (contender,))]
    assert kinds.count("claim_rejected") == 1
