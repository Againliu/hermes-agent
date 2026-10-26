"""Pre-fire provider usage-limit gate for cron (cron/quota_hold.py)."""

import time

import pytest

from agent import usage_hold
from cron import jobs as cron_jobs
from cron.quota_hold import STATE_KEY, hold_due_jobs_for_provider_limits

_CFG = {"model": {"provider": "custom", "base_url": "http://127.0.0.1:9/v1"}}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron", raising=False)
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json", raising=False)
    return tmp_path


def _due(name, **extra):
    job = cron_jobs.create_job(prompt="hi", schedule="every 30m", name=name, **extra)
    cron_jobs.update_job(job["id"], {"next_run_at": cron_jobs._hermes_now().isoformat()})
    return cron_jobs.get_job(job["id"])


def test_held_provider_parks_due_jobs_wait_and_skip(home):
    wait_job, skip_job = _due("w"), _due("s")
    cron_jobs.update_job(skip_job["id"], {"on_limit": "skip"})
    until = time.time() + 3600
    usage_hold.record_hold("custom", base_url="http://127.0.0.1:9/v1", until=until)

    runnable = hold_due_jobs_for_provider_limits(
        [cron_jobs.get_job(wait_job["id"]), cron_jobs.get_job(skip_job["id"])], _CFG,
        lambda job, cfg: None)

    assert runnable == []
    waited = cron_jobs.get_job(wait_job["id"])
    skipped = cron_jobs.get_job(skip_job["id"])
    waited_at = cron_jobs._parse_aware(waited["next_run_at"]).timestamp()
    assert until <= waited_at <= until + 120  # one run right after the reset
    assert waited[STATE_KEY] == waited["next_run_at"]
    assert skipped["last_status"] == "skipped_usage_limit"
    assert cron_jobs._parse_aware(skipped["next_run_at"]).timestamp() > waited_at


def test_unheld_provider_or_fallback_chain_still_fires(home):
    job = _due("j")
    usage_hold.record_hold("anthropic", until=time.time() + 3600)
    assert hold_due_jobs_for_provider_limits([job], _CFG, lambda j, c: None) == [job]
    usage_hold.record_hold("custom", base_url="http://127.0.0.1:9/v1", until=time.time() + 3600)
    assert hold_due_jobs_for_provider_limits([job], _CFG, lambda j, c: ["fallback"]) == [job]
