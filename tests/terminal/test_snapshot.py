"""Real scoped application reads plus authored activity/text controls."""

from dataclasses import replace

import pytest
from rich.cells import cell_len

from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.presentation.terminal.snapshot import SessionReader, activity, status_line
from ai_task_manager.presentation.terminal.text import literal, rendered_lines
from tests.domains.helpers import Rig


def test_two_sessions_and_accounts_read_only_their_own_tasks(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    first = rig.task()
    first_reader = SessionReader(ApplicationService(rig.core, rig.handle))
    rig.scope = replace(rig.scope, principal="user-b", logical_session="session-b")
    rig.handle = rig.session(rig.scope)
    second = rig.task()
    second_reader = SessionReader(ApplicationService(rig.core, rig.handle))
    with rig.db.transaction() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM tasks")]
    assert {t.id for t in first_reader.snapshot().tasks} == {first.id}
    assert {t.id for t in second_reader.snapshot().tasks} == {second.id}
    assert "Tasks 1" in status_line(first_reader.snapshot())
    with rig.db.transaction() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM tasks")] == before


@pytest.mark.parametrize(
    "state", ["running", "merging", "verifying", "paused", "failed", "reserved", "starting"]
)
def test_activity_uses_observed_execution_not_prose(state):
    assert (
        activity({"sync_state": "confirmed", "execution_state": state, "title": "Done merging"})
        == state
    )


@pytest.mark.parametrize(
    "data,expected",
    [
        (
            {"sync_state": "confirmed", "execution_state": "idle", "title": "Merging done"},
            "unknown",
        ),
        ({"sync_state": "pending", "execution_state": "running"}, "unknown"),
        ({"sync_state": "confirmed", "execution_state": "stopping_or_unresolved"}, "unknown"),
        (
            {"sync_state": "confirmed", "execution_state": "running", "activity": "blocked"},
            "blocked",
        ),
        (
            {
                "sync_state": "confirmed",
                "execution_state": "idle",
                "observed_status": "Done",
                "activity": "blocked",
            },
            "done",
        ),
        (
            {
                "sync_state": "confirmed",
                "execution_state": "idle",
                "observed_status": "Todo",
                "queued_once": True,
            },
            "queued",
        ),
        ({"sync_state": "confirmed", "activity": "invented"}, "unknown"),
    ],
)
def test_pending_unknown_and_provider_terminal_facts_remain_distinct(data, expected):
    assert activity(data) == expected


def test_untrusted_terminal_markup_controls_and_unicode_are_literal():
    text = "[bold]literal[/bold]\x1b]52;c;PRIVATE\x07\x1b[31m red\r\n👩‍💻界 e\u0301\u202e"
    safe = literal(text, multiline=True)
    assert "PRIVATE" not in safe and "\x1b" not in safe and "\u202e" not in safe
    assert "[bold]literal[/bold]" in safe and "👩‍💻界 e\u0301" in safe
    lines = rendered_lines(safe, 12)
    assert all(cell_len(line) <= 12 for line in lines)
    assert len(lines) > 2


def test_failed_or_changed_authority_clears_all_old_task_prose(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    rig.task()
    service = ApplicationService(rig.core, rig.handle)
    reader = SessionReader(service)
    assert reader.snapshot().tasks
    service.scope = replace(service.scope, logical_session="foreign-session")
    assert not reader.snapshot().available and reader.snapshot().tasks == ()
