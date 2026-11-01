import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.config.files import FileSeal, read_private, strict_json, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError, ValidationError
from ai_task_manager.presentation.cli.render import ISSUE_SYMBOLS, task_lines

from .helpers import ServiceRig


def run_cli(config, *arguments, input_data=None):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "ai_task_manager", "--config", str(config), *arguments],
        input=input_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_a01_private_json_and_changed_configuration_are_refused(tmp_path):
    path = tmp_path / "private.json"
    os.chmod(tmp_path, 0o700)
    write_private(path, {"a": 1})
    assert path.stat().st_mode & 0o777 == 0o600
    seal = FileSeal(path)
    write_private(path, {"a": 2}, replace=True)
    with pytest.raises(AccessDenied):
        seal.check()
    os.chmod(path, 0o644)
    with pytest.raises(AccessDenied):
        read_private(path)
    for raw in ['{"a":1,"a":2}', '{"a":NaN}', b"\xff", "x" * 262145]:
        with pytest.raises(ValidationError):
            strict_json(raw)


def test_a25_authority_rows_are_durable_signed_and_unknown_schema_refuses(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    ref = env.decisions.decision(env.rig.scope, task, "Todo", "enqueue", grant_ref="host-grant")
    reopened = AuthorityJournal(env.authority.path, env.authority.key_path)
    assert reopened.verify_evidence(ref).payload["actor"] == "agent"
    with sqlite3.connect(reopened.path) as conn:
        conn.execute("UPDATE records SET data='{}' WHERE ref=?", (ref,))
    with pytest.raises(AccessDenied):
        reopened.verify_evidence(ref)
    with sqlite3.connect(reopened.path) as conn:
        conn.execute("UPDATE authority_version SET version=999")
    with pytest.raises(SchemaError):
        AuthorityJournal(env.authority.path, env.authority.key_path)


def test_a20_actual_cli_scoped_read_json_and_exit_errors(tmp_path):
    env = ServiceRig(tmp_path)
    own = env.rig.task(confirmed=False)
    foreign = env.rig.session(replace(env.rig.scope, logical_session="foreign"))
    foreign_task = env.rig.task(handle=foreign, confirmed=False)
    config = env.configuration()
    response = run_cli(config, "--json", "task", "list")
    assert response.returncode == 0, response.stderr
    payload = json.loads(response.stdout)
    assert [row["id"] for row in payload["data"]] == [own.id]
    assert payload["data"][0]["execution_state"] == "pending_intake"
    assert "capability" not in response.stdout
    denied = run_cli(
        config,
        "--json",
        "task",
        "show",
        "--input",
        "-",
        input_data=json.dumps({"task_id": foreign_task.id}),
    )
    assert denied.returncode == 3
    assert json.loads(denied.stderr)["outcome"] == "denied"
    invalid = run_cli(
        config, "--json", "task", "show", "--input", "-", input_data='{"task_id":"a","task_id":"b"}'
    )
    assert invalid.returncode == 2


def test_a21_status_symbols_distinct_no_color_and_control_text_is_safe(tmp_path):
    assert len({row[0] for row in ISSUE_SYMBOLS.values()}) == len(ISSUE_SYMBOLS)
    env = ServiceRig(tmp_path)
    task = env.rig.task(confirmed=False)
    data = {**vars(task), "title": "untrusted\x1b[31m title\nnext"}
    rendered = task_lines([data], color=False)
    assert "\x1b" not in rendered and "execution=pending_intake" in rendered
    config = env.configuration()
    response = run_cli(config, "--no-color", "task", "list")
    assert response.returncode == 0
    assert "Pending intake" in response.stdout and "\x1b" not in response.stdout


def test_a01_foreign_session_override_and_changed_config_refuse(tmp_path):
    env = ServiceRig(tmp_path)
    config = env.configuration()
    with pytest.raises(AccessDenied):
        load_service(config, tmp_path / "another.json")
    service = load_service(config)
    data = read_private(config)
    data["grant_ref"] = "changed"
    write_private(config, data, replace=True)
    assert service.call("task_list", {})["outcome"] == "denied"
