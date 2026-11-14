"""Missing installed DP01 queue/refusal controls; all HTTP and history are authored."""

import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.linear.client import CREATE, UPDATE
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.installation.workflows.bootstrap import activation_reference
from ai_task_manager.storage import work
from ai_task_manager.storage.database import Database
from tests.linear.helpers import CONNECTION, EXPECTED

from .test_dp01_preparation import installed as installed


@pytest.fixture
def submitted(installed):
    p = installed
    approved = p.approved()
    request = {"writing_ref": approved["approval_ref"], "operation_key": "enqueue-control-submit"}
    task = p.prepare("task_submit", request)["data"]
    assert task["observed_status"] == "Backlog" and not task["queued_once"]
    assert task["scheduling"]["state"] == "unknown"
    assert task["scheduling"]["graph_version"] == 0 and not task["work_authorized"]
    return p, task, request


def queue_request(task, key):
    return {
        "task_id": task["id"],
        "action": "enqueue",
        "expected_version": task["version"],
        "decision_revision": task["decision_revision"],
        "operation_key": key,
    }


def facts(p):
    """Compare durable authority/task facts, allowing retained append-only refusal audit."""
    config = read_private(p.config)
    with sqlite3.connect(config["core_database"]) as conn:
        core = {
            "tasks": conn.execute("SELECT id,data FROM tasks ORDER BY id").fetchall(),
            "capacity": conn.execute("SELECT * FROM capacity ORDER BY host").fetchall(),
            "sessions": conn.execute("SELECT * FROM sessions ORDER BY id").fetchall(),
            "outbox": conn.execute("SELECT * FROM outbox ORDER BY id").fetchall(),
            "attempts": conn.execute("SELECT * FROM attempts ORDER BY id").fetchall(),
            "resources": conn.execute(
                "SELECT * FROM resource_leases ORDER BY kind,resource_id"
            ).fetchall(),
        }
    with sqlite3.connect(config["authority_database"]) as conn:
        authority = conn.execute(
            "SELECT ref,data,mac FROM records WHERE "
            "ref NOT LIKE 'workflow-bootstrap:%' AND ref NOT LIKE 'decision-proof:%' ORDER BY ref"
        ).fetchall()
    return core, authority, p.config.read_bytes(), (p.config.parent / "session.json").read_bytes()


def effects(p):
    return tuple(
        sum(request["query"] == query for request, _ in p.provider.requests)
        for query in (CREATE, UPDATE)
    )


def refused(p, request, *, message=None):
    before, mutations = facts(p), effects(p)
    result = p.prepare("task_action", request, expected=None)
    assert not result["ok"]
    if message:
        assert message in result["error"]["message"]
    assert facts(p) == before
    assert effects(p) == mutations
    return result


def test_DP01E01_installed_enqueue_inspect_replay_and_changed_or_stale_inputs(submitted):
    p, backlog, submit = submitted
    enqueue = queue_request(backlog, "enqueue-once")
    queued = p.prepare("task_action", enqueue)["data"]
    assert queued["id"] == backlog["id"] and queued["issue_id"] == backlog["issue_id"]
    assert queued["observed_status"] == "Todo" and queued["sync_state"] == "confirmed"
    assert queued["queued_once"] and effects(p) == (1, 1)
    assert queued["scheduling"]["state"] == "unknown"
    assert queued["scheduling"]["graph_version"] == 0 and not queued["work_authorized"]
    assert not any(
        "DependencySchema" in request["query"] or "query Dependency" in request["query"]
        for request, _ in p.provider.requests
    )
    remote = p.provider.issues[queued["issue_id"]]
    assert remote["state"]["name"] == "Todo"
    assert remote["assignee"]["id"] == EXPECTED.viewer_id
    assert remote["delegate"]["id"] == CONNECTION.agent_id
    assert p.prepare("task_show", {"task_id": queued["id"]})["data"] == queued
    assert p.prepare("task_list", {})["data"] == [queued]
    assert p.prepare("task_submit", submit)["data"] == queued
    assert p.prepare("task_action", enqueue)["data"] == queued
    assert effects(p) == (1, 1)

    refused(
        p,
        {**queue_request(queued, "new-stale-version"), "expected_version": backlog["version"]},
        message="Task version has changed",
    )
    refused(
        p,
        queue_request(queued, "enqueue-once"),
        message="Operation key was reused with changed arguments",
    )
    refused(p, {**queue_request(queued, "foreign-task"), "task_id": str(uuid4())})

    # Seed a later authored revision; this is not an actual human observation.
    with Database(read_private(p.config)["core_database"]).transaction() as conn:
        current = work.get(conn, backlog["session_id"], backlog["id"])
        later = work.save(
            conn, replace(current, decision_revision=current.decision_revision + 1), current.version
        )
    refused(
        p,
        {
            **queue_request({**queued, "version": later.version}, "new-stale-revision"),
            "decision_revision": queued["decision_revision"],
        },
        message="A newer human decision takes precedence",
    )
    assert facts(p)[0]["attempts"] == [] and facts(p)[0]["resources"] == []
    assert effects(p) == (1, 1)


@pytest.mark.parametrize("state", ["pending", "leased", "uncertain"])
def test_DP01E02_installed_unsettled_outbox_stops_enqueue_without_retry(submitted, state):
    p, task, _ = submitted
    with Database(read_private(p.config)["core_database"]).transaction() as conn:
        conn.execute(
            "UPDATE outbox SET state=?,lease_owner=?,lease_until=?",
            (
                state,
                "authored-prior-holder" if state == "leased" else None,
                time.time() + 120 if state == "leased" else None,
            ),
        )
    refused(
        p,
        queue_request(task, "refused-unsettled-" + state),
        message="Unsettled effects require reconciliation",
    )
    assert effects(p) == (1, 0)


def test_DP01E03_installed_authored_execution_history_stops_enqueue(submitted):
    p, task, _ = submitted
    with Database(read_private(p.config)["core_database"]).transaction() as conn:
        conn.execute(
            "INSERT INTO attempts VALUES(?,?,?,'codex',0,'{}')",
            (str(uuid4()), task["id"], task["session_id"]),
        )
    refused(
        p,
        queue_request(task, "refused-after-history"),
        message="Execution history forbids initial preparation",
    )
    assert effects(p) == (1, 0)


def test_DP01E04_installed_signed_activation_latch_survives_removed_descriptor(submitted):
    p, task, _ = submitted
    config = read_private(p.config)
    control = config["workflow_control"]
    descriptor = p.config.parent / "owner.json"
    write_private(descriptor, {"authored_descriptor": True})
    descriptor.unlink()
    journal = AuthorityJournal(config["authority_database"], config["authority_key"])
    bind_record(
        journal, activation_reference(p.config, control), {"authored_prior_activation": True}
    )
    assert not descriptor.exists() and not (p.config.parent / "owner.sock").exists()
    refused(
        p,
        queue_request(task, "refused-after-activation"),
        message="Owner activation history permanently closes initial preparation",
    )
    assert effects(p) == (1, 0)
