from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition
from ai_task_manager.storage import issue_claims, work
from ai_task_manager.storage.database import Database
from tests.domains.helpers import Rig


def test_ea41_remote_delivery_then_newer_todo_cannot_restore_work_lease(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task, attempt = rig.running()
    before = rig.db.connect()
    try:
        resources = [tuple(row) for row in before.execute("SELECT * FROM resource_leases")]
    finally:
        before.close()
    for status, revision in [("Done", "2026-11-01T00:02:00Z"), ("Todo", "2026-11-01T00:03:00Z")]:
        current = rig.current(task)
        ref = rig.proof(
            "external_observation",
            task.id,
            {
                "issue_id": task.issue_id,
                "status": status,
                "source_revision": revision,
                "title": task.title,
                "body": task.body,
                "assigned_to_owner": True,
                "actor_kind": "unknown",
                "actor_authorized": False,
            },
        )
        rig.core.external.observe(
            rig.handle, task.id, current.version, current.decision_revision, ref, rig.key()
        )
    assert rig.current(task).delivered_once
    with pytest.raises((EvidenceRequired, InvalidTransition)):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
    with rig.db.transaction() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM resource_leases")] == resources
        with pytest.raises(EvidenceRequired):
            issue_claims.require_open(conn, rig.scope, task.issue_id)


def test_ea53_schema_two_backfills_delivery_without_losing_pending_effects(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    delivered, pending = rig.task(), rig.task(confirmed=False)
    with rig.db.transaction() as conn:
        delivered = work.save(conn, replace(delivered, delivered_once=True), delivered.version)
        original_tasks = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        original_outbox = [tuple(row) for row in conn.execute("SELECT * FROM outbox ORDER BY id")]
        for table in (
            "store_identity",
            "issue_lifecycle",
            "existing_admissions",
            "existing_checks",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE metadata SET version=2")
    migrated = Database(rig.db.path)
    with migrated.transaction() as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 3
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")
        ] == original_tasks
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM outbox ORDER BY id")
        ] == original_outbox
        assert migrated.identity(conn)
        with pytest.raises(EvidenceRequired):
            issue_claims.require_open(conn, rig.scope, delivered.issue_id)
    assert rig.current(pending).execution_state == "pending_intake"


def test_ea53_ea69_migration_retains_positive_terminal_observation_after_reset(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    observation = rig.proof(
        "external_observation",
        task.id,
        {
            "issue_id": task.issue_id,
            "source_revision": "2026-11-01T00:02:00Z",
            "status": "Canceled",
            "title": task.title,
            "body": task.body,
        },
    )
    rig.core.external.observe(
        rig.handle, task.id, task.version, task.decision_revision, observation, rig.key()
    )
    with rig.db.transaction() as conn:
        for table in (
            "store_identity",
            "issue_lifecycle",
            "existing_admissions",
            "existing_checks",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE metadata SET version=2")
    migrated = Database(rig.db.path)
    with migrated.transaction() as conn:
        with pytest.raises(EvidenceRequired):
            issue_claims.require_open(conn, rig.scope, task.issue_id)
    assert rig.current(task).observed_status == "Backlog"
