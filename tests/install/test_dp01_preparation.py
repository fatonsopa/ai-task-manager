"""Installed preparation against authored HTTP and semantic controls, never live acceptance."""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.client import CREATE, UPDATE
from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.config.files import read_private, write_private
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.linear.helpers import CONNECTION, EXPECTED
from tests.linear.live.adapter_fixture import AdapterProvider
from tests.linear.live.fixture_admin import FixtureAdministration
from tests.linear.live.fixture_graphql import FixtureGraphQL
from tests.linear.live.ledger import DriverRefused, Limits, RunLedger, Scope

OBSERVER = Path(__file__).with_name("cli_observer.py").resolve()


def snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.stat().st_ino,
            p.stat().st_mode,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def installed():
    selected = os.environ.get("DP01_INSTALLED_PYTHON")
    writer = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not selected or not writer:
        pytest.skip("Explicit installed candidate Python and frozen Writer package required")
    python = Path(selected).absolute()  # Do not resolve away its venv context.
    provider = AdapterProvider()
    temporary = tempfile.TemporaryDirectory(prefix="dp01-prep-", dir="/tmp")
    folder = Path(temporary.name).resolve()
    folder.chmod(0o700)
    project = folder / "empty-project"
    project.mkdir(mode=0o700)
    now = time.time()
    ledger = RunLedger(
        folder / "ledger.sqlite",
        str(uuid4()),
        Scope(
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.team_id,
            EXPECTED.project_id,
            str(uuid4()),
        ),
        Limits(
            now + 900,
            requests=600,
            projects=1,
            issues=1,
            webhooks=0,
            comments=0,
            started_at=now,
            cleanup_requests=100,
            cleanup_seconds=60,
        ),
    )
    graphql = FixtureGraphQL(
        ledger,
        HttpTransport(timeout=2, _test_endpoint=provider.endpoint),
        lambda: Credentials("AUTHORED_ADMIN_NOT_LIVE"),
    )
    admin = FixtureAdministration(graphql, quiescence=lambda: None)
    graphql.preflight()
    admin.create_project("owned-project")
    state = folder / "state"
    config = state / "service.json"
    package = WriterPackage.capture(writer, str(python))
    manifest = {
        "version": 1,
        "state_directory": str(state),
        "logical_session": CONNECTION.logical_session,
        "linear": {
            "expected": asdict(EXPECTED),
            "agent_connection": asdict(CONNECTION),
            "credential": {"environment": "AITM_CREDENTIAL_DP01_CONTROL"},
        },
        "writer": {
            "root": str(package.root),
            "python": str(python),
            "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
            "pins": package.pins,
        },
        "capacity_policies": [{"host": "codex", "host_limit": 1, "project_limit": 1}],
        "actions": ["enqueue"],
        "deadline": now + 800,
        "operation_key": "authored-installed-setup",
    }
    env = {
        k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ
    }
    env["AITM_CREDENTIAL_DP01_CONTROL"] = "AUTHORED_SECRET_MUST_NOT_APPEAR_IN_OBSERVER"
    commands = []

    def cli(arguments, *, expected=0, config_path=config):
        index = len(commands) + 1
        receipt = folder / f"receipt-{index}.json"
        command = [
            str(python),
            "-I",
            "-B",
            str(OBSERVER),
            "--ledger",
            str(ledger.path),
            "--receipt",
            str(receipt),
            "--authored-endpoint",
            provider.endpoint,
            "--",
            "--config",
            str(config_path),
            *arguments,
        ]
        child = subprocess.run(
            command, cwd=project, env=env, capture_output=True, text=True, timeout=45
        )
        raw = child.stdout if child.stdout.strip() else child.stderr
        row = {
            "command": command,
            "code": child.returncode,
            "stdout": child.stdout,
            "stderr": child.stderr,
            "receipt": read_private(receipt) if receipt.exists() else None,
        }
        commands.append(row)
        assert child.returncode == expected if expected is not None else child.returncode != 0, row
        result = json.loads(raw)
        assert row["receipt"] and row["receipt"]["mode"] == "authored_loopback_control"
        assert "AUTHORED_SECRET_MUST_NOT_APPEAR_IN_OBSERVER" not in json.dumps(row["receipt"])
        assert not list(project.iterdir())
        return result

    def prepare(operation, data, **kwargs):
        request = folder / f"request-{len(commands) + 1}.json"
        write_private(request, {"operation": operation, "data": data})
        return cli(["workflow", "prepare", "--input", str(request)], **kwargs)

    def approved(*, title="Preserve scoped fixture task ownership", body=None):
        body = body or FEATURE_BODY.replace(
            "Separate sessions need", "Fixture run: " + ledger.run_id + "\n\nSeparate sessions need"
        )
        prepared = prepare(
            "writing_prepare",
            {
                "kind": "issue_feature",
                "purpose": "task",
                "title": title,
                "body": body,
                "code_bearing": False,
                "required_work": ["verify"],
            },
        )["data"]
        assert prepared["mechanical_passed"]
        # Declared authored semantic control; actual human/model review is lead-owned.
        prepare(
            "writing_review",
            {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
        )
        return prepare("writing_finalize", {"writing_id": prepared["writing_id"]})["data"]

    request = folder / "setup.json"
    write_private(request, manifest)
    try:
        cli(["session", "setup", "--input", str(request)])
        yield SimpleNamespace(
            config=config,
            folder=folder,
            project=project,
            provider=provider,
            ledger=ledger,
            admin=admin,
            graphql=graphql,
            commands=commands,
            cli=cli,
            prepare=prepare,
            approved=approved,
            manifest=manifest,
        )
    finally:
        evidence = os.environ.get("DP01_PREPARATION_EVIDENCE")
        saved = None
        try:
            if evidence:
                target = Path(evidence)
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                saved = target / (ledger.run_id + ".json")
                write_private(
                    saved,
                    {
                        "scope": (
                            "Authored installed HTTP/semantic controls; "
                            "no live provider or model acceptance"
                        ),
                        "commands": commands,
                        "ledger": ledger.summary(),
                        "provider_requests": len(provider.requests),
                        "provider_creates": sum(r["query"] == CREATE for r, _ in provider.requests),
                        "provider_updates": sum(r["query"] == UPDATE for r, _ in provider.requests),
                        "project_empty": not list(project.iterdir()),
                        "owner_started": False,
                    },
                )
        finally:
            provider.close()
            temporary.cleanup()
            assert not provider.thread.is_alive()
            assert not folder.exists()
        if saved is not None:
            row = read_private(saved)
            write_private(
                saved,
                {**row, "provider_thread_stopped": True, "temporary_state_removed": True},
                replace=True,
            )


def test_DP01I01_installed_exact_approval_backlog_replay_and_observed_cleanup(installed):
    p = installed
    writing = p.approved()
    request = {"writing_ref": writing["approval_ref"], "operation_key": "installed-submit"}
    first = p.prepare("task_submit", request)["data"]
    assert first["observed_status"] == "Backlog" and first["sync_state"] == "confirmed"
    before = sum(r["query"] == CREATE for r, _ in p.provider.requests)
    replay = p.prepare("task_submit", request)["data"]
    assert (
        replay == first and sum(r["query"] == CREATE for r, _ in p.provider.requests) == before == 1
    )
    assert p.prepare("task_show", {"task_id": first["id"]})["data"] == first
    assert p.prepare("task_list", {})["data"] == [first]
    state_before = snapshot(p.config.parent)
    owned = p.admin.observe_product_issue(
        p.config, "installed-submit", first["id"], writing["approval_ref"]
    )
    assert owned["id"] == first["issue_id"] and snapshot(p.config.parent) == state_before
    again = p.admin.observe_product_issue(
        p.config, "installed-submit", first["id"], writing["approval_ref"]
    )
    assert again == owned and snapshot(p.config.parent) == state_before
    ledger = p.ledger.summary()
    assert len(ledger["product_observations"]) == 1
    assert ledger["product_observations"][0]["origin"] == "confirmed_product_post_effect"
    assert not [r for r in ledger["operations"] if r["kind"] == "issue"]
    assert not [r for r in ledger["requests"] if r["purpose"] == "create"][1:]
    with sqlite3.connect(read_private(p.config)["core_database"]) as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM resource_leases").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM outbox WHERE kind='set_status'").fetchone()[0] == 0
        )
    p.admin.cleanup("archive-product-issue", first["issue_id"], "archive")
    assert p.ledger.resource(first["issue_id"])["state"] == "archived"
    assert p.provider.issues[first["issue_id"]]["archivedAt"]
    p.admin.cleanup("archive-product-issue", first["issue_id"], "archive")
    assert sum(r["query"] == CREATE for r, _ in p.provider.requests) == 1
    assert sum(r["query"] == UPDATE for r, _ in p.provider.requests) == 0
    summary = p.ledger.summary()
    dispatches = sum(len(row["receipt"]["dispatches"]) for row in p.commands)
    assert sum(row["purpose"] == "product_transport" for row in summary["requests"]) == dispatches
    assert len(summary["requests"]) == len(p.provider.requests)


