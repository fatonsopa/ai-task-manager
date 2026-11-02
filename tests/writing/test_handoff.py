"""Actual Writer execution; injected faults and judgments are authored controls."""

import hashlib
import json
import os
import sqlite3
from dataclasses import replace

import pytest

from ai_task_manager.adapters.writing.handoff import FIELDS
from ai_task_manager.config.files import read_private
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    ApprovalRequired,
    EvidenceRequired,
)
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import FEATURE_BODY, ServiceRig, reviewed_control


@pytest.fixture
def env(tmp_path):
    source = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not source:
        pytest.skip("Configure the actual approved Writer package for integration checks")
    return ServiceRig(tmp_path, writer_root=source)


def prepare_review(env):
    prepared = env.writer.prepare(
        env.rig.scope, "issue_feature", TaskProse("Verify scoped task ownership", FEATURE_BODY)
    )
    assert prepared["mechanical_passed"]
    result = env.writer.review(
        env.rig.scope, prepared["writing_id"], reviewed_control(prepared["packet"])
    )
    assert result["accepted"]
    folder, _ = env.writer._session(prepared["writing_id"], env.rig.scope)
    return prepared, folder


def snapshot(env):
    return (
        env.rig.core.list_tasks(env.rig.handle),
        env.rig.core.sync.list_operations(env.rig.handle),
        env.rig.core.notifications.list_events(env.rig.handle),
        list(env.provider.calls),
    )


def approval_count(env):
    with sqlite3.connect(env.writer.journal.path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM records WHERE ref LIKE 'writing-task:%' "
            "OR ref LIKE 'writing-progress:%'"
        ).fetchone()[0]


class FaultAfterActualExecution:
    """Faults the return/file after genuine retained validator execution, never a fake pass."""

    def __init__(self, runner, callback):
        self.runner, self.callback, self.commands = runner, callback, []

    def run(self, folder, command, *arguments):
        self.commands.append(command)
        result = self.runner.run(folder, command, *arguments)
        return self.callback(folder, command, result)


def fault_finalize(env, callback):
    runner = FaultAfterActualExecution(
        env.writer.runner,
        lambda folder, command, result: (
            callback(folder, result) if command == "finalize" else result
        ),
    )
    env.writer.runner = runner
    return runner


def assert_refused_without_approval(env, prepared, before):
    result = env.service.call("writing_finalize", {"writing_id": prepared["writing_id"]})
    assert not result["ok"] and result["outcome"] == "denied", result
    assert approval_count(env) == 0
    assert snapshot(env) == before
    return result


