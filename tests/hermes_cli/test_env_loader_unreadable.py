"""A present but unreadable profile .env loads like a missing one.

A sandbox launcher denies the profile's .env to the sandboxed backend and passes its values in as
environment. The backend must still start, and the launcher's values must survive the load.
"""

import logging
import os
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv


def _deny_reads(monkeypatch, denied: Path) -> None:
    """Make *denied* exist but refuse reads, as Windows does for a file a sandbox denies."""
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self == denied:
            raise PermissionError(13, "Access is denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def _profile(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
    return home, env_file


def test_unreadable_user_env_loads_as_absent(tmp_path, monkeypatch, caplog):
    home, env_file = _profile(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "from-launcher")
    _deny_reads(monkeypatch, env_file)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        assert load_hermes_dotenv(hermes_home=home) == []

    assert os.environ["OPENAI_API_KEY"] == "from-launcher"
    assert any("not readable" in r.getMessage() and str(env_file) in r.getMessage() for r in caplog.records)


def test_unreadable_user_env_keeps_inherited_provider_keys(tmp_path, monkeypatch):
    """The keys a readable .env clears when it does not define them are the launcher's here."""
    home, env_file = _profile(tmp_path)
    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "from-launcher")
    _deny_reads(monkeypatch, env_file)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["HERMES_ACP_AUTH_METHOD"] == "from-launcher"


def test_readable_user_env_still_loads(tmp_path, monkeypatch):
    home, env_file = _profile(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "from-launcher")

    assert load_hermes_dotenv(hermes_home=home) == [env_file]
    assert os.environ["OPENAI_API_KEY"] == "from-file"
