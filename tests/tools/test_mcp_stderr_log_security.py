import os
from pathlib import Path


def test_mcp_stderr_log_redacts_secrets_and_bounds_retention(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from tools import mcp_tool_config as config

    config._close_mcp_stderr_logs()
    canary = "sk-proj-" + "canarycredential" * 4
    try:
        for _ in range(config._MCP_STDERR_BACKUP_COUNT + 3):
            tee = config._StderrTee(config._get_mcp_stderr_log())
            tee.sink.write((f"credential={canary} " + "x" * config._MCP_STDERR_MAX_BYTES + "\n").encode())
            tee.close()
    finally:
        config._close_mcp_stderr_logs()

    logs = sorted((home / "logs").glob("mcp-stderr.log*"))
    assert 1 <= len(logs) <= config._MCP_STDERR_BACKUP_COUNT + 1
    retained = b"".join(path.read_bytes() for path in logs)
    assert canary.encode() not in retained
    assert b"credential=" in retained
    assert sum(path.stat().st_size for path in logs) <= (
        config._MCP_STDERR_MAX_BYTES * (config._MCP_STDERR_BACKUP_COUNT + 1)
    )
