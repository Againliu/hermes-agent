"""Uninstall only ever deletes a directory that is provably a Hermes home."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import uninstall


@pytest.fixture
def user_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A user home with personal files, a real Hermes home, and a git checkout outside both."""
    user_home = tmp_path / "users" / "me"
    (user_home / "Documents").mkdir(parents=True)
    (user_home / "Documents" / "taxes.txt").write_text("keep", encoding="utf-8")
    (user_home / "Pictures").mkdir()
    (user_home / "Pictures" / "a.jpg").write_text("keep", encoding="utf-8")
    # Launching any ``hermes`` command seeds SOUL.md and the skeleton into HERMES_HOME;
    # that alone must not make a directory a Hermes home.
    (user_home / "Pictures" / "SOUL.md").write_text("seeded", encoding="utf-8")
    (user_home / "Pictures" / "sessions").mkdir()
    hermes_home = user_home / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    project = tmp_path / "src" / "hermes-agent"
    (project / ".git").mkdir(parents=True)
    (project / "run_agent.py").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setattr(uninstall, "get_project_root", lambda: project)
    return tmp_path, user_home, hermes_home, project


def _link(link: Path, target: Path) -> Path:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    return link


_UNSAFE_HOMES = {
    "user-home": lambda root, user, hermes: user,
    "ancestor-of-home": lambda root, user, hermes: user.parent,
    "filesystem-root": lambda root, user, hermes: Path(root.anchor),
    "link-to-user-home": lambda root, user, hermes: _link(root / "hermes-link", user),
    "dir-without-hermes-marker": lambda root, user, hermes: user / "Pictures",
    "linked-hermes-home": lambda root, user, hermes: _link(root / "linked-hermes", hermes),
}


@pytest.mark.parametrize("mode", ["full", "lite", "data"])
@pytest.mark.parametrize("case", list(_UNSAFE_HOMES))
def test_uninstall_refuses_a_home_it_cannot_prove_is_hermes(user_tree, monkeypatch, mode, case):
    root, user_home, hermes_home, _project = user_tree
    if case == "linked-hermes-home" and mode != "full":
        pytest.skip("only the whole-tree wipe refuses a linked home; the other modes follow it")
    home = _UNSAFE_HOMES[case](root, user_home, hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    before = sorted(p.relative_to(root) for p in root.rglob("*"))

    with pytest.raises(SystemExit) as refused:
        uninstall.main(["--mode", mode])

    assert refused.value.code == 1
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_full_uninstall_still_wipes_a_real_home_reached_through_a_linked_parent(
    user_tree, monkeypatch, capsys,
):
    """A link ABOVE the home (Fedora Atomic's /home -> /var/home, macOS /var) is not a refusal."""
    root, user_home, hermes_home, project = user_tree
    linked_users = _link(root / "home-link", user_home.parent)
    monkeypatch.setenv("HERMES_HOME", str(linked_users / "me" / ".hermes"))
    for step in ("uninstall_gateway_service", "remove_path_from_shell_configs", "remove_wrapper_script",
                 "remove_node_symlinks", "remove_desktop_app_leftovers", "remove_dashboard_launchd_jobs",
                 "_macos_cache_leftover_dirs", "_discover_named_profiles", "remove_path_from_windows_registry",
                 "remove_hermes_env_vars_windows", "remove_windows_bin_launchers"):
        monkeypatch.setattr(uninstall, step, lambda *a, **k: [])
    monkeypatch.setattr("hermes_cli.gui_uninstall.uninstall_gui", lambda *a, **k: False)

    uninstall.main(["--mode", "full"])

    assert not hermes_home.exists()
    assert not project.exists()
    assert (user_home / "Documents" / "taxes.txt").read_text(encoding="utf-8-sig") == "keep"
    assert "Uninstall Complete" in capsys.readouterr().out