def test_wh01_actual_exact_handoff_and_consumption_without_other_effects(env):
    prepared, folder = prepare_review(env)
    before = snapshot(env)
    result = env.service.call("writing_finalize", {"writing_id": prepared["writing_id"]})
    assert result["ok"], result
    data = result["data"]
    assert set(data) == {"approval_ref", "purpose", "kind", "policy", "digest", "handoff"}
    receipt = read_private(folder / "validator/delivery.json")
    expected = {name: ({"status": "delivered", **receipt})[name] for name in FIELDS}
    assert data["handoff"] == expected
    assert set(data["handoff"]["reviewer"]) == {"kind", "identity", "model", "method"}
    assert data["handoff"]["reviewer"]["kind"] == "current_host_self_review"
    assert data["handoff"]["reviewer"]["model"] is None
    assert data["handoff"]["message"].endswith("\n")
    assert data["handoff"]["message"].encode() == (folder / "validator/message.md").read_bytes()
    assert (
        data["handoff"]["message_sha256"]
        == hashlib.sha256(data["handoff"]["message"].encode()).hexdigest()
    )
    assert (
        data["handoff"]["output_sha256"]
        == hashlib.sha256((folder / "accepted.md").read_bytes()).hexdigest()
    )
    assert data["handoff"]["objective_sha256"] == prepared["packet"]["objective_sha256"]
    invocations = [read_private(p) for p in folder.glob("invocation-*.json")]
    checked = [row for row in invocations if row["command"] == "verify-message"]
    assert len(invocations) == 5 and len(checked) == 1
    assert checked[0]["outcome"] == "succeeded" and checked[0]["result"]["passed"] is True
    assert snapshot(env) == before
    assert "review" not in data["handoff"]
    assert env.writer.journal._key.hex() not in json.dumps(data)
    view = env.writer.approvals_for_tasks(env.rig.scope)
    assert view.verify_approval(data["approval_ref"]).digest == data["digest"]
    assert len(list(folder.glob("invocation-*.json"))) == 5  # no re-finalization on consumption


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_wh04_missing_current_field_refuses_after_real_delivery(env, field):
    prepared, folder = prepare_review(env)
    before = snapshot(env)

    def omit(_, result):
        result.pop(field)
        return result

    fault_finalize(env, omit)
    assert_refused_without_approval(env, prepared, before)
    assert (folder / "accepted.md").exists()
    assert read_private(folder / "validator/delivery.json")["message"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "accepted"),
        ("message", None),
        ("message", "Changed public text\n"),
        ("message", "\ud800"),
        ("message", "x" * 262145),
        ("message_sha256", "0" * 64),
        ("message_sha256", ["not-a-hash"]),
        ("destination", "/different-session/accepted.md"),
        ("output_sha256", "1" * 64),
        ("objective_sha256", "2" * 64),
        ("reviewer", {}),
        ("reviewer", {"kind": "human", "identity": "Claim", "model": None, "method": "Claim"}),
        ("document_status", "ready"),
        ("document_status", False),
    ],
)
def test_wh04_wh05_malformed_or_changed_handoff_refuses(env, field, value):
    prepared, _ = prepare_review(env)
    before = snapshot(env)

    def alter(_, result):
        result[field] = value
        return result

    fault_finalize(env, alter)
    assert_refused_without_approval(env, prepared, before)


@pytest.mark.parametrize("extra", ["private_review", "authority_key"])
def test_wh12_extra_reviewer_fields_cannot_leak_through_handoff(env, extra):
    prepared, _ = prepare_review(env)

    def alter(_, result):
        result["reviewer"][extra] = "synthetic private sentinel"
        return result

    fault_finalize(env, alter)
    result = assert_refused_without_approval(env, prepared, snapshot(env))
    assert "synthetic private sentinel" not in json.dumps(result)


@pytest.mark.parametrize("file", ["accepted.md", "validator/message.md"])
@pytest.mark.parametrize("operation", ["change", "remove", "symlink"])
def test_wh06_changed_missing_symlinked_bytes_refuse(env, file, operation):
    prepared, folder = prepare_review(env)
    before = snapshot(env)

    def alter(_, result):
        target = folder / file
        if operation == "change":
            target.write_bytes(target.read_bytes().rstrip(b"\n"))
        elif operation == "remove":
            target.unlink()
        else:
            copy = folder / "foreign-copy.md"
            copy.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(copy)
        return result

    fault_finalize(env, alter)
    assert_refused_without_approval(env, prepared, before)


@pytest.mark.parametrize(
    "response", [{}, {"passed": False}, {"passed": True, "message_sha256": "0" * 64}]
)
def test_wh07_actual_verify_message_must_return_matching_pass(env, response):
    prepared, folder = prepare_review(env)
    runner = FaultAfterActualExecution(
        env.writer.runner,
        lambda _, command, result: response if command == "verify-message" else result,
    )
    env.writer.runner = runner
    assert_refused_without_approval(env, prepared, snapshot(env))
    assert runner.commands == ["finalize", "verify-message"]
    actual = [read_private(p) for p in folder.glob("invocation-*.json")]
    assert any(
        row["command"] == "verify-message" and row["outcome"] == "succeeded" for row in actual
    )


