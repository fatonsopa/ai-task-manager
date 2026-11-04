"""Actual Writer/HTTP boundaries with declared local provider and review controls.

The provider's terminal-LF behavior reproduces the retained TST-149 observation.
These checks do not execute live Linear, host models or historical fixture recovery.
"""

import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.domains.work.errors import ApprovalRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import RenderedProse, TaskProse
from tests.application.helpers import FEATURE_BODY, reviewed_control


class FrozenV1:
    """Old representation for this already list-canonical fixture only."""

    def render(self, prose):
        assert "\n- " not in prose.body
        return RenderedProse(prose.title, prose.body, "linear-markdown-v1")


def approve(service, body=FEATURE_BODY):
    prose = TaskProse("Verify scoped task ownership", body)
    prepared = service.writer.prepare(service.scope, "issue_feature", prose)
    assert prepared["mechanical_passed"]
    review = service.writer.review(
        service.scope, prepared["writing_id"], reviewed_control(prepared["packet"])
    )
    assert review["accepted"]
    approved = service.writer.finalize(service.scope, prepared["writing_id"])
    return approved["approval_ref"], prepared


def mutations(provider):
    return [r for r, _ in provider.requests if r["query"].startswith("mutation ")]


def strip_terminal_lf(provider):
    def observed_contract(request, response):
        if "mutation CreateIssue" in request["query"]:
            issue = response["issueCreate"]["issue"]
            issue["description"] = issue["description"].rstrip("\n")
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = observed_contract


def use_renderer(service, renderer):
    service.writer.renderer = renderer
    service.core.context.renderer = renderer


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_v2_normalizes_only_terminal_lf_and_is_idempotent(ending):
    body = "- prose `literal`\n\n```sh\n- literal --exact\n\n```\n    - code  \n\nTail  "
    renderer = LinearMarkdownRenderer()
    prose = TaskProse("Exact representation", body + ending)
    rendered = renderer.render(prose)
    assert rendered.representation == "linear-markdown-v4"
    assert rendered.body == body.replace("- prose", "* prose", 1)
    assert renderer.render(replace(prose, body=rendered.body)) == rendered
    assert rendered.title == prose.title


@pytest.mark.parametrize("ending", ["", "\n"])
def test_new_approval_reviews_exact_v2_bytes_and_confirms_create(composed, ending):
    service, _, provider, _ = composed
    strip_terminal_lf(provider)
    ref, prepared = approve(service, FEATURE_BODY.rstrip("\n") + ending)
    view = service.writer.approvals_for_tasks(service.scope)
    approved_prose = view.prose(ref)
    approval = view.verify_approval(ref)
    rendered = LinearMarkdownRenderer().render(approved_prose)
    assert not approved_prose.body.endswith("\n")
    assert approval.representation == rendered.representation == "linear-markdown-v4"
    assert approval.digest == prose_digest(approved_prose, rendered)
    folder, _ = service.writer._session(prepared["writing_id"], service.scope)
    expected = ("# " + approved_prose.title + "\n\n" + rendered.body).encode()
    assert (folder / "draft.md").read_bytes() == (folder / "accepted.md").read_bytes() == expected
    assert prepared["packet"]["output"].encode() == expected
    result = service.call("task_submit", {"writing_ref": ref, "operation_key": "v2-create"})
    assert result["ok"] and result["outcome"] == "confirmed", result
    task = result["data"]
    assert task["observed_status"] == task["desired_status"] == "Backlog"
    assert task["body"] == provider.issues[task["issue_id"]]["description"] == rendered.body
    assert len(mutations(provider)) == 1
    assert (
        service.call("task_submit", {"writing_ref": ref, "operation_key": "v2-create"})["data"][
            "id"
        ]
        == task["id"]
    )
    assert len(mutations(provider)) == 1


