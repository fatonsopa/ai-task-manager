"""Actual private tmux and PTY panes, with no account/model or normal config."""

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.presentation.terminal.layout import LayoutUnavailable, OwnedLayout
from ai_task_manager.presentation.terminal.pane_control import PaneControl
from tests.application.helpers import ServiceRig

from .test_pty import fixture_task


def tmux_binary():
    binary = Path.cwd() / ".local/tools/tmux-install/bin/tmux"
    if not binary.exists():
        pytest.skip("Actual private tmux fixture dependency is unavailable")
    return binary


def wait(check, seconds=5):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("Owned terminal condition not observed")


def stopped_panes(pids):
    reply = subprocess.run(
        ["ps", "-p", ",".join(map(str, pids)), "-o", "pid=,stat="],
        capture_output=True,
        text=True,
        timeout=2,
    )
    return not reply.stdout.strip() and reply.returncode in {0, 1}


def test_actual_owned_layout_keeps_host_space_and_closing_view_keeps_host(tmp_path):
    product = ServiceRig(tmp_path / "product")
    fixture_task(product)
    config = product.configuration()
    source = Path(__file__).with_name("native_fixture.py").resolve()
    root = tmp_path / "layout"
    layout = OwnedLayout(
        tmux_binary(),
        root,
        environment={"PYTHONPATH": str(Path.cwd()) + ":" + str(Path.cwd() / "src")},
    )
    stages = []
    pids = []
    try:
        layout.create(
            [sys.executable, str(source), "host"],
            [sys.executable, str(source), "sidebar", str(config), str(root / "context.json")],
        )
        layout.write_context(product.service)
        panes = layout.panes()
        pids = [row["pid"] for row in panes]
        assert len(panes) == 2 and all(row["height"] == 30 for row in panes)
        assert next(row for row in panes if row["id"] == layout.sidebar_pane)["left"] > 0
        wait(lambda: "Backlog (1)" in layout.capture(layout.sidebar_pane))
        host_before = layout.capture(layout.host_pane)
        assert host_before.splitlines()[-1] == "NATIVE BOTTOM"
        assert layout._command("show-options", "-gv", "status") == "off"
        stages.append(
            {
                "stage": "shown",
                "panes": panes,
                "host": host_before,
                "sidebar": layout.capture(layout.sidebar_pane),
            }
        )
        # The actual terminal peer receives an SGR click inside its task row.
        layout._command("send-keys", "-t", layout.sidebar_pane, "-l", "\x1b[<0;10;4M\x1b[<0;10;4m")
        wait(lambda: "Run `check" in layout.capture(layout.sidebar_pane))
        assert layout.panes() == panes
        assert layout.capture(layout.host_pane) == host_before
        stages.append({"stage": "details", "sidebar": layout.capture(layout.sidebar_pane)})
        layout._command("send-keys", "-t", layout.sidebar_pane, "-l", "h")
        wait(
            lambda: (
                next(row for row in layout.panes() if row["id"] == layout.sidebar_pane)["width"]
                == 8
            )
        )
        wait(lambda: "[show]" in layout.capture(layout.sidebar_pane))
        assert layout.capture(layout.host_pane).splitlines()[-1] == "NATIVE BOTTOM"
        stages.append(
            {"stage": "hidden", "panes": layout.panes(), "host": layout.capture(layout.host_pane)}
        )
        layout._command("send-keys", "-t", layout.sidebar_pane, "-l", "h")
        wait(
            lambda: (
                next(row for row in layout.panes() if row["id"] == layout.sidebar_pane)["width"]
                == 40
            )
        )
        layout._command("resize-window", "-t", "aitm", "-x", "96", "-y", "24")
        wait(lambda: all(row["height"] == 24 for row in layout.panes()))
        wait(lambda: layout.capture(layout.host_pane).splitlines()[-1] == "NATIVE BOTTOM")
        stages.append(
            {"stage": "resized", "panes": layout.panes(), "host": layout.capture(layout.host_pane)}
        )
        layout._command("send-keys", "-t", layout.sidebar_pane, "-l", "q")
        wait(lambda: len(layout.panes(complete=False)) == 1)
        layout._command("send-keys", "-t", layout.host_pane, "ping", "Enter")
        wait(lambda: "HOST GOT ping" in layout.capture(layout.host_pane))
        stages.append({"stage": "view_closed_host_alive", "host": layout.capture(layout.host_pane)})
    finally:
        (tmp_path / "actual-layout-stages.json").write_text(
            json.dumps(stages, indent=2, sort_keys=True) + "\n"
        )
        closed = layout.close()
        assert closed["server"]["confirmed_stop"]
        assert closed["panes_stopped"]
        if pids:
            wait(lambda: stopped_panes(pids), seconds=3)


def test_layout_context_refuses_another_session_without_resize(tmp_path):
    product = ServiceRig(tmp_path / "product")
    fixture_task(product)
    source = Path(__file__).with_name("native_fixture.py").resolve()
    layout = OwnedLayout(tmux_binary(), tmp_path / "layout")
    try:
        layout.create(
            [sys.executable, str(source), "host"],
            [sys.executable, "-c", "import time; time.sleep(20)"],
        )
        path = layout.write_context(product.service)
        panes = layout.panes()
        product.service.scope = replace(product.service.scope, logical_session="other-session")
        with pytest.raises(ValueError, match="another session"):
            PaneControl(path, product.service)
        assert layout.panes() == panes
    finally:
        closed = layout.close()
        assert closed["server"]["confirmed_stop"] and closed["panes_stopped"]


def test_owned_layout_start_failure_retains_stopped_cleanup(tmp_path):
    layout = OwnedLayout(tmux_binary(), tmp_path / "layout")
    try:
        with pytest.raises(LayoutUnavailable):
            layout.create(
                ["/owned-fixture-missing-executable"], ["/owned-fixture-missing-executable"]
            )
    finally:
        closed = layout.close()
        assert closed["server"]["confirmed_stop"] and closed["panes_stopped"]
