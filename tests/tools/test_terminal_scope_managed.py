"""Managed scope pins the per-turn terminal policy the way it pins the standalone bridge.

A sandbox launcher pins ``terminal.backend`` through managed scope and withholds the profile's .env.
The per-turn scope must take the pinned backend over the profile's own ``config.yaml``, and an
unreadable .env must block turns only while nothing pins the backend.
"""

import textwrap
from pathlib import Path

import pytest

from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    md = tmp_path / "managed"
    md.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(md))
    managed_scope.invalidate_managed_cache()
    yield md
    managed_scope.invalidate_managed_cache()


def _write_managed(md: Path, body: str) -> None:
    from hermes_cli import managed_scope

    (md / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    managed_scope.invalidate_managed_cache()


def _profile(tmp_path: Path, *, config_yaml: str = "", dotenv: str = "") -> Path:
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    if config_yaml:
        (home / "config.yaml").write_text(textwrap.dedent(config_yaml), encoding="utf-8")
    if dotenv:
        (home / ".env").write_text(dotenv, encoding="utf-8")
    return home


def _deny_reads(monkeypatch, denied: Path) -> None:
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self == denied:
            raise PermissionError(13, "Access is denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_managed_backend_beats_profile_config(tmp_path, managed):
    _write_managed(managed, "terminal:\n  backend: busybox\n")
    home = _profile(tmp_path, config_yaml="terminal:\n  backend: local\n  timeout: 77\n")

    scope = build_profile_terminal_scope(home)

    assert scope["TERMINAL_ENV"] == "busybox"
    assert scope["TERMINAL_TIMEOUT"] == "77"


def test_unreadable_env_builds_when_backend_is_pinned(tmp_path, managed, monkeypatch):
    _write_managed(managed, "terminal:\n  backend: busybox\n")
    home = _profile(tmp_path, dotenv="TERMINAL_ENV=docker\n")
    _deny_reads(monkeypatch, home / ".env")

    scope = build_profile_terminal_scope(home, env_overlay={"TERMINAL_TIMEOUT": "42"})

    assert scope["TERMINAL_ENV"] == "busybox"
    assert scope["TERMINAL_TIMEOUT"] == "42"


def test_unreadable_env_fails_closed_without_a_pinned_backend(tmp_path, managed, monkeypatch):
    _write_managed(managed, "terminal:\n  timeout: 5\n")
    home = _profile(tmp_path, dotenv="TERMINAL_ENV=docker\n")
    _deny_reads(monkeypatch, home / ".env")

    with pytest.raises(TerminalPolicyUnavailable):
        build_profile_terminal_scope(home)


def test_managed_backend_beats_env_and_launch_overlay(tmp_path, managed):
    _write_managed(managed, "terminal:\n  backend: busybox\n")
    home = _profile(tmp_path, dotenv="TERMINAL_ENV=docker\n")

    scope = build_profile_terminal_scope(home, env_overlay={"TERMINAL_ENV": "ssh"})

    assert scope["TERMINAL_ENV"] == "busybox"


def test_managed_values_expand_against_the_process_environment(tmp_path, managed, monkeypatch):
    monkeypatch.setenv("LAB_PINNED_BACKEND", "busybox")
    _write_managed(managed, "terminal:\n  backend: ${LAB_PINNED_BACKEND}\n")
    home = _profile(tmp_path, config_yaml="terminal:\n  backend: local\n")

    assert build_profile_terminal_scope(home)["TERMINAL_ENV"] == "busybox"


def test_no_managed_scope_keeps_the_profile_backend(tmp_path, managed):
    home = _profile(tmp_path, config_yaml="terminal:\n  backend: local\n")

    assert build_profile_terminal_scope(home)["TERMINAL_ENV"] == "local"
