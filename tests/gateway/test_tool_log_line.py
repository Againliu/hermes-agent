"""Formatting of the ``tool_progress: log`` line written to tools_calls.log."""

from gateway.run_turn_progress import tool_log_line

TS = "2026-10-25 01:17:15"


def test_tool_without_preview_has_no_dangling_separator():
    # skill_manage / todo / memory have no prepare_tool_preview branch: preview is None, and the
    # old inline format still emitted the ":" separator, leaving a bare "skill_manage:" line.
    assert tool_log_line(TS, "skill_manage", None) == f"{TS}  skill_manage"


def test_empty_preview_is_treated_as_no_preview():
    assert tool_log_line(TS, "todo", "") == f"{TS}  todo"


def test_preview_is_quoted():
    assert tool_log_line(TS, "read_file", "SOUL.md") == f'{TS}  read_file: "SOUL.md"'


def test_quotes_inside_preview_are_escaped():
    # A multi-command terminal preview embeds shell quoting; raw quotes used to break the columns.
    line = tool_log_line(TS, "terminal", 'grep -E "hermes.*(gateway)" x')
    assert line == f'{TS}  terminal: "grep -E \\"hermes.*(gateway)\\" x"'


def test_non_ascii_preview_is_not_escaped():
    assert tool_log_line(TS, "read_file", "/数据/a.csv") == f'{TS}  read_file: "/数据/a.csv"'
