"""Captured link spelling plus authored provider/review controls; no live acceptance."""

import json
import re
from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.domains.work.errors import (
    ApprovalRequired,
    IdempotencyConflict,
    ValidationError,
)
from ai_task_manager.domains.work.models import RenderedProse, TaskProse
from tests.application.helpers import FEATURE_BODY
from tests.application.test_linear_prose import FrozenV1, approve, mutations, use_renderer

URL = "https://linear.app/developers/webhooks"
LINK = f"[{URL}](<{URL}>)"
BODY = FEATURE_BODY + f"\nRead {URL}.\n"


class FrozenV2:
    """Exact pre-link renderer from the assigned 1f5fca3 baseline, test-only."""

    def render(self, prose):
        lines, fence = [], None
        for line in prose.body.split("\n"):
            match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if match:
                marker = match[1][0]
                if fence is None:
                    fence = (marker, len(match[1]))
                elif marker == fence[0] and len(match[1]) >= fence[1]:
                    fence = None
                lines.append(line)
                continue
            if fence is None:
                line = re.sub(r"^( {0,3})- ", r"\1* ", line)
                if (
                    re.match(r"^ {0,3}\* ", line)
                    and len(lines) >= 2
                    and lines[-1] == ""
                    and re.match(r"^ {0,3}\* ", lines[-2])
                ):
                    lines.pop()
            lines.append(line)
        return RenderedProse(prose.title, "\n".join(lines).rstrip("\n"), "linear-markdown-v2")


@pytest.mark.parametrize(
    "body, expected",
    [
        (f"Read {URL}.", f"Read {LINK}."),
        (f"Read {URL}, then {URL}!", f"Read {LINK}, then {LINK}!"),
        (f"({URL});", f"({LINK});"),
        (f'"{URL}"', f'"{LINK}"'),
        (f"{URL}: {URL}?", f"{LINK}: {LINK}?"),
        (
            "https://example.test/a(b(c)).",
            "[https://example.test/a(b(c))](<https://example.test/a(b(c))>).",
        ),
        (
            "(https://example.test/a(b))",
            "([https://example.test/a(b)](<https://example.test/a(b)>))",
        ),
        (
            "https://example.test/?utm_source=task&a=(one)&next=%2Fdocs#section",
            "[https://example.test/?utm_source=task&a=(one)&next=%2Fdocs#section]"
            "(<https://example.test/?utm_source=task&a=(one)&next=%2Fdocs#section>)",
        ),
        (f"  Read  {URL}.  \n\nNext\t{URL}", f"  Read  {LINK}.  \n\nNext\t{LINK}"),
        (f"<{URL}>", LINK),
        (
            f"[Read {URL}](<https://example.test/destination>)",
            f"[Read {URL}](<https://example.test/destination>)",
        ),
        (f'[Guide]({URL} "Title (exact)")', f'[Guide]({URL} "Title (exact)")'),
        (f"![Image]({URL})", f"![Image]({URL})"),
        (f"[Reference][guide]\n\n[guide]: {URL}", f"[Reference][guide]\n\n[guide]: {URL}"),
        (f"[guide]:\n  <{URL}>", f"[guide]:\n  <{URL}>"),
        (f"[multiline\nlabel](<{URL}>)", f"[multiline\nlabel](<{URL}>)"),
    ],
)
def test_lr01_lr02_lr03_supported_links_are_exact_and_idempotent(body, expected):
    renderer = LinearMarkdownRenderer()
    prose = TaskProse(f"Title {URL}", body)
    actual = renderer.render(prose)
    assert actual == RenderedProse(prose.title, expected, "linear-markdown-v3")
    assert renderer.render(replace(prose, body=actual.body)) == actual


@pytest.mark.parametrize(
    "literal",
    [
        f"`{URL}`",
        f"``literal `{URL}``` inside``",
        f"`line one\n{URL}\nline three`",
        f"```sh\n{URL}\n```",
        f"~~~~text\n{URL}\n~~~~",
        f"````text\n```\n{URL}\n````",
        f"    {URL}",
        f"\t{URL}",
        f"```text\n{URL}",
    ],
)
def test_lr04_literal_urls_remain_exact(literal):
    body = literal + f"\n\nRead {URL}." if not literal.startswith("```text\n") else literal
    expected = literal + f"\n\nRead {LINK}." if body != literal else literal
    assert LinearMarkdownRenderer().render(TaskProse("Literal source", body)).body == expected


@pytest.mark.parametrize(
    "body",
    [
        "https://example.test/a(b",
        "https://example.test/a)b",
        "https://example.test/a[b",
        "https://example.test/a\\b",
        "https://example.test/_literal_",
        "https://example.test/*x*",
        "https://example.test/%broken",
        "https://user:password@example.test/path",
        "prefixhttps://example.test",
        "\\https://example.test",
        "`https://example.test",
        "[label](https://example.test",
        "[https://example.test]",
        "<https://example.test",
        '<a href="https://example.test">link</a>',
        "https://example.test/o'reilly",
    ],
)
def test_lr05_ambiguous_input_refuses(body):
    with pytest.raises(ValidationError, match="explicit Markdown link or code"):
        LinearMarkdownRenderer().render(TaskProse("Ambiguous input", body))


def normalize_observed_link(provider):
    """Independent narrow oracle: only the sentence from this fixture is converted."""

    def response(request, data):
        if "mutation CreateIssue" in request["query"]:
            issue = data["issueCreate"]["issue"]
            issue["description"] = issue["description"].replace(f"Read {URL}.", f"Read {LINK}.")
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = response


def test_lr06_writer_approves_the_exact_link_bytes_sent_and_confirmed(composed):
    service, _, provider, _ = composed
    normalize_observed_link(provider)
    ref, prepared = approve(service, BODY)
    view = service.writer.approvals_for_tasks(service.scope)
    prose, approval = view.prose(ref), view.verify_approval(ref)
    assert f"Read {LINK}." in prose.body and not prose.body.endswith("\n")
    rendered = LinearMarkdownRenderer().render(prose)
    assert approval.representation == "linear-markdown-v3"
    assert approval.digest == prose_digest(prose, rendered)
    folder, _ = service.writer._session(prepared["writing_id"], service.scope)
    exact = ("# " + prose.title + "\n\n" + prose.body).encode()
    assert (folder / "draft.md").read_bytes() == (folder / "accepted.md").read_bytes() == exact
    assert prepared["packet"]["output"].encode() == exact
    request = {"writing_ref": ref, "operation_key": "exact-v3-link"}
    first = service.call("task_submit", request)
    assert first["ok"] and first["outcome"] == "confirmed", first
    task = first["data"]
    assert task["body"] == provider.issues[task["issue_id"]]["description"] == prose.body
    assert len(mutations(provider)) == 1
    assert service.call("task_submit", request)["data"] == task
    assert len(mutations(provider)) == 1


def test_lr05_prepare_refuses_ambiguous_links_before_writing_or_provider_effects(composed):
    service, _, provider, _ = composed
    before = list(service.writer.root.iterdir()), list(provider.requests)
    with pytest.raises(ValidationError):
        service.writer.prepare(
            service.scope,
            "issue_feature",
            TaskProse("Ambiguous input", BODY + "\nhttps://example.test/a[b"),
        )
    assert before == (list(service.writer.root.iterdir()), provider.requests)
    assert not service.core.list_tasks(service.handle)


@pytest.mark.parametrize("change", ["bare", "destination", "label"])
def test_lr07_postapproval_link_changes_remain_uncertain(composed, change):
    service, _, provider, _ = composed
    ref, _ = approve(service, BODY)

    def changed(request, data):
        if "mutation CreateIssue" in request["query"]:
            issue = data["issueCreate"]["issue"]
            replacement = {
                "bare": URL,
                "destination": f"[{URL}](<https://foreign.test>)",
                "label": f"[Different label](<{URL}>)",
            }[change]
            issue["description"] = issue["description"].replace(LINK, replacement)
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = changed
    response = service.call("task_submit", {"writing_ref": ref, "operation_key": "changed-link"})
    assert not response["ok"] and response["outcome"] == "uncertain", response
    task = service.core.list_tasks(service.handle)[0]
    assert task.issue_id is None and task.execution_state == "pending_intake"
    assert service.provider.journal.sends(service.scope)[0]["error"] == "readback_conflict"
    assert len(mutations(provider)) == 1