def test_wh07_post_verify_race_is_rechecked_before_approval(env):
    prepared, folder = prepare_review(env)

    def alter(_, command, result):
        if command == "verify-message":
            (folder / "accepted.md").write_text("Changed after the actual check.\n")
        return result

    env.writer.runner = FaultAfterActualExecution(env.writer.runner, alter)
    assert_refused_without_approval(env, prepared, snapshot(env))


def test_wh07_real_verifier_failure_is_retained_without_approval(env):
    prepared, folder = prepare_review(env)
    actual_runner = env.writer.runner

    class ChangedBeforeActualVerifier:
        def run(self, workdir, command, *arguments):
            if command == "verify-message":
                with (folder / "validator/message.md").open("ab") as stream:
                    stream.write(b"Unreviewed extra claim.\n")
            return actual_runner.run(workdir, command, *arguments)

    env.writer.runner = ChangedBeforeActualVerifier()
    assert_refused_without_approval(env, prepared, snapshot(env))
    outcomes = [read_private(p) for p in folder.glob("invocation-*.json")]
    failed = [row for row in outcomes if row["command"] == "verify-message"]
    assert len(failed) == 1 and failed[0]["outcome"] == "failed"
    assert failed[0]["result"]["passed"] is False


def test_wh05_other_session_receipt_cannot_be_substituted(env):
    first, _ = prepare_review(env)
    second, _ = prepare_review(env)
    other = env.writer.finalize(env.rig.scope, second["writing_id"])
    original_count = approval_count(env)
    other_record = env.writer.journal._get(other["approval_ref"], "writing-task:")
    fault_finalize(env, lambda _, result: other_record["validator_delivery"])
    with pytest.raises(ApprovalRequired):
        env.writer.finalize(env.rig.scope, first["writing_id"])
    assert approval_count(env) == original_count


@pytest.mark.parametrize(
    "field", ["provider", "workspace", "principal", "project", "logical_session"]
)
def test_wh08_each_foreign_scope_field_refuses_before_finalization(env, field):
    prepared, folder = prepare_review(env)
    before = snapshot(env)
    foreign = replace(env.rig.scope, **{field: "foreign"})
    count = len(list(folder.glob("invocation-*.json")))
    with pytest.raises(ApprovalRequired):
        env.writer.finalize(foreign, prepared["writing_id"])
    assert approval_count(env) == 0 and snapshot(env) == before
    assert len(list(folder.glob("invocation-*.json"))) == count


def test_wh08_stale_package_refuses_finalize_before_delivery(env):
    prepared, folder = prepare_review(env)
    old = env.writer.package
    env.writer.package = replace(old, pins={**old.pins, "SKILL.md": "0" * 64})
    assert_refused_without_approval(env, prepared, snapshot(env))
    assert not (folder / "accepted.md").exists()
    assert len(list(folder.glob("invocation-*.json"))) == 3


@pytest.mark.parametrize(
    "missing", ["handoff", "delivery_seals", "validator_delivery", "writing_id"]
)
def test_wh09_signed_active_record_without_handoff_contract_cannot_be_consumed(env, missing):
    prepared, _ = prepare_review(env)
    receipt = env.writer.finalize(env.rig.scope, prepared["writing_id"])
    row = env.writer.journal._get(receipt["approval_ref"], "writing-task:")
    row.pop(missing)
    # An explicitly trusted malformed signed-store fixture, not a public issuer.
    ref = env.writer.journal._put("writing-task:", row)
    before = snapshot(env)
    response = env.service.call("task_submit", {"writing_ref": ref, "operation_key": missing})
    assert not response["ok"] and response["outcome"] == "denied"
    assert snapshot(env) == before


