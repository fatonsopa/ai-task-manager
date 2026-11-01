"""Display labels are provider facts; canonical UUIDs remain authority."""

from dataclasses import asdict, replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.presentation.cli.render import task_lines
from ai_task_manager.storage.work import task_from
from tests.domains.helpers import Rig
from tests.linear.helpers import LinearRig, ProviderFixture, uid


def test_a34_actual_readback_caches_display_label_without_changing_uuid_routing(tmp_path):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        task = rig.task()
        assert task.display_identifier == provider.issues[task.issue_id]["identifier"]

        def actual_mutation_label(request, response):
            if "mutation UpdateIssue" in request["query"]:
                provider.issues[task.issue_id]["identifier"] = "TST-2000"
                response["issueUpdate"]["issue"]["identifier"] = "TST-2000"

        provider.hook = actual_mutation_label
        queued = rig.transition(task, "Todo", "enqueue")
        assert queued.issue_id == task.issue_id and queued.display_identifier == "TST-2000"
        updates = [
            request[0]
            for request in provider.requests
            if "mutation UpdateIssue" in request[0]["query"]
        ]
        assert updates[-1]["variables"]["id"] == task.issue_id
        rendered = task_lines([asdict(queued)], color=False)
        assert "TST-2000" in rendered and "local:" not in rendered
    finally:
        provider.close()


@pytest.mark.parametrize(
    "identifier", [None, "TST-", "OTHER-1", "TST-0", "TST-1\x1b[31m", "TST-" + "1" * 129]
)
def test_a34_malformed_provider_label_cannot_become_cached_display(tmp_path, identifier):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        task = rig.task()
        provider.edit(task.issue_id, identifier=identifier)
        with pytest.raises(LinearError):
            rig.client.read_issue(task.issue_id)
        assert rig.core.get_task(rig.handle, task.id).display_identifier == task.display_identifier
    finally:
        provider.close()


def test_a34_old_records_and_receipts_have_explicit_local_fallback(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    assert task.display_identifier is None
    old = asdict(task)
    del old["display_identifier"]
    decoded = task_from(old)
    assert decoded.display_identifier is None
    assert "local:" + task.id in task_lines([asdict(decoded)], color=False)
    dirty = asdict(replace(task, display_identifier="TST-1\x1b[31m"))
    assert "\x1b" not in task_lines([dirty], color=False)


@pytest.mark.parametrize("mismatch", ["label", "issue", "scope"])
def test_a34_wrong_receipt_cannot_update_cache_or_pending_intent(tmp_path, mismatch):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task() if mismatch == "issue" else rig.task(confirmed=False)
    if mismatch == "issue":
        task = rig.transition(task, "Todo", "enqueue", confirm=False)
    operation = [
        item
        for item in rig.core.sync.list_operations(rig.handle)
        if item.task_id == task.id and item.state != "confirmed"
    ][-1]
    data = {
        "operation_id": operation.id,
        "status": task.desired_status,
        "prose_digest": task.prose_digest,
        "title": task.title,
        "body": task.body,
        "representation": task.representation,
        "issue_id": task.issue_id or uid(987),
        "display_identifier": "TST-123",
    }
    if mismatch == "label":
        data["display_identifier"] = "TST-123\nforged"
    if mismatch == "issue":
        data["issue_id"] = uid(988)
    scope = replace(rig.scope, logical_session="foreign") if mismatch == "scope" else rig.scope
    ref = rig.proof("provider_status", task.id, data, scope=scope)
    with pytest.raises(EvidenceRequired):
        rig.core.intake.confirm_status(
            rig.handle, task.id, task.version, task.decision_revision, ref, rig.key()
        )
    assert rig.core.get_task(rig.handle, task.id) == task