@pytest.mark.parametrize("change", ["literal", "internal_newline", "terminal_newline"])
def test_postapproval_provider_changes_stay_uncertain(composed, change):
    service, _, provider, _ = composed
    ref, _ = approve(service)

    def changed(request, response):
        if "mutation CreateIssue" in request["query"]:
            issue = response["issueCreate"]["issue"]
            body = issue["description"]
            issue["description"] = {
                "literal": body.replace("--scope=session", "--scope=foreign"),
                "internal_newline": body.replace("\n\n", "\n", 1),
                "terminal_newline": body + "\n",
            }[change]
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = changed
    result = service.call("task_submit", {"writing_ref": ref, "operation_key": "changed"})
    assert not result["ok"] and result["outcome"] == "uncertain", result
    task = service.core.list_tasks(service.handle)[0]
    assert task.issue_id is None and task.execution_state == "pending_intake"
    assert service.provider.journal.sends(service.scope)[0]["error"] == "readback_conflict"
    assert len(mutations(provider)) == 1


def test_unsubmitted_v1_approval_cannot_acquire_v2_meaning(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV1())
    ref, _ = approve(service)
    view = service.writer.approvals_for_tasks(service.scope)
    old = view.record(ref)
    use_renderer(service, LinearMarkdownRenderer())
    refused = service.call("task_submit", {"writing_ref": ref, "operation_key": "unused-v1"})
    assert not refused["ok"] and refused["outcome"] == "denied", refused
    assert view.record(ref) == old
    assert not service.core.list_tasks(service.handle) and not mutations(provider)


def test_v1_uncertain_create_is_retained_and_cannot_retry_or_recover_after_upgrade(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV1())
    strip_terminal_lf(provider)
    ref, _ = approve(service)
    request = {"writing_ref": ref, "operation_key": "historic-uncertain"}
    failed = service.call("task_submit", request)
    assert not failed["ok"] and failed["outcome"] == "uncertain", failed
    task = service.core.list_tasks(service.handle)[0]
    journal = service.provider.journal
    sends = journal.sends(service.scope)
    assert sends[0]["error"] == "readback_conflict"
    assert task.body == json.loads(sends[0]["snapshot"])["body"] + "\n"
    operations = service.core.sync.list_operations(service.handle)
    before = (task, sends, operations, list(mutations(provider)))
    use_renderer(service, LinearMarkdownRenderer())
    retry = service.call("task_submit", request)
    assert not retry["ok"] and retry["outcome"] == "denied", retry
    reconcile = Reconciliation(service.core, service.provider.client, journal)
    with pytest.raises(LinearError, match="readback_conflict"):
        reconcile.recover_outbound(service.handle, operations[0].id)
    assert service.provider.run_once(service.handle) is None
    assert before == (
        service.core.get_task(service.handle, task.id),
        journal.sends(service.scope),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )


def test_exact_confirmed_v1_replay_preserves_effect_and_refuses_changed_requests(composed):
    service, _, provider, components = composed
    use_renderer(service, FrozenV1())
    ref, _ = approve(service)
    request = {"writing_ref": ref, "operation_key": "confirmed-v1"}
    first = service.call("task_submit", request)
    assert first["ok"] and first["outcome"] == "confirmed", first
    before = (
        service.core.list_tasks(service.handle),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )
    view = service.writer.approvals_for_tasks(service.scope)
    signed = view.record(ref)
    prose = view.prose(ref)
    use_renderer(service, LinearMarkdownRenderer())
    replay = service.call("task_submit", request)
    assert replay["ok"] and replay["data"] == first["data"], replay
    assert view.record(ref) == signed
    with pytest.raises(ApprovalRequired):
        service.core.intake.submit(service.handle, prose, ref, "new-key")
    with pytest.raises(IdempotencyConflict):
        service.core.intake.submit(
            service.handle,
            replace(prose, body=prose.body + "Changed"),
            ref,
            request["operation_key"],
        )
    with pytest.raises(ApprovalRequired):
        service.writer.approvals_for_tasks(
            replace(service.scope, logical_session="foreign")
        ).verify_approval(ref)
    foreign = replace(service.scope, logical_session="foreign-replay")
    handle = service.core.register_session(
        components.journal.bind_principal(components.client.verify_binding(), foreign)
    )
    with pytest.raises(ApprovalRequired):
        service.core.intake.submit(handle, prose, ref, request["operation_key"])
    service.core.context.approval_policy = replace(
        service.core.context.approval_policy, style_hash="changed"
    )
    with pytest.raises(ApprovalRequired):
        service.core.intake.submit(service.handle, prose, ref, request["operation_key"])
    assert before == (
        service.core.list_tasks(service.handle),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )
