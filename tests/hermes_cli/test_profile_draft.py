"""Drafting a bot from a description: catalog-checked capabilities, partial regenerate, no writes.

The aux model is replaced by a canned reply at ``agent.auxiliary_client.call_llm`` (where
``draft_profile`` imports it); everything else — catalog read from a real profile tree, the
``profiles.draft`` RPC through the gateway dispatcher, the CLI create path — runs for real
against a temp ``HERMES_HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import profile_draft

REPLY = {
    "name": "Inbox Triage!",
    "title": "Inbox Triage",
    "description": "Sorts new mail and flags what needs a human.",
    "sections": {
        "role": "You triage the user's inbox.",
        "voice": "Short and plain.",
        "does": "- Label mail\n- Flag urgent threads",
        "doesnt": "- Send replies",
        "silence": "Say nothing when the inbox is empty.",
    },
    "skills": ["himalaya", "made-up-skill"],
    "toolsets": ["web", "teleport"],
    "mcp_servers": ["gmail", "ghost-server"],
}


class _FakeLLM:
    def __init__(self, reply: str):
        self.reply, self.calls = reply, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


@pytest.fixture
def llm(monkeypatch):
    fake = _FakeLLM(json.dumps(REPLY))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)
    return fake


def _catalog() -> profile_draft.Catalog:
    return profile_draft.Catalog(skills=["email/himalaya", "github/github-pr"], toolsets=["web", "terminal"],
                                 mcp_servers=["gmail"])


def test_suggestions_outside_the_target_catalog_are_dropped_and_reported(llm):
    draft = profile_draft.draft_profile("triage my inbox", catalog=_catalog())

    assert draft.ok and draft.name == "inbox-triage"
    assert (draft.skills, draft.toolsets, draft.mcp_servers) == (["email/himalaya"], ["web"], ["gmail"])
    assert {(d["kind"], d["name"]) for d in draft.dropped} == {
        ("skills", "made-up-skill"), ("toolsets", "teleport"), ("mcp_servers", "ghost-server")}
    # The model was only offered what is installed.
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "email/himalaya" in prompt and "ghost-server" not in prompt
    assert profile_draft.split_soul(draft.soul) == REPLY["sections"]


def test_regenerating_one_section_keeps_every_other_part_verbatim(llm):
    current = {"name": "keeper", "title": "Keeper", "description": "Old blurb.",
               "sections": {"role": "Old role.", "voice": "Old voice."}, "toolsets": ["terminal"]}

    draft = profile_draft.draft_profile("triage my inbox", catalog=_catalog(), current=current,
                                        note="friendlier", only="voice")

    assert draft.ok
    assert (draft.name, draft.title, draft.description) == ("keeper", "Keeper", "Old blurb.")
    assert draft.sections == {"role": "Old role.", "voice": "Short and plain.", "does": "", "doesnt": "",
                              "silence": ""}
    assert draft.toolsets == ["terminal"] and draft.dropped == []


@pytest.mark.parametrize("reply", ['{"name": "x", "sections": {"role": "cut', ""])
def test_unusable_reply_is_a_reason_not_an_exception(monkeypatch, reply):
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _FakeLLM(reply))

    draft = profile_draft.draft_profile("anything", catalog=_catalog())

    assert not draft.ok and draft.reason and draft.soul.strip() == ""


# ── RPC + CLI against a temp HERMES_HOME ───────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    from tui_gateway import server
    launch = tmp_path / ".hermes"
    skill = launch / "skills" / "email" / "himalaya"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: himalaya\n---\n", encoding="utf-8")
    (launch / "config.yaml").write_text("mcp_servers:\n  gmail:\n    command: gmail-mcp\n", encoding="utf-8")
    bot = launch / "profiles" / "scout"
    bot.mkdir(parents=True)
    (bot / "SOUL.md").write_text("# Scout\n\n## Role\n\nOld role.\n\n## Messaging other agents\n\nKeep me.\n",
                                 encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    return launch


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_rpc_drafts_from_the_target_catalog_and_writes_nothing(home, llm):
    from tui_gateway import server
    # Loading config seeds the launch home's defaults; the draft itself must not touch any profile.
    before = _tree(home / "profiles")

    resp = server._methods["profiles.draft"](1, {"description": "triage my inbox"})

    result = resp["result"]
    assert result["ok"] and result["skills"] == ["email/himalaya"] and result["mcp_servers"] == ["gmail"]
    assert [s["key"] for s in result["sections"]] == ["role", "voice", "does", "doesnt", "silence"]
    assert "previous" not in result
    assert _tree(home / "profiles") == before

    edit = server._methods["profiles.draft"](2, {"name": "scout", "description": "be terse"})["result"]
    assert edit["previous"]["sections"][0] == {"key": "role", "title": "Role", "text": "Old role."}
    assert "Keep me." in edit["previous"]["soul"]
    assert _tree(home / "profiles") == before


def _create_args(**over):
    base = dict(profile_name="mailbot", clone=False, clone_all=False, no_alias=True, no_skills=True,
                clone_from=None, clone_channels=False, sync_imports=False, description=None,
                from_prompt="triage my inbox", yes=False)
    return SimpleNamespace(**(base | over))


def test_cli_from_prompt_refuses_without_a_tty_unless_yes(home, llm, monkeypatch, capsys):
    from hermes_cli import profile_cmd
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit):
        profile_cmd._profile_create(_create_args())
    assert not (home / "profiles" / "mailbot").exists() and llm.calls == []
    assert "--yes" in capsys.readouterr().out

    profile_cmd._profile_create(_create_args(yes=True))
    created = home / "profiles" / "mailbot"
    assert profile_draft.split_soul((created / "SOUL.md").read_text(encoding="utf-8")) == REPLY["sections"]
    assert "Sorts new mail" in (created / "profile.yaml").read_text(encoding="utf-8")
