"""Native Type uses approved Skill metadata; HTTP and review input are authored."""

import copy
import os
import sys

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.adapters.writing.service import WritingService
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import reviewed_control
from tests.linear.helpers import EXPECTED, LinearRig, ProviderFixture, uid

BODY = """## Description

Record the approved task category in the existing tracker metadata.

## Expected outcome

The new issue keeps its exact approved prose and its declared category.

## Acceptance criteria

- The issue has the approved native category and no repeated type declaration.
- Existing unrelated labels remain after an authorized status update."""


class NativeTypeProvider(ProviderFixture):
    def __init__(self):
        super().__init__()
        self.group = dict(
            id=uid(700),
            name="Type",
            isGroup=True,
            groupType="singleSelect",
            archivedAt=None,
            team=None,
            parent=None,
        )
        self.leaf = dict(
            id=uid(701),
            name="Chore",
            isGroup=False,
            groupType=None,
            archivedAt=None,
            team=None,
            parent={k: self.group[k] for k in ("id", "name", "isGroup", "team")},
        )
        self.labels = [self.group, self.leaf]
        self.sentinel = [uid(702), uid(703), uid(704)]  # Explicit existing server labels.
        self.drop_type = False

    def graphql(self, query, variables):
        if "query NativeTypeLabels" in query:
            return {
                "viewer": {"id": EXPECTED.viewer_id},
                "organization": {"id": EXPECTED.workspace_id},
                "issueLabels": {
                    "nodes": copy.deepcopy(self.labels),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        if "query NativeTypeRead" in query:
            issue = copy.deepcopy(self.issues[variables["id"]])
            issue["labels"] = {
                "nodes": [
                    {
                        "id": value,
                        "parent": {"id": self.group["id"]} if value == self.leaf["id"] else None,
                    }
                    for value in issue["native_labels"]
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            return {
                "viewer": {"id": EXPECTED.viewer_id},
                "organization": {"id": EXPECTED.workspace_id},
                "issue": issue,
            }
        result = super().graphql(query, variables)
        if "mutation CreateIssue" in query:
            values = variables["input"]
            issue = self.issues[result["issueCreate"]["issue"]["id"]]
            issue["native_labels"] = self.sentinel + (
                [] if self.drop_type else values.get("labelIds", [])
            )
        return result


@pytest.fixture
def rig(tmp_path):
    provider = NativeTypeProvider()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def approved_task(rig, tmp_path):
    package = WriterPackage.capture(os.environ["TECHNICAL_WRITER_PACKAGE"], sys.executable)
    writer = WritingService(
        package,
        AuthorityJournal(tmp_path / "writer.sqlite", tmp_path / "writer.key"),
        tmp_path / "writing",
        rig.renderer,
    )
    rig.policy = package.policy()
    rig.core = rig.new_core()
    rig.core.context.approvals = writer.approvals_for_tasks(rig.scope)
    rig.outbound = OutboundDrain(rig.core, rig.client, rig.journal)
    rig.reconcile = Reconciliation(rig.core, rig.client, rig.journal)
    prose = TaskProse("Record the approved tracker category", BODY, False, ("verify",))
    prepared = writer.prepare(rig.scope, "issue_chore", prose)
    assert prepared["mechanical_passed"], prepared
    assert writer.review(rig.scope, prepared["writing_id"], reviewed_control(prepared["packet"]))[
        "accepted"
    ]
    approved = writer.finalize(rig.scope, prepared["writing_id"])
    record = writer.approvals_for_tasks(rig.scope).record(approved["approval_ref"])
    assert record["native_task_type"] == "Chore" and "Type:" not in record["prose"]["body"]
    task = rig.core.intake.submit(rig.handle, prose, approved["approval_ref"], rig.key())
    assert task.body == record["prose"]["body"]
    return task, writer


def test_actual_writer_kind_creates_native_type_and_updates_preserve_other_labels(rig, tmp_path):
    task, _ = approved_task(rig, tmp_path)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "confirmed", result
    current = rig.current(task)
    assert current.body == task.body and "Type:" not in current.body
    creates = [q for q, _ in rig.provider.requests if "mutation CreateIssue" in q["query"]]
    assert creates[0]["variables"]["input"]["labelIds"] == [rig.provider.leaf["id"]]
    assert creates[0]["variables"]["input"]["description"] == task.body
    expected = rig.provider.sentinel + [rig.provider.leaf["id"]]
    assert rig.provider.issues[current.issue_id]["native_labels"] == expected
    queued = rig.transition(current, "Todo", "enqueue")
    rig.client.update_issue(queued, "update_prose")
    assert rig.provider.issues[current.issue_id]["native_labels"] == expected
    assert all(
        "labelIds" not in q["variables"]["input"]
        for q, _ in rig.provider.requests
        if "mutation UpdateIssue" in q["query"]
    )


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "wrong_group", "foreign_scope"])
def test_native_type_metadata_refuses_before_create(rig, fault, tmp_path):
    task, _ = approved_task(rig, tmp_path)
    if fault == "missing":
        rig.provider.labels.remove(rig.provider.leaf)
    elif fault == "ambiguous":
        rig.provider.labels.append(dict(rig.provider.leaf, id=uid(705)))
    elif fault == "wrong_group":
        rig.provider.group["groupType"] = "multiSelect"
    else:
        rig.provider.leaf["team"] = {"id": uid(706)}
    before = len(rig.provider.requests)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed" and result["error"].startswith("native_type")
    assert not rig.journal.sends(rig.scope) and rig.current(task).issue_id is None
    assert not any(q["query"].startswith("mutation") for q, _ in rig.provider.requests[before:])


def test_native_type_uncertain_readback_recovers_original_binding_without_resend(rig, tmp_path):
    task, _ = approved_task(rig, tmp_path)
    rig.provider.drop_type = True
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain" and result["error"] == "native_type_readback_conflict"
    send = rig.journal.sends(rig.scope)[-1]
    binding = rig.journal.native_type_binding(rig.scope, send["id"])
    assert binding["label_id"] == rig.provider.leaf["id"]
    issue_id = next(iter(rig.provider.issues))
    original_leaf = rig.provider.leaf["id"]
    rig.provider.leaf["id"] = uid(707)
    with pytest.raises(LinearError, match="native_type_metadata_changed") as error:
        rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert (
        error.value.uncertain and rig.journal.native_type_binding(rig.scope, send["id"]) == binding
    )
    rig.provider.leaf["id"] = original_leaf
    rig.provider.issues[issue_id]["native_labels"].append(binding["label_id"])
    before = len(rig.provider.requests)
    recovered = rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert recovered.issue_id == issue_id and recovered.body == task.body
    assert not any(q["query"].startswith("mutation") for q, _ in rig.provider.requests[before:])
    assert len(rig.provider.issues) == 1


def test_legacy_approval_without_marker_keeps_exact_create_and_no_type_lookup(rig):
    prose = TaskProse(
        "Preserve legacy category prose",
        "Type: Chore\n\nRetain the original approved text.",
        False,
        ("verify",),
    )
    task = rig.core.intake.submit(rig.handle, prose, rig.approval(prose), rig.key())
    task = rig.confirm(task)
    create = next(q for q, _ in rig.provider.requests if "mutation CreateIssue" in q["query"])
    assert "labelIds" not in create["variables"]["input"]
    assert not any("NativeType" in q["query"] for q, _ in rig.provider.requests)
    assert rig.current(task).body == task.body
