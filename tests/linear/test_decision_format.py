"""Skill-owned decision format with actual Writer and authored provider effects."""

import copy
import os
from dataclasses import replace

import pytest

from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.application.linear_sync.progress import decision_prose
from ai_task_manager.application.service.models import validate
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import ApprovalRequired, ValidationError
from tests.application.helpers import ServiceRig, reviewed_control
from tests.linear.test_decision_publication import env as env
from tests.linear.test_decision_publication import fields


def test_new_decision_uses_real_pinned_writer_template_and_canonical_options(env, tmp_path):
    source = os.environ["TECHNICAL_WRITER_PACKAGE"]
    writer = ServiceRig(tmp_path / "actual-writer", writer_root=source).writer
    rig, task, publisher, _ = env
    writer.renderer = publisher.renderer
    supplied = fields()
    supplied["options"] = [dict(o, id=str(i + 10)) for i, o in enumerate(supplied["options"])]
    supplied["recommendation"] = "11"
    prepared = publisher.prepare_decision(rig.handle, task.id, supplied, writer)
    assert prepared["mechanical_passed"], prepared
    _, record = writer._session(prepared["writing_id"], rig.scope)
    metadata = record["decision"]
    assert [o["id"] for o in metadata["options"]] == ["A", "B"]
    assert metadata["recommendation"] == "B"
    assert metadata["proposal_rendering"] == writer.package.decision_template()
    assert metadata["proposal_rendering_sha256"] == digest(metadata["proposal_rendering"])
    assert record["prose"]["body"].startswith(
        "## Decision:\n\n" + supplied["title"] + "\n\n## Context\n\n"
    )
    reviewed = writer.review(
        rig.scope, prepared["writing_id"], reviewed_control(prepared["packet"])
    )
    assert reviewed["accepted"]
    approval = writer.finalize(rig.scope, prepared["writing_id"])
    publisher.approvals = writer.approvals_for_decisions(rig.scope, task.id, task.prose_digest)
    publisher.approval_policy = writer.package.policy()
    prose = publisher.approvals.prose(approval["approval_ref"])
    published = publisher.publish(
        rig.handle, task.id, prose, approval["approval_ref"], "skill-format"
    )
    assert published["state"] == published["label_state"] == "confirmed"
    body = rig.provider.comments[published["comment_id"]]["body"]
    assert body == record["prose"]["body"]
    assert "### Option A: Direct check" in body and "### Option B: Separate check" in body


def test_template_integrity_and_public_legacy_switch_refuse(tmp_path):
    package = WriterPackage.capture(os.environ["TECHNICAL_WRITER_PACKAGE"], "unused-python")
    template = package.decision_template()
    metadata = dict(
        fields(),
        recipient={"url": "https://linear.app/fixture/profiles/recipient"},
        proposal_rendering=template,
        proposal_rendering_sha256=digest(template),
    )
    invalid = copy.deepcopy(metadata)
    invalid["options"][1]["id"] = "X"
    with pytest.raises(ValidationError, match="consecutive"):
        decision_prose(invalid)
    with pytest.raises(ValidationError, match="plain-text"):
        decision_prose(dict(metadata, title="Title\nAnother paragraph"))
    changed = copy.deepcopy(metadata)
    changed["proposal_rendering"]["body"] += "changed"
    with pytest.raises(ApprovalRequired, match="differs"):
        decision_prose(changed)
    with pytest.raises(ApprovalRequired, match="changed"):
        replace(package, pins={**package.pins, "SKILL.md": "0" * 64}).decision_template()
    baseline = dict(fields(), task_id="00000000-0000-4000-8000-000000000001", operation_key="key")
    validate("task_decision_prepare", baseline)
    with pytest.raises(ValidationError):
        validate("task_decision_prepare", dict(baseline, proposal_rendering=template))
