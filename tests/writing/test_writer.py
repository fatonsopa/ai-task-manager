"""Execute the real pinned skill with synthetic review controls, not model claims."""

import os
from dataclasses import replace

import pytest

from ai_task_manager.adapters.writing.package import ISSUE_KINDS
from ai_task_manager.domains.work.errors import AccessDenied, ApprovalRequired
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import FEATURE_BODY, ServiceRig, reviewed_control


@pytest.fixture
def env(tmp_path):
    source = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not source:
        pytest.skip(
            "Actual external Technical Writer package must be configured for integration tests"
        )
    return ServiceRig(tmp_path, writer_root=source)


def approved(env, *, purpose="task", task=None, body=FEATURE_BODY):
    response = env.writer.prepare(
        env.rig.scope,
        "issue_feature",
        TaskProse("Verify scoped task ownership", body),
        purpose=purpose,
        task=task,
    )
    assert response["mechanical_passed"]
    reviewed = env.writer.review(
        env.rig.scope, response["writing_id"], reviewed_control(response["packet"])
    )
    assert reviewed["accepted"]
    return env.writer.finalize(env.rig.scope, response["writing_id"]), response


def test_a05_actual_skill_commands_approve_exact_content_then_intake(env):
    receipt, response = approved(env)
    assert set(ISSUE_KINDS) == {
        "issue_bug",
        "issue_chore",
        "issue_decision",
        "issue_feature",
        "issue_improvement",
        "issue_release",
    }
    assert response["packet"]["contract"]["task_type"] == "Feature"
    assert "references/issue.md" in response["packet"]["guidance"]
    assert "references/writing_style.md" in response["packet"]["guidance"]
    result = env.service.call(
        "task_submit",
        {"writing_ref": receipt["approval_ref"], "operation_key": "real-writer-intake"},
    )
    assert result["ok"] and result["data"]["observed_status"] == "Backlog"
    assert result["data"]["body"] == FEATURE_BODY
    assert "check --scope=session" in result["data"]["body"]
    assert len(list((env.root / "writing").rglob("invocation-*.json"))) == 4


def test_a06_progress_cannot_become_task_or_other_issue_approval(env):
    initial, _ = approved(env)
    submitted = env.service.call(
        "task_submit",
        {"writing_ref": initial["approval_ref"], "operation_key": "initial-progress-context"},
    )
    task = env.rig.core.get_task(env.rig.handle, submitted["data"]["id"])
    receipt, _ = approved(
        env,
        purpose="progress",
        task=task,
        body="The scoped check passed; no delivery evidence has been captured.",
    )
    with pytest.raises(AccessDenied):
        env.writer.approvals_for_tasks(env.rig.scope).verify_approval(receipt["approval_ref"])
    view = env.writer.approvals_for_progress(env.rig.scope, task.id, task.prose_digest)
    assert view.prose(receipt["approval_ref"]).required_work == ("progress",)
    with pytest.raises(ApprovalRequired):
        env.writer.approvals_for_progress(
            env.rig.scope, "another-task", task.prose_digest
        ).verify_approval(receipt["approval_ref"])
    with pytest.raises(ApprovalRequired):
        env.writer.approvals_for_progress(env.rig.scope, task.id, "changed-work").verify_approval(
            receipt["approval_ref"]
        )


def test_a07_fabricated_human_review_and_wrong_packet_fail(env):
    prepared = env.writer.prepare(
        env.rig.scope, "issue_feature", TaskProse("Verify scoped task ownership", FEATURE_BODY)
    )
    review = reviewed_control(prepared["packet"])
    review["reviewer"]["kind"] = "human"
    with pytest.raises(ApprovalRequired):
        env.writer.review(env.rig.scope, prepared["writing_id"], review)
    review = reviewed_control(prepared["packet"])
    review["output_sha256"] = "0" * 64
    with pytest.raises(ApprovalRequired):
        env.writer.review(env.rig.scope, prepared["writing_id"], review)


def test_writing_omissions_and_changed_draft_do_not_publish(env):
    prepared = env.writer.prepare(
        env.rig.scope, "issue_feature", TaskProse("Verify scoped task ownership", FEATURE_BODY)
    )
    with pytest.raises(AccessDenied):
        env.writer.finalize(env.rig.scope, prepared["writing_id"])
    env.writer.review(env.rig.scope, prepared["writing_id"], reviewed_control(prepared["packet"]))
    folder, _ = env.writer._session(prepared["writing_id"], env.rig.scope)
    (folder / "draft.md").write_text("# Changed unreviewed title\n\n" + FEATURE_BODY)
    with pytest.raises(ApprovalRequired):
        env.writer.finalize(env.rig.scope, prepared["writing_id"])
    assert not (folder / "accepted.md").exists()


def test_a06_cross_session_and_unknown_approval_refuse(env):
    receipt, prepared = approved(env)
    foreign = replace(env.rig.scope, logical_session="other")
    with pytest.raises(ApprovalRequired):
        env.writer.approvals_for_tasks(foreign).verify_approval(receipt["approval_ref"])
    with pytest.raises(ApprovalRequired):
        env.writer.review(foreign, prepared["writing_id"], reviewed_control(prepared["packet"]))
    assert not env.service.call(
        "task_submit", {"writing_ref": "writing-task:fake", "operation_key": "fake"}
    )["ok"]


def test_a09_changed_pins_block_new_writes_without_relabeling_old_receipts(env):
    receipt, _ = approved(env)
    old = env.writer.package
    env.writer.package = replace(old, pins={**old.pins, "SKILL.md": "0" * 64})
    with pytest.raises(ApprovalRequired):
        env.writer.approvals_for_tasks(env.rig.scope).verify_approval(receipt["approval_ref"])
    env.writer.package = old
    assert env.writer.approvals_for_tasks(env.rig.scope).verify_approval(receipt["approval_ref"])


@pytest.mark.parametrize(
    "field", ["objective_sha256", "output_sha256", "scores", "requirements", "style_review"]
)
def test_writing_omitted_review_bindings_do_not_finalize(env, field):
    prepared = env.writer.prepare(
        env.rig.scope, "issue_feature", TaskProse("Verify scoped task ownership", FEATURE_BODY)
    )
    control = reviewed_control(prepared["packet"])
    del control[field]
    try:
        result = env.writer.review(env.rig.scope, prepared["writing_id"], control)
        assert result["accepted"] is False
    except ApprovalRequired:
        pass
    with pytest.raises((AccessDenied, ApprovalRequired)):
        env.writer.finalize(env.rig.scope, prepared["writing_id"])


@pytest.mark.parametrize("change", ["title", "body", "code_bearing", "required_work"])
def test_exact_approved_metadata_cannot_be_reused_for_different_work(env, change):
    receipt, _ = approved(env)
    view = env.writer.approvals_for_tasks(env.rig.scope)
    prose = view.prose(receipt["approval_ref"])
    values = {
        "title": "Another work item",
        "body": prose.body + "Changed scope.\n",
        "code_bearing": False,
        "required_work": ("different-unit",),
    }
    with pytest.raises(ApprovalRequired):
        env.rig.core.intake.submit(
            env.rig.handle,
            replace(prose, **{change: values[change]}),
            receipt["approval_ref"],
            "changed-" + change,
        )
