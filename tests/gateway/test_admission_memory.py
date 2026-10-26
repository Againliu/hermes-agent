"""Memory-aware turn admission (gateway/admission_memory.py) and child OOM preference."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway import admission_memory as am

MB = 1024 * 1024


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _gate(free_mb, *, ceiling_mb=4096, **settings):
    state = {"free": free_mb}
    clock = _Clock()
    gate = am.MemoryGate(
        settings=am.MemoryGuardSettings(**{"critical_mb": 256, "low_mb": 512, "reserve_mb": 0, **settings}),
        reader=lambda: am.MemoryReading(state["free"] * MB, ceiling_mb * MB, "cgroup"),
        clock=clock,
    )
    return gate, state, clock


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cgroup_headroom_uses_tightest_ancestor_and_credits_page_cache(tmp_path):
    # Container root capped at 1 GiB; the gateway's own leaf is uncapped. 900 MiB charged, of
    # which 300 MiB is reclaimable inactive file cache -> 424 MiB really left.
    _write(tmp_path / "memory.max", str(1024 * MB))
    _write(tmp_path / "memory.current", str(900 * MB))
    _write(tmp_path / "memory.stat", f"anon 1\ninactive_file {300 * MB}\n")
    leaf = tmp_path / "svc" / "gw"
    _write(leaf / "memory.max", "max")
    _write(leaf / "memory.current", str(500 * MB))
    headroom, limit = am.cgroup_headroom("/svc/gw", root=tmp_path)
    assert (headroom // MB, limit // MB) == (424, 1024)


def test_people_start_at_low_while_background_waits_behind_them():
    gate, state, clock = _gate(400)  # between critical (256) and low (512)
    assert gate.try_admit(am.PERSON)
    assert not gate.try_admit(am.BACKGROUND)
    state["free"], clock.now = 100, clock.now + 2  # critical: the person waits too
    person = gate.enqueue(am.PERSON)
    state["free"], clock.now = 4000, clock.now + 2
    # Plenty of memory again, but a person is still queued: background may not jump ahead.
    assert not gate.try_admit(am.BACKGROUND)
    assert gate.try_admit(am.PERSON, person)
    assert gate.try_admit(am.BACKGROUND)


def test_reservation_stops_a_released_burst_from_starting_into_the_same_headroom():
    gate, _, clock = _gate(700, reserve_mb=300)
    assert gate.try_admit(am.PERSON)  # 700 free -> reserves 300
    assert gate.try_admit(am.PERSON)  # 400 effective >= 256
    assert not gate.try_admit(am.PERSON)  # 100 effective < 256
    clock.now += am._RESERVATION_WINDOW_S + 1
    assert gate.try_admit(am.PERSON)


def test_wait_is_bounded_and_publishes_waiting_count(tmp_path):
    gate, _, clock = _gate(10, max_wait_seconds=5)
    gate.status_path = tmp_path / "state" / "memory_guard.json"
    seen = []

    def sleep(_):
        seen.append(am.read_status(tmp_path)["waiting_people"])
        clock.now += 2

    assert am.wait_blocking(am.PERSON, gate=gate, sleep=sleep) == "timeout"
    assert seen and seen[0] == 1
    assert am.read_status(tmp_path)["waiting_people"] == 0
    assert am.wait_blocking(am.PERSON, gate=gate, still_wanted=lambda: False, sleep=sleep) == "abandoned"


def test_unknown_memory_never_blocks():
    gate = am.MemoryGate(reader=lambda: None)
    assert gate.try_admit(am.BACKGROUND)


def test_disabled_guard_admits_everything():
    gate, _, _ = _gate(1, enabled=False)
    assert gate.try_admit(am.BACKGROUND)


class _Runner:
    from gateway.run_inbound_hooks import GatewayInboundHooksMixin as _M

    _hm_memory_admission = _M._hm_memory_admission

    def __init__(self):
        self.sent = []
        self.generation = 1

    def _delivery_adapter_for(self, source):
        return object()

    async def _send_busy_reply(self, event, adapter, text):
        self.sent.append(text)

    def _is_session_run_current(self, key, generation):
        return generation == self.generation


def _event():
    from gateway.config import Platform

    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="1")
    return SimpleNamespace(source=source), source


def test_inbound_turn_waits_with_notice_then_refuses_after_the_bound(monkeypatch):
    gate, _, clock = _gate(10, max_wait_seconds=60)
    monkeypatch.setattr(am, "_gate", gate)

    async def fast_sleep(_):
        clock.now += 30

    monkeypatch.setattr(am.asyncio, "sleep", fast_sleep)
    runner, (event, source) = _Runner(), _event()
    reply = asyncio.run(runner._hm_memory_admission(event, source, "k", False, 1))
    assert len(runner.sent) == 1 and "memory" in runner.sent[0]
    assert reply and "memory" in reply  # refused, not started


def test_inbound_turn_starts_once_memory_frees(monkeypatch):
    gate, state, clock = _gate(10, max_wait_seconds=600)
    monkeypatch.setattr(am, "_gate", gate)

    async def fast_sleep(_):
        clock.now += 2
        state["free"] = 2048

    monkeypatch.setattr(am.asyncio, "sleep", fast_sleep)
    runner, (event, source) = _Runner(), _event()
    assert asyncio.run(runner._hm_memory_admission(event, source, "k", False, 1)) is None
    assert len(runner.sent) == 1


def test_cron_run_is_recorded_failed_when_memory_never_frees(monkeypatch):
    from cron.scheduler_memory_hold import hold_for_memory

    gate, _, clock = _gate(10, max_wait_seconds=4)
    monkeypatch.setattr(am, "_gate", gate)
    monkeypatch.setattr(am.time, "sleep", lambda _: setattr(clock, "now", clock.now + 2))  # cron thread's sleep
    held = hold_for_memory({"id": "j1", "name": "nightly"}, "j1", "nightly", "do it", None)
    assert held is not None and held[0] is False and "memory" in held[3]


@pytest.mark.platforms("linux")
def test_terminal_commands_are_preferred_oom_victims_over_hermes(tmp_path):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = env.execute("sleep 0.3; cat /proc/$$/oom_score_adj")
    finally:
        env.cleanup()
    own = int(open("/proc/self/oom_score_adj", encoding="utf-8").read())
    assert int(result["output"].strip().splitlines()[-1]) == min(1000, own + 500)


@pytest.mark.platforms("linux")
def test_child_score_is_raised_relative_to_the_spawner_and_capped():
    from hermes_platform.oom import prefer_as_oom_victim

    own = int(open("/proc/self/oom_score_adj", encoding="utf-8").read())
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        prefer_as_oom_victim(proc.pid)
        assert int(open(f"/proc/{proc.pid}/oom_score_adj", encoding="utf-8").read()) == min(1000, own + 500)
        prefer_as_oom_victim(proc.pid)
        assert int(open(f"/proc/{proc.pid}/oom_score_adj", encoding="utf-8").read()) == min(1000, own + 1000)
    finally:
        proc.kill()
        proc.wait()
