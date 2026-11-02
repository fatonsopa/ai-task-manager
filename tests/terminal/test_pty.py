"""Actual local PTY/CLI execution; no installed Claude/Codex acceptance claim."""

import sys

import pytest

from ai_task_manager.domains.work.models import TaskProse
from ai_task_manager.presentation.cli.main import main
from tests.application.helpers import ServiceRig

from .pty_driver import Terminal, color_codes


def fixture_task(fixture, *, body=None, display_identifier="TST-9001"):
    # Synthetic exact provider readback; no scheduling declaration or live issue.
    rig = fixture.rig
    prose = TaskProse(
        "Verify session isolation",
        body or "Run `check --scope=session` and retain its result.",
        False,
    )
    task = rig.core.intake.submit(rig.handle, prose, rig.approval(prose), rig.key())
    operation = rig.core.sync.list_operations(rig.handle)[-1]
    reference = rig.proof(
        "provider_status",
        task.id,
        {
            "operation_id": operation.id,
            "status": task.desired_status,
            "prose_digest": task.prose_digest,
            "title": task.title,
            "body": task.body,
            "representation": task.representation,
            "issue_id": "TST:" + task.id,
            "display_identifier": display_identifier,
        },
    )
    return rig.core.intake.confirm_status(
        rig.handle, task.id, task.version, task.decision_revision, reference, rig.key()
    )


def test_actual_cli_passive_status_uses_scoped_read_without_mutation(tmp_path, capsys):
    fixture = ServiceRig(tmp_path / "product")
    task = fixture_task(fixture)
    config = fixture.configuration()
    before = fixture.rig.core.get_task(fixture.rig.handle, task.id)
    assert main(["--config", str(config), "--ascii", "view", "status"]) == 0
    assert "Tasks 1" in capsys.readouterr().out
    assert fixture.rig.core.get_task(fixture.rig.handle, task.id) == before


def test_actual_pty_keyboard_mouse_resize_and_no_color(tmp_path):
    fixture = ServiceRig(tmp_path / "product")
    fixture_task(fixture)
    config = fixture.configuration()
    terminal = Terminal(
        [
            sys.executable,
            "-c",
            "from ai_task_manager.presentation.cli.main import main; raise SystemExit(main())",
            "--config",
            str(config),
            "--ascii",
            "--no-color",
            "view",
            "sidebar",
        ],
        tmp_path / "pty",
        environment={"NO_COLOR": "1"},
    )
    try:
        terminal.wait(lambda s: "Session tasks" in s and "Backlog (1)" in s, label="initial")
        # Inspect the actual focus style rather than assuming an initial focus
        # position or a fixed number of Tab stops (the scroll region is focusable).
        focused = False
        for _ in range(6):
            terminal.send(b"\t")
            terminal.pump(0.15)
            row = next(i for i, line in enumerate(terminal.screen.display) if "TST-9001" in line)
            if any(cell.bold for cell in terminal.screen.buffer[row].values()):
                focused = True
                break
        terminal.frame("task-keyboard-focus")
        assert focused
        terminal.send(b"\r")
        terminal.wait(lambda s: "Run `check --scope=session`" in s, label="expanded-by-keyboard")
        terminal.send(b"h")
        terminal.wait(lambda s: "[show]" in s and "Backlog" not in s, label="hidden")
        # Actual SGR terminal mouse press/release on the right header control.
        terminal.send(b"\x1b[<0;40;1M\x1b[<0;40;1m")
        terminal.wait(lambda s: "[hide]" in s and "Backlog" in s, label="shown-by-mouse")
        terminal.resize(28, 14)
        terminal.wait(lambda s: "Backlog (1)" in s, label="resized")
        terminal.send(b"q")
        terminal.pump(0.2)
        terminal.frame("view-quit")
        # NO_COLOR permits control/style codes but emits no foreground/background colors.
        assert not color_codes(terminal.raw)
    finally:
        terminal.close()
    assert terminal.closed["group"]["confirmed_stop"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"\x1b[31m", True),
        (b"\x1b[38;2;255;0;0m", True),
        (b"\x1b[48;5;1m", True),
        (b"\x1b[1;39;49m", False),
    ],
)
def test_color_oracle_distinguishes_selection_from_default_reset(raw, expected):
    assert bool(color_codes(raw)) is expected


def test_actual_pty_five_wrapped_lines_more_less_and_whole_scroll(tmp_path):
    fixture = ServiceRig(tmp_path / "product")
    body = " ".join(f"word{i:02}" for i in range(80)) + "\nTAIL AFTER SHOW MORE"
    task = fixture_task(fixture, body=body, display_identifier=None)
    config = fixture.configuration()
    terminal = Terminal(
        [
            sys.executable,
            "-c",
            "from ai_task_manager.presentation.cli.main import main; raise SystemExit(main())",
            "--config",
            str(config),
            "--no-color",
            "view",
            "sidebar",
        ],
        tmp_path / "pty",
        environment={"NO_COLOR": "1"},
    )

    def click_label(label):
        row = next(i for i, line in enumerate(terminal.screen.display) if label in line)
        terminal.send(f"\x1b[<0;6;{row + 1}M\x1b[<0;6;{row + 1}m".encode())

    try:
        terminal.wait(lambda s: "local:" in s and "Verify session" in s, label="local-id-compact")
        terminal.send(b"\x1b[<0;10;4M\x1b[<0;10;4m")
        terminal.wait(lambda s: "word29" in s and "[show more]" in s, label="five-wrapped-lines")
        assert "word30" not in "\n".join(terminal.screen.display)
        assert ("local:" + task.id) in "\n".join(terminal.screen.display)
        click_label("[show more]")
        terminal.pump(0.2)
        terminal.send(b"\x1b[F")
        terminal.wait(
            lambda s: "TAIL AFTER SHOW MORE" in s and "[show less]" in s,
            label="whole-sidebar-scroll",
        )
        assert "Session tasks" in "\n".join(terminal.screen.display)
        assert "Backlog (1)" not in "\n".join(terminal.screen.display)
        click_label("[show less]")
        terminal.wait(lambda s: "word29" in s and "word30" not in s, label="preview-restored")
    finally:
        terminal.close()
