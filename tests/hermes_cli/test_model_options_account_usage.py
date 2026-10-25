"""The model picker shows subscription usage before the wall, without ever waiting on a usage API.

Usage windows come from the provider's API, so ``model.options`` must read them from cache and
refresh in the background: a slow or hung usage endpoint can't be allowed to stall the picker.
"""

import threading
import time
from datetime import datetime, timedelta, timezone, UTC

from agent import account_usage
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.account_usage_cache import cached_account_usage
from hermes_cli.inventory import build_model_options_payload, load_picker_context


def _snapshot(used: float) -> AccountUsageSnapshot:
    reset = datetime.now(UTC) + timedelta(hours=2)
    return AccountUsageSnapshot(
        provider="openrouter", source="test", fetched_at=datetime.now(UTC),
        windows=(AccountUsageWindow(label="Current session", used_percent=used, reset_at=reset),))


def test_picker_reads_cached_usage_and_refreshes_without_waiting(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    release, called, answered = threading.Event(), threading.Event(), threading.Event()
    fetch_started: list[float] = []
    answers = iter([_snapshot(92.0)])

    def slow_usage_api(_base_url, _api_key):
        fetch_started.append(time.monotonic())
        called.set()
        release.wait(60)  # hung until the test lets go: a picker that waits on it never returns first
        answered.set()
        return next(answers)

    monkeypatch.setitem(account_usage._USAGE_FETCHERS, "openrouter", slow_usage_api)
    ctx = load_picker_context()

    # Time only what follows the usage fetch's start: the rest of the open (cold catalogs, the
    # Copilot `gh auth token` probe) is not the usage API, and its cost depends on the host.
    first = next(r for r in build_model_options_payload(ctx)["providers"] if r["slug"] == "openrouter")
    returned = time.monotonic()
    assert not answered.is_set(), "the picker waited on the usage API"
    assert "usage" not in first, "nothing cached yet, so nothing to show"
    assert called.wait(5), "opening the picker must start a background usage refresh"
    assert returned - fetch_started[0] < 5, "the picker waited on the usage API"

    release.set()
    deadline = time.monotonic() + 5
    while cached_account_usage("openrouter") is None and time.monotonic() < deadline:
        time.sleep(0.05)

    row = next(r for r in build_model_options_payload(ctx)["providers"] if r["slug"] == "openrouter")
    (window,) = row["usage"]["windows"]
    assert window["label"] == "Current session" and window["used_percent"] == 92.0
    assert datetime.fromisoformat(window["resets_at"]) > datetime.now(UTC)
