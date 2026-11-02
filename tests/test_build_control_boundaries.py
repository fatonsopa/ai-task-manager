"""Module boundaries and build-command outcomes, including real child processes."""

import ast
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.build_control import BuildControl, ControlError
from tools.build_support import cli, ledger, values

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "tools" / "build_support"
SCRIPT = ROOT / "tools" / "build_control.py"
DEPENDENCIES = {
    "values": set(),
    "storage": {"values", "policy"},
    "checkpoint": {"values"},
    "ledger": {
        "values",
        "storage",
        "checkpoint",
        "amendments",
        "batches",
        "authorization",
        "policy",
    },
    "policy": {"values"},
    "authorization": {"values"},
    "amendments": {"values", "storage", "checkpoint", "policy", "authorization"},
    "batches": {"values", "storage", "amendments", "policy", "authorization"},
    "process_group": {"values"},
    "process": {"values", "ledger", "checkpoint", "process_group"},
    "cli": {"values", "ledger", "process", "authorization"},
}


def test_internal_imports_follow_component_direction():
    for path in PACKAGE.glob("*.py"):
        allowed = DEPENDENCIES.get(path.stem, set())
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1
                    if node.module is None:
                        assert all(alias.name in allowed for alias in node.names), path.name
                    else:
                        assert node.module in allowed, (path.name, node.module)
                else:
                    assert not node.module.startswith(("tools.", "build_support."))
            if isinstance(node, ast.Import):
                for name in node.names:
                    assert not name.name.startswith(("tools.", "build_support."))


def test_only_storage_executes_sql_and_opens_sqlite_connections():
    for path in PACKAGE.glob("*.py"):
        if path.stem == "storage":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"execute", "executemany", "executescript"}, path.name
                assert not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sqlite3"
                    and node.func.attr == "connect"
                ), path.name


def test_facade_exports_the_same_control_and_error_types():
    assert BuildControl is ledger.BuildControl
    assert ControlError is values.ControlError


def initialize(path):
    control = BuildControl(path)
    control.initialize(run_id="entry-test", started_at=datetime.now(UTC))
    return control


def run_cli(ledger_path, *arguments, cwd=None):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(ledger_path), *arguments],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=environment,
    )


def test_absolute_entrypoint_runs_from_an_unrelated_directory(tmp_path):
    directory = tmp_path / "unrelated"
    directory.mkdir()
    path = tmp_path / "ledger.sqlite"
    initialized = run_cli(
        path,
        "init",
        "--run-id",
        "independent",
        "--started-at",
        datetime.now(UTC).isoformat(),
        cwd=directory,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["run_id"] == "independent"
    status = run_cli(path, "status", cwd=directory)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["active_workers"] == 0


@pytest.mark.parametrize(
    "program,status,exit_code",
    [
        ("print('complete')", "succeeded", 0),
        ("raise SystemExit(7)", "failed", 1),
        ("import time; time.sleep(10)", "failed", 1),
    ],
)
def test_cli_run_exit_status_agrees_with_actual_child_outcome(tmp_path, program, status, exit_code):
    path = tmp_path / "ledger.sqlite"
    control = initialize(path)
    result = run_cli(
        path,
        "run",
        "child",
        "--timeout-seconds",
        "0.1",
        "--output-directory",
        str(tmp_path / "traces"),
        "--command-json",
        json.dumps([sys.executable, "-c", program]),
    )
    assert result.returncode == exit_code, result.stderr
    output = json.loads(result.stdout)
    assert output["call"]["status"] == status
    assert output["call"]["process_stopped"] is True
    assert control.snapshot()["calls"][0]["status"] == status
    assert control.snapshot()["active_workers"] == 0
    if "sleep" in program:
        assert output["call"]["outcome"] == "interrupted"
        assert output["call"]["reason"] == "timeout"


def test_cli_control_refusal_is_distinct_and_does_not_launch(tmp_path):
    path = tmp_path / "ledger.sqlite"
    control = initialize(path)
    result = run_cli(
        path,
        "run",
        "unknown-charge",
        "--paid",
        "--timeout-seconds",
        "1",
        "--output-directory",
        str(tmp_path / "traces"),
        "--command-json",
        json.dumps([sys.executable, "-c", "print('never')"]),
    )
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr)["error"]["code"] == "unknown_cost"
    assert control.snapshot()["calls"] == []
    assert not (tmp_path / "traces").exists()


def test_cli_uncertain_outcome_is_nonzero_with_structured_result(tmp_path, monkeypatch, capsys):
    # A mechanical presentation control, not a substitute for real provider execution.
    result = {"call": {"status": "unknown", "process_stopped": False}, "returncode": None}
    monkeypatch.setattr(cli, "run_command", lambda *args, **kwargs: result)
    exit_code = cli.main(
        [
            "--ledger",
            str(tmp_path / "ledger.sqlite"),
            "run",
            "uncertain",
            "--timeout-seconds",
            "1",
            "--output-directory",
            str(tmp_path),
            "--command-json",
            '["unexecuted-control"]',
        ]
    )
    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == result