def test_DP01I02_missing_altered_scope_status_and_changed_payload_refuse(installed):
    p = installed
    p.prepare(
        "task_submit",
        {"writing_ref": "writing-task:missing", "operation_key": "missing"},
        expected=None,
    )
    writing = p.approved()
    p.prepare(
        "task_submit",
        {"writing_ref": writing["approval_ref"], "operation_key": "direct-todo", "status": "Todo"},
        expected=None,
    )
    destination = Path(writing["handoff"]["destination"])
    original = destination.read_bytes()
    destination.write_bytes(b"AUTHORED_ALTERED_APPROVED_OUTPUT")
    p.prepare(
        "task_submit",
        {"writing_ref": writing["approval_ref"], "operation_key": "altered"},
        expected=None,
    )
    destination.write_bytes(original)
    viewer = p.provider.viewer_id
    p.provider.viewer_id = str(uuid4())
    p.prepare(
        "task_submit",
        {"writing_ref": writing["approval_ref"], "operation_key": "foreign-scope"},
        expected=None,
    )
    p.provider.viewer_id = viewer
    assert not p.provider.issues
    first = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "same-key"}
    )["data"]
    other = p.approved(title="Keep another scoped fixture request exact")
    p.prepare(
        "task_submit",
        {"writing_ref": other["approval_ref"], "operation_key": "same-key"},
        expected=None,
    )
    assert len(p.provider.issues) == 1
    with pytest.raises(DriverRefused):
        p.admin.observe_product_issue(
            p.config, "different-operation", first["id"], writing["approval_ref"]
        )
    assert "product_observations" not in p.ledger.summary()


