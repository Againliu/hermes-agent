"""``hermes update --when-idle``: waits out live work, and never auto-retries a target that failed."""

from __future__ import annotations

import pytest

from hermes_cli import update_cmd_when_idle as when_idle


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _probe(answers):
    seq = iter(answers)
    return lambda: next(seq)


def test_waits_for_two_idle_polls_in_a_row_and_gives_up_at_the_bound(capsys):
    clock = _Clock()
    # A turn that ends as the next is admitted reads idle once: that single gap is not enough.
    assert when_idle.wait_until_idle(60, probe=_probe(["1 chat turn", "", "1 cron run", "", ""]),
                                     poll_s=5, clock=clock, sleep=clock.sleep) is True
    out = capsys.readouterr().out
    assert "1 chat turn" in out and "1 cron run" in out

    clock = _Clock()
    # A gateway that answers identify but not active-work cannot be proven idle: it is waited on.
    assert when_idle.wait_until_idle(12, probe=lambda: None, poll_s=5, clock=clock, sleep=clock.sleep) is False
    assert clock.now >= 12


def test_a_failed_target_is_skipped_until_a_newer_one_ships(tmp_path, monkeypatch):
    monkeypatch.setattr(when_idle, "declined_path", lambda: tmp_path / "declined_target.json")
    monkeypatch.setattr(when_idle, "wait_until_idle", lambda max_wait: True)
    monkeypatch.setattr(when_idle, "_project_root", lambda: tmp_path)
    (tmp_path / ".git").mkdir()
    calls = []

    def failing_update(args):
        calls.append("update")
        raise SystemExit(1)

    args = type("Args", (), {"when_idle": 60.0, "gateway": False})()
    monkeypatch.setattr(when_idle, "_resolve_target", lambda a: ("a" * 40, "v1.2.3"))
    with pytest.raises(SystemExit):
        when_idle.run_when_idle(args, failing_update)
    assert calls == ["update"] and when_idle.read_declined()["target"] == "a" * 40

    when_idle.run_when_idle(args, failing_update)  # same target again: skipped, no update attempted
    assert calls == ["update"]

    monkeypatch.setattr(when_idle, "_resolve_target", lambda a: ("b" * 40, "v1.2.4"))
    when_idle.run_when_idle(args, lambda a: calls.append("newer"))
    assert calls == ["update", "newer"]