@pytest.mark.parametrize(
    "file",
    ["accepted.md", "validator/message.md", "validator/delivery.json", "validator/state.json"],
)
def test_wh10_changed_artifacts_refuse_approval_consumption_and_do_not_overwrite(env, file):
    prepared, folder = prepare_review(env)
    receipt = env.writer.finalize(env.rig.scope, prepared["writing_id"])
    target = folder / file
    changed = target.read_bytes() + b"\n"
    target.write_bytes(changed)
    before = snapshot(env)
    response = env.service.call(
        "task_submit", {"writing_ref": receipt["approval_ref"], "operation_key": "after-change"}
    )
    assert not response["ok"] and response["outcome"] == "denied", response
    assert snapshot(env) == before
    assert target.read_bytes() == changed
    assert len(list(folder.glob("invocation-*.json"))) == 5


def test_wh11_document_handoff_cannot_be_task_decision_or_notification_evidence(env):
    prepared, _ = prepare_review(env)
    receipt = env.writer.finalize(env.rig.scope, prepared["writing_id"])
    submitted = env.service.call(
        "task_submit", {"writing_ref": receipt["approval_ref"], "operation_key": "real-intake"}
    )
    assert submitted["ok"]
    event = env.rig.core.notifications.list_events(env.rig.handle)[-1]
    before = snapshot(env)
    for value in [receipt["approval_ref"], receipt["handoff"]["message"]]:
        with pytest.raises(AccessDenied):
            env.authority.verify_evidence(value)
        with pytest.raises(EvidenceRequired):
            env.rig.core.notifications.acknowledge(
                env.rig.handle, event.id, "ai_acknowledged", value, "not-an-ack-" + value[:20]
            )
    response = env.service.call(
        "task_submit", {"writing_ref": receipt["handoff"]["message"], "operation_key": "not-a-ref"}
    )
    # Prose is not a reference: the unchanged public schema rejects it first.
    assert not response["ok"] and response["outcome"] == "invalid_request"
    assert snapshot(env) == before
    assert set(env.rig.core.notifications.receipts(env.rig.handle, event.id)) == {"queued"}


@pytest.mark.parametrize("change", ["document", "message_removed", "package_pin"])
def test_wh13_committed_retry_stays_historical_but_new_effect_requires_current_approval(
    env, change
):
    prepared, folder = prepare_review(env)
    receipt = env.writer.finalize(env.rig.scope, prepared["writing_id"])
    request = {"writing_ref": receipt["approval_ref"], "operation_key": "committed-intake"}
    first = env.service.call("task_submit", request)
    assert first["ok"] and first["data"]["observed_status"] == "Backlog"
    before = snapshot(env)
    counts = (approval_count(env), len(env.rig.proofs.evidence))
    if change == "document":
        (folder / "accepted.md").write_bytes(b"Changed since committed intake.\n")
    elif change == "message_removed":
        (folder / "validator/message.md").unlink()
    else:
        old = env.writer.package
        env.writer.package = replace(old, pins={**old.pins, "SKILL.md": "0" * 64})
    # No approval is consumed and no effect is repeated by this same-key replay.
    replay = env.service.call("task_submit", request)
    assert replay["ok"] and replay["data"] == first["data"]
    fresh = env.service.call("task_submit", {**request, "operation_key": "new-intake"})
    assert not fresh["ok"] and fresh["outcome"] == "denied"
    assert snapshot(env) == before
    assert (approval_count(env), len(env.rig.proofs.evidence)) == counts
    assert len(env.provider.calls) == 1


def test_wh09_changed_signed_writing_record_refuses_without_effect(env):
    prepared, _ = prepare_review(env)
    receipt = env.writer.finalize(env.rig.scope, prepared["writing_id"])
    before = snapshot(env)
    with sqlite3.connect(env.writer.journal.path) as conn:
        conn.execute("UPDATE records SET data='{}' WHERE ref=?", (receipt["approval_ref"],))
    refused = env.service.call(
        "task_submit", {"writing_ref": receipt["approval_ref"], "operation_key": "invalid-mac"}
    )
    assert not refused["ok"] and refused["outcome"] == "denied"
    assert snapshot(env) == before