def test_DP01I03_uncertain_create_is_not_adopted_or_blindly_replayed(installed):
    p = installed
    writing = p.approved()

    def corrupt_reply(query, variables, result):
        if query == CREATE:
            result["issueCreate"]["issue"]["id"] = "AUTHORED_UNCONFIRMED_ID"
        return result

    p.provider.transform = corrupt_reply
    request = {"writing_ref": writing["approval_ref"], "operation_key": "uncertain-submit"}
    p.prepare("task_submit", request, expected=None)
    assert len(p.provider.issues) == 1
    with sqlite3.connect(read_private(p.config)["core_database"]) as conn:
        row = conn.execute("SELECT id,data FROM tasks").fetchone()
        task = json.loads(row[1])
        assert task["issue_id"] is None and task["sync_state"] == "pending"
        outbound = conn.execute("SELECT id,state FROM outbox").fetchone()
        assert outbound[1] == "uncertain"
        original_task_id = row[0]
        original_outbox_id = outbound[0]
    with pytest.raises(DriverRefused):
        p.admin.observe_product_issue(p.config, "uncertain-submit", row[0], writing["approval_ref"])
    p.prepare("task_submit", request, expected=None)
    assert sum(r["query"] == CREATE for r, _ in p.provider.requests) == 1
    with sqlite3.connect(read_private(p.config)["core_database"]) as conn:
        assert conn.execute("SELECT id FROM tasks").fetchall() == [(original_task_id,)]
        assert conn.execute("SELECT id,state FROM outbox").fetchall() == [
            (original_outbox_id, "uncertain")
        ]
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert "product_observations" not in p.ledger.summary()


@pytest.mark.parametrize(
    "change",
    [
        "marker",
        "title",
        "foreign_project",
        "foreign_creator",
        "foreign_assignee",
        "different_task",
        "different_writing",
        "uncertain_intent",
        "copied_store",
        "expired_during_readback",
    ],
)
def test_DP01I04_observation_refuses_foreign_changed_or_unresolved_evidence(
    installed, monkeypatch, change
):
    import shutil

    from ai_task_manager.storage.database import Database

    p = installed
    writing = p.approved()
    task = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "observe-submit"}
    )["data"]
    task_id, reference = task["id"], writing["approval_ref"]
    issue = p.provider.issues[task["issue_id"]]
    if change == "marker":
        issue["description"] = issue["description"].replace("Fixture run:", "Foreign run:")
    elif change == "title":
        issue["title"] = "Changed authored fixture title"
    elif change == "foreign_project":
        issue["project"] = {"id": str(uuid4())}
    elif change == "foreign_creator":
        issue["creator"] = {"id": str(uuid4())}
    elif change == "foreign_assignee":
        issue["assignee"] = {"id": str(uuid4())}
    elif change == "different_task":
        task_id = str(uuid4())
    elif change == "different_writing":
        reference = "writing-task:" + str(uuid4())
    elif change == "uncertain_intent":
        with Database(read_private(p.config)["core_database"]).transaction() as conn:
            conn.execute("UPDATE outbox SET state='uncertain'")
    elif change == "copied_store":
        target = p.config.parent / "copied.sqlite"
        source = Path(read_private(p.config)["core_database"])
        shutil.copyfile(source, target)
        target.chmod(0o600)
        os.replace(target, source)
    else:
        current = [time.time()]
        monkeypatch.setattr(time, "time", lambda: current[0])

        def expire(query, variables, result):
            if "query FixtureIssueMarker" in query:
                current[0] = p.manifest["deadline"] + 1
            return result

        p.provider.transform = expire
    before = snapshot(p.config.parent)
    effects = sum(r["query"] == CREATE for r, _ in p.provider.requests)
    with pytest.raises(DriverRefused):
        p.admin.observe_product_issue(p.config, "observe-submit", task_id, reference)
    assert snapshot(p.config.parent) == before
    assert "product_observations" not in p.ledger.summary()
    assert sum(r["query"] == CREATE for r, _ in p.provider.requests) == effects == 1
