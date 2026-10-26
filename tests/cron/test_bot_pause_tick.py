"""A paused Bot Mode bot's scheduled jobs do not fire, and its resume does not burst the runs it
missed: each due slot is consumed while paused. The pause is the bot's own (profile.yaml), so a
sibling profile ticked by the same process keeps firing."""
from datetime import timedelta

import pytest

import hermes_yaml as yaml


def _set_paused(home, paused):
    meta = {"ui_meta": {"hermes-bots": {"title": home.name, "paused": paused}}}
    (home / "profile.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")


def _overdue_job(home, minutes=3):
    from cron.jobs import _hermes_now, create_job, load_jobs, save_jobs
    from cron.scheduler_provider import _profile_cron_scope

    with _profile_cron_scope(home):
        job = create_job(prompt="report", schedule="every 5m")
        jobs = load_jobs()
        for row in jobs:
            row["next_run_at"] = (_hermes_now() - timedelta(minutes=minutes)).isoformat()
        save_jobs(jobs)
    return job["id"]


def _tick(home):
    from cron import scheduler
    from cron.jobs import load_jobs
    from cron.scheduler_provider import _profile_cron_scope

    with _profile_cron_scope(home):
        scheduler.tick(verbose=False)
        return load_jobs()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    from cron import scheduler

    root = tmp_path / ".hermes"
    paused, active = root / "profiles" / "scribe", root / "profiles" / "coder"
    for home in (paused, active):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    fired = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a, **k: fired.append(job["id"]) or True)
    return paused, active, fired


def test_a_paused_bot_skips_its_due_run_and_its_sibling_still_fires(fleet):
    from cron.jobs import _hermes_now, _parse_aware

    paused, active, fired = fleet
    _set_paused(paused, True)
    _set_paused(active, False)
    held, live = _overdue_job(paused), _overdue_job(active)

    (job,) = _tick(paused)
    _tick(active)

    assert fired == [live]
    # The missed slot is consumed, not left overdue for the resume to fire.
    assert job["id"] == held and _parse_aware(job["next_run_at"]) > _hermes_now()


def test_resume_does_not_burst_the_runs_missed_while_paused(fleet):
    paused, _active, fired = fleet
    _set_paused(paused, True)
    _overdue_job(paused)
    _tick(paused)
    _tick(paused)

    _set_paused(paused, False)
    _tick(paused)

    assert fired == []


def test_a_pause_no_tick_saw_still_skips_the_slot_that_fell_due_inside_it(fleet, monkeypatch):
    """Pause, a slot falls due, resume, all before the next tick (or with the ticker down)."""
    import tui_gateway.server as srv
    from cron.jobs import _hermes_now, _parse_aware

    paused, _active, fired = fleet
    monkeypatch.setenv("HOME", str(paused.parent.parent.parent))  # profiles resolve under ~/.hermes

    def configure(flag):
        params = {"name": "scribe", "ui_meta": {"hermes-bots": {"title": "scribe", "paused": flag}}}
        return srv._methods["profiles.configure"]("c", params)["result"]["applied"]

    pause = configure(True)
    held = _overdue_job(paused, minutes=0)  # due now: inside the pause
    configure(False)
    (job,) = _tick(paused)

    assert pause["bot_pause_enforced"] is True  # the Desktop's proof this gateway enforces the pause
    assert fired == [] and job["id"] == held and _parse_aware(job["next_run_at"]) > _hermes_now()
