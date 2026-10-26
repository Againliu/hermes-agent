"""``hermes profile create NAME --from-prompt TEXT``: draft the bot, show it, save on confirm."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from hermes_cli.profile_draft import ProfileDraft, build_catalog, draft_profile


def _catalog_source(args) -> Path:
    """Skills/MCP come from the profile the new one starts from (clone source, else the active one)."""
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir
    return Path(get_profile_dir(getattr(args, "clone_from", None) or get_active_profile_name()))


def render_draft(name: str, draft: ProfileDraft) -> str:
    lines = [f"Name:        {name}", f"Title:       {draft.title or '(none)'}",
             f"Description: {draft.description or '(none)'}", "", "SOUL.md", "-------", draft.soul.rstrip(), ""]
    for kind, label in (("skills", "Suggested skills"), ("toolsets", "Suggested toolsets"),
                        ("mcp_servers", "Suggested MCP servers")):
        picks = getattr(draft, kind)
        lines.append(f"{label}: {', '.join(picks) if picks else '(none)'}")
    if draft.dropped:
        lines.append("Not installed here, left out: " + ", ".join(f"{d['name']} ({d['kind']})" for d in draft.dropped))
    return "\n".join(lines)


def confirm_draft(args, die) -> Optional[ProfileDraft]:
    """Draft from ``--from-prompt`` and get the user's yes, or ``die`` with a reason (None without
    the flag). Nothing is created before this returns. Non-interactive runs must pass ``--yes``:
    a hidden prompt would hang."""
    if not getattr(args, "from_prompt", None):
        return None
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not getattr(args, "yes", False):
        die("Error: --from-prompt shows a draft and asks before creating; re-run in a terminal or add --yes.")
    print("Drafting the profile with the auxiliary model…", flush=True)
    draft = draft_profile(args.from_prompt, catalog=build_catalog(_catalog_source(args)))
    if not draft.ok:
        die(f"Error: could not draft the profile: {draft.reason}")
    print("\n" + render_draft(args.profile_name, draft) + "\n")
    if getattr(args, "yes", False):
        return draft
    answer = input(f"Create profile '{args.profile_name}' with this draft? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        die("Cancelled — nothing was created.")
    return draft


def apply_draft(profile_dir: Path, draft: Optional[ProfileDraft], *, keep_description: bool) -> None:
    """Write the confirmed draft's identity (an explicit ``--description`` wins); capabilities
    stay printed suggestions — skills are already seeded, toolset/MCP changes are the user's call."""
    if draft is None:
        return
    from hermes_cli.profiles import write_profile_meta
    (profile_dir / "SOUL.md").write_text(draft.soul, encoding="utf-8")
    write_profile_meta(profile_dir, description=None if keep_description else draft.description,
                       description_auto=None if keep_description else False, display_name=draft.title or None)
    if draft.toolsets:
        print(f"Suggested toolsets: {', '.join(draft.toolsets)} — enable with `hermes -p {profile_dir.name} tools`.")
    if draft.mcp_servers:
        print(f"Suggested MCP servers: {', '.join(draft.mcp_servers)} — add with `hermes -p {profile_dir.name} mcp`.")
