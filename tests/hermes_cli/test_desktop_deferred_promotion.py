"""An update never swaps files under (or stops) a running POSIX Desktop: it parks the verified
pack and installs it once the app has quit (#139235, #138502)."""

import json
import shutil
import subprocess
import pytest

from hermes_cli import main_desktop, main_desktop_pending
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")


def _pack(base, tag):
    unpacked = base / "linux-unpacked"
    (unpacked / "resources").mkdir(parents=True)
    shutil.copy2("/bin/bash", unpacked / "hermes")  # a real executable living in the build tree
    (unpacked / "resources" / "install-stamp.json").write_text(json.dumps({"commit": tag}), encoding="utf-8")
    return unpacked


def _stamp(desktop_dir):
    return json.loads((desktop_dir / "release/linux-unpacked/resources/install-stamp.json").read_text(encoding="utf-8-sig"))["commit"]


@pytest.fixture
def waiters(monkeypatch):
    spawned = []
    monkeypatch.setattr(main_desktop_pending, "spawn_promotion_waiter", spawned.append)
    return spawned


@pytest.fixture
def desktop_dir(tmp_path):
    desktop = tmp_path / "repo" / "apps" / "desktop"
    _pack(desktop / "release", "OLD")
    return desktop


def test_update_parks_the_build_while_desktop_runs_and_installs_it_after_quit(desktop_dir, waiters):
    running = subprocess.Popen([str(desktop_dir / "release/linux-unpacked/hermes"), "-c", "sleep 60; :"])
    try:
        wait_until(lambda: main_desktop_pending.live_desktop_pids(desktop_dir) == [running.pid], timeout=10)
        staging = main_desktop._desktop_staging_dir(desktop_dir)
        _pack(staging, "NEW")

        assert main_desktop._promote_staged_desktop_app(desktop_dir, staging, integrity_check=lambda p: None) is None
        assert running.poll() is None, "the running Desktop was stopped"
        assert _stamp(desktop_dir) == "OLD"
        assert waiters == [desktop_dir]
        assert main_desktop_pending.promote_pending_desktop_app(desktop_dir) is None  # still running
    finally:
        running.terminate()
        running.wait()

    assert main_desktop_pending.wait_then_promote(desktop_dir) == desktop_dir / "release/linux-unpacked/hermes"
    assert _stamp(desktop_dir) == "NEW"
    assert not main_desktop_pending.pending_dir(desktop_dir).exists()


def test_update_with_no_desktop_running_swaps_now_and_supersedes_a_parked_build(desktop_dir, waiters):
    parked = main_desktop_pending.pending_dir(desktop_dir)
    _pack(parked, "PARKED")
    (parked / main_desktop_pending.READY_MARKER).write_text("{}", encoding="utf-8")
    staging = main_desktop._desktop_staging_dir(desktop_dir)
    _pack(staging, "NEW")

    assert main_desktop._promote_staged_desktop_app(
        desktop_dir, staging, integrity_check=lambda p: None) == desktop_dir / "release/linux-unpacked/hermes"
    assert _stamp(desktop_dir) == "NEW"
    assert not parked.exists()
    assert waiters == []
