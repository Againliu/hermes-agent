"""How the exact-value registry loads: concurrent first readers, bad entries, frozen workers."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent.redact_known_values import MARKER, clear_known_secret_values

# Built by concatenation: opaque, no vendor prefix, never a real credential.
ENV_VAL = "opaqueEnv" + "Value8812qz"
SNAPSHOT_VAL = "frozenSnap" + "shotKey4471x"


@pytest.fixture(autouse=True)
def _fresh_registry():
    clear_known_secret_values()
    yield
    clear_known_secret_values()


@pytest.mark.parametrize("safe_mode", [True, False])
def test_frozen_worker_masks_its_snapshot_without_opening_profile_files(tmp_path, safe_mode):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(f"ACME_SERVICE_TOKEN={ENV_VAL}\n", encoding="utf-8")
    (home / "config.yaml").write_text("model:\n  api_key: fileOnlyValue99231x\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    script = home / "probe.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(root)!r})\n" + textwrap.dedent(f"""
        import json, sys
        from agent.safe_worker_policy import _bind_safe_worker_policy
        _bind_safe_worker_policy(safe_mode={safe_mode}, ignore_user_config=True,
                                 config={{"model": {{"api_key": {SNAPSHOT_VAL!r}}}}})
        opens = []
        def audit(event, args):
            if event == "open" and str(args[0]).endswith((".env", "config.yaml")):
                opens.append(str(args[0]))
        sys.addaudithook(audit)
        from agent.redact import redact_sensitive_text
        out = redact_sensitive_text("echo {SNAPSHOT_VAL}", force=True)
        print(json.dumps({{"out": out, "opens": opens}}))
    """), encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home))
    result = subprocess.run([sys.executable, str(script)], env=env, cwd=root, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    assert probe == {"out": f"echo {MARKER}", "opens": []}