@pytest.mark.parametrize("renderer", [FrozenV1(), FrozenV2()])
def test_lr08_confirmed_legacy_replay_does_not_apply_new_syntax_or_repeat_effect(
    composed, renderer
):
    service, _, provider, components = composed
    use_renderer(service, renderer)
    ref, _ = approve(service, FEATURE_BODY.rstrip("\n") + "\n\nReference: https://example.test/a[b")
    request = {"writing_ref": ref, "operation_key": "historical-confirmed"}
    first = service.call("task_submit", request)
    assert first["ok"] and first["outcome"] == "confirmed", first
    view = service.writer.approvals_for_tasks(service.scope)
    prose, signed = view.prose(ref), view.record(ref)
    before = (
        service.core.list_tasks(service.handle),
        service.core.sync.list_operations(service.handle),
        list(provider.requests),
    )
    use_renderer(service, LinearMarkdownRenderer())
    with pytest.raises(ValidationError):
        service.core.context.renderer.render(prose)
    for operation in (service.core.intake.recover_submit, service.core.intake.submit):
        assert (
            operation(service.handle, prose, ref, request["operation_key"]).id
            == first["data"]["id"]
        )
    assert service.call("task_submit", request)["data"] == first["data"]
    assert view.record(ref) == signed
    with pytest.raises((ApprovalRequired, ValidationError)):
        service.core.intake.submit(service.handle, prose, ref, "different-key")
    with pytest.raises(IdempotencyConflict):
        service.core.intake.submit(
            service.handle,
            replace(prose, body=prose.body + "Changed"),
            ref,
            request["operation_key"],
        )
    assert provider.requests == before[2]
    foreign = replace(service.scope, logical_session="foreign-replay")
    handle = service.core.register_session(
        components.journal.bind_principal(components.client.verify_binding(), foreign)
    )
    provider_before = list(provider.requests)
    with pytest.raises(ApprovalRequired):
        service.core.intake.recover_submit(handle, prose, ref, request["operation_key"])
    service.core.context.approval_policy = replace(
        service.core.context.approval_policy, style_hash="changed"
    )
    with pytest.raises((ApprovalRequired, ValidationError)):
        service.core.intake.recover_submit(service.handle, prose, ref, request["operation_key"])
    assert before[:2] == (
        service.core.list_tasks(service.handle),
        service.core.sync.list_operations(service.handle),
    )
    assert provider.requests == provider_before
    assert len(mutations(provider)) == 1


@pytest.mark.parametrize("renderer", [FrozenV1(), FrozenV2()])
def test_lr09_legacy_unsubmitted_approval_cannot_authorize_v3(composed, renderer):
    service, _, provider, _ = composed
    use_renderer(service, renderer)
    ref, _ = approve(service, BODY)
    view = service.writer.approvals_for_tasks(service.scope)
    original = view.record(ref)
    use_renderer(service, LinearMarkdownRenderer())
    response = service.call("task_submit", {"writing_ref": ref, "operation_key": "unsubmitted-old"})
    assert not response["ok"] and response["outcome"] == "denied", response
    assert view.record(ref) == original
    assert not service.core.list_tasks(service.handle) and not mutations(provider)


def test_lr09_v2_uncertainty_is_not_reinterpreted_or_retried(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV2())
    normalize_observed_link(provider)
    ref, _ = approve(service, BODY)
    request = {"writing_ref": ref, "operation_key": "uncertain-v2-link"}
    failed = service.call("task_submit", request)
    assert not failed["ok"] and failed["outcome"] == "uncertain", failed
    task = service.core.list_tasks(service.handle)[0]
    journal = service.provider.journal
    sends = journal.sends(service.scope)
    assert sends[0]["error"] == "readback_conflict"
    assert task.body != json.loads(sends[0]["snapshot"])["body"]
    operations = service.core.sync.list_operations(service.handle)
    before = (task, sends, operations, list(mutations(provider)))
    use_renderer(service, LinearMarkdownRenderer())
    retry = service.call("task_submit", request)
    assert not retry["ok"] and retry["outcome"] == "denied", retry
    with pytest.raises(LinearError, match="readback_conflict"):
        Reconciliation(service.core, service.provider.client, journal).recover_outbound(
            service.handle, operations[0].id
        )
    assert service.provider.run_once(service.handle) is None
    assert before == (
        service.core.get_task(service.handle, task.id),
        journal.sends(service.scope),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )


def test_lr07_unknown_historical_representation_cannot_gain_replay_authority(composed):
    class Unknown:
        def render(self, prose):
            return replace(FrozenV2().render(prose), representation="unknown-representation")

    service, _, provider, _ = composed
    use_renderer(service, Unknown())
    ref, _ = approve(service, BODY)
    request = {"writing_ref": ref, "operation_key": "unknown-history"}
    first = service.call("task_submit", request)
    assert first["ok"], first
    prose = service.writer.approvals_for_tasks(service.scope).prose(ref)
    before = list(provider.requests)
    use_renderer(service, LinearMarkdownRenderer())
    with pytest.raises(ApprovalRequired):
        service.core.intake.recover_submit(service.handle, prose, ref, request["operation_key"])
    assert provider.requests == before and len(mutations(provider)) == 1
