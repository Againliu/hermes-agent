"""ctx.sessions / `hermes sessions show --json`: a read-only, profile-bound projection of state.db."""

import argparse
import json

from hermes_cli import sessions_cmd
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.subcommands.sessions import build_sessions_parser
from hermes_state import SessionDB


def _seed(home):
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="cli", model="m", system_prompt="SECRET PROMPT SENTINEL")
        db.append_message("root", "user", "build the thing")
        db.update_token_counts("root", input_tokens=100, output_tokens=10, estimated_cost_usd=0.5)
        db.create_session("sub", source="cli", model="m", parent_session_id="root",
                          model_config={"_delegate_from": "root"})
        db.update_token_counts("sub", input_tokens=40, output_tokens=4, estimated_cost_usd=0.25)
        db.create_session("grandsub", source="cli", model="m", parent_session_id="sub",
                          model_config={"_delegate_from": "sub"})
        db.update_token_counts("grandsub", input_tokens=7, output_tokens=1)


def _ctx(home):
    manager = PluginManager(scope_key=str(home))
    return PluginContext(PluginManifest(name="reader", key="reader", source="user"), manager)


def test_ctx_sessions_reads_its_own_profile_store_read_only(tmp_path, monkeypatch):
    launch, served = tmp_path / "launch", tmp_path / "served"
    launch.mkdir(), served.mkdir()
    _seed(served)
    monkeypatch.setenv("HERMES_HOME", str(launch))  # the ambient profile is NOT the plugin's
    before = (served / "state.db").stat().st_mtime_ns
    sessions = _ctx(served).sessions

    row = sessions.get("ro")  # unique prefix
    assert row["id"] == "root" and row["input_tokens"] == 100 and row["delegate_from"] is None
    assert "SECRET PROMPT SENTINEL" not in json.dumps(row) and "system_prompt" not in row
    assert [r["id"] for r in sessions.recent()] == ["root"]
    assert {r["id"]: r["delegate_from"] for r in sessions.subagents("root")} == {"sub": "root", "grandsub": "sub"}
    assert sessions.usage("root")["input_tokens"] == 100
    tree = sessions.usage("root", include_subagents=True)
    assert (tree["input_tokens"], tree["output_tokens"], sorted(tree["session_ids"])) == (
        147, 15, ["grandsub", "root", "sub"])
    assert tree["estimated_cost_usd"] == 0.75
    assert [(m["role"], m["content"]) for m in sessions.messages("root")] == [("user", "build the thing")]
    assert (served / "state.db").stat().st_mtime_ns == before
    assert not (launch / "state.db").exists()
    # A profile that never created a store answers empty instead of creating one.
    fresh = _ctx(launch).sessions
    assert (fresh.get("root"), fresh.recent(), fresh.usage("root")) == (None, [], None)
    assert not (launch / "state.db").exists()


def _run(capsys, *argv):
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(), cmd_sessions=sessions_cmd.cmd_sessions)
    code = sessions_cmd.cmd_sessions(parser.parse_args(["sessions", *argv]))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_sessions_show_json_is_the_plugin_projection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)
    code, out, _ = _run(capsys, "show", "root", "--json")
    detail = json.loads(out)
    assert code is None
    assert next(iter(detail)) == "schema_version" and detail["schema_version"] == 1
    assert detail["session"] == _ctx(tmp_path).sessions.get("root")
    assert [s["id"] for s in detail["subagents"]] == ["sub", "grandsub"]
    assert detail["usage"]["input_tokens"] == 147
    assert "SECRET PROMPT SENTINEL" not in out
    # A miss under --json keeps stdout empty for `| jq` and reports on stderr with exit 2.
    code, out, err = _run(capsys, "show", "nope", "--json")
    assert (code, out) == (2, "") and "nope" in err
