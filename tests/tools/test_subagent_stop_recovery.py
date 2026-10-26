"""Restart recovery fires ``subagent_stop`` for children whose owner died before their unit joined."""

from __future__ import annotations

import json
import os
import subprocess
import sys

_PRODUCER = r"""
import os, sys, time
from unittest.mock import MagicMock
import tools.delegate_tool as dt
parent = MagicMock(); parent._delegate_depth = 0; parent.session_id = "parent-sess"; parent._interrupt_requested = False
parent._active_children = []; parent._active_children_lock = None
def child(task_index, goal, child=None, parent_agent=None, **kw):
    if task_index == 1:
        time.sleep(600)
    return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 2,
            "duration_seconds": 0.5, "model": "m", "exit_reason": "completed", "tokens": {"input": 10, "output": 3},
            "cost_usd": 0.0012, "cost_status": "estimated"}
def build(**kw):
    c = MagicMock(); i = kw["task_index"]
    c._delegate_role = "leaf"; c._subagent_id = f"sa-{i}"; c.session_id = f"child-sess-{i}"
    c._delegate_parent_tool_call_id = kw.get("parent_tool_call_id")
    return c
creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None, "args": None}
dt._build_child_agent = build; dt._run_single_child = child; dt._resolve_delegation_credentials = lambda *a, **k: creds
dt.delegate_task(tasks=[{"goal": "fast member of the group", "group": "g"},
                        {"goal": "slow member of the group", "group": "g"}], background=True,
                 parent_agent=parent, tool_call_id="call-delegate-1")
time.sleep(2.0)
os._exit(1)
"""

_CONSUMER = r"""
import json, queue
from hermes_cli import plugins
from tools import async_delegation as ad
seen = []
manager = plugins.PluginManager(); manager._discovered = True
manager._hooks["subagent_stop"] = [lambda **kw: seen.append(kw)]
plugins._delivery_manager = lambda: manager
ad.restore_undelivered_completions(queue.Queue())
ad.recover_abandoned_delegations()  # a second recovery pass must not replay the stops
print(json.dumps(seen, sort_keys=True))
"""


def test_owner_death_replays_one_subagent_stop_per_started_child(tmp_path):
    """Real-import E2E: a background unit's owner dies with one child finished and one still running. The next
    process's recovery fires exactly one ``subagent_stop`` per child, naming the ``delegate_task`` call and task,
    with the finished child's recorded result and spend and the other ``unknown``."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    subprocess.run([sys.executable, "-c", _PRODUCER], cwd=repo, env=env, text=True, encoding="utf-8",
                   capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    out = subprocess.run([sys.executable, "-c", _CONSUMER], cwd=repo, env=env, text=True, encoding="utf-8",
                         capture_output=True, timeout=30, check=True, stdin=subprocess.DEVNULL)
    stops = json.loads(out.stdout.strip().splitlines()[-1])

    assert [s["task_index"] for s in stops] == [0, 1]
    for i, stop in enumerate(stops):
        assert stop["recovered"] is True
        assert stop["parent_session_id"] == "parent-sess"
        assert stop["parent_tool_call_id"] == "call-delegate-1"
        assert (stop["child_session_id"], stop["child_subagent_id"]) == (f"child-sess-{i}", f"sa-{i}")
    assert stops[0]["child_status"] == "completed" and stops[0]["child_summary"] == "done: fast member of the group"
    assert (stops[0]["cost_usd"], stops[0]["tokens"], stops[0]["api_calls"]) == (0.0012, {"input": 10, "output": 3}, 2)
    assert stops[1]["child_status"] == "unknown" and stops[1]["cost_status"] == "unknown"
