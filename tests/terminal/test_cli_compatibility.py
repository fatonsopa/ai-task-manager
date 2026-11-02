"""Legacy CLI routes remain intact beside additive read-only view commands."""

import json
from dataclasses import replace

from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import ServiceRig
from tests.application.test_configuration_cli import run_cli

from .test_pty import fixture_task


def test_legacy_task_list_json_remains_scoped(tmp_path):
    fixture = ServiceRig(tmp_path / "product")
    own = fixture_task(fixture)
    foreign = fixture.rig.session(replace(fixture.rig.scope, logical_session="foreign"))
    prose = TaskProse("Inspect isolated task", "Retain the scoped result.", False)
    hidden = fixture.rig.core.intake.submit(
        foreign, prose, fixture.rig.approval(prose), fixture.rig.key()
    )
    config = fixture.configuration()
    response = run_cli(config, "--json", "task", "list")
    assert response.returncode == 0, response.stderr
    payload = json.loads(response.stdout)
    assert payload["ok"] and [row["id"] for row in payload["data"]] == [own.id]
    assert hidden.id not in response.stdout and "capability" not in response.stdout
    assert "Session tasks" not in response.stdout


def test_legacy_task_show_foreign_and_duplicate_input_refuse(tmp_path):
    fixture = ServiceRig(tmp_path / "product")
    own = fixture_task(fixture)
    foreign = fixture.rig.session(replace(fixture.rig.scope, logical_session="foreign"))
    prose = TaskProse("Inspect isolated task", "Retain the scoped result.", False)
    hidden = fixture.rig.core.intake.submit(
        foreign, prose, fixture.rig.approval(prose), fixture.rig.key()
    )
    before = fixture.rig.core.get_task(fixture.rig.handle, own.id)
    config = fixture.configuration()
    response = run_cli(
        config,
        "--json",
        "task",
        "show",
        "--input",
        "-",
        input_data=json.dumps({"task_id": hidden.id}),
    )
    assert response.returncode == 3
    assert json.loads(response.stderr)["outcome"] == "denied"
    assert hidden.title not in response.stderr and not response.stdout
    invalid = run_cli(
        config,
        "--json",
        "task",
        "show",
        "--input",
        "-",
        input_data='{"task_id":"a","task_id":"b"}',
    )
    assert invalid.returncode == 2 and not invalid.stdout
    assert fixture.rig.core.get_task(fixture.rig.handle, own.id) == before
    assert fixture.rig.core.get_task(foreign, hidden.id) == hidden


def test_legacy_task_list_no_color_remains_plain(tmp_path):
    fixture = ServiceRig(tmp_path / "product")
    own = fixture_task(fixture)
    config = fixture.configuration()
    response = run_cli(config, "--no-color", "task", "list")
    assert response.returncode == 0, response.stderr
    assert "Backlog" in response.stdout and own.title in response.stdout
    assert "\x1b" not in response.stdout and "Session tasks" not in response.stdout
