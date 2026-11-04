"""Heading representation from retained evidence; authored HTTP/review, no live acceptance."""

import json
import re
from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.links import explicit_https_links, literal_blocks
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
from tests.application.test_linear_prose import approve, mutations, use_renderer

BODY = (
    FEATURE_BODY.replace("## Description\n\n", "## Description\n")
    .replace("## Expected outcome\n\n", "## Expected outcome\n")
    .replace("## Acceptance criteria", "## Scope\nfixture-heading-source\n\n## Acceptance criteria")
)


def observed_separators(body):
    """Only the three observed heading/paragraph boundaries, independently spelled."""
    return (
        body.replace("## Description\nSeparate", "## Description\n\nSeparate")
        .replace("## Expected outcome\nOnly", "## Expected outcome\n\nOnly")
        .replace("## Scope\nfixture-heading-source", "## Scope\n\nfixture-heading-source")
    )


def provider_heading_contract(provider):
    def observed(request, response):
        if "mutation CreateIssue" in request["query"]:
            issue = response["issueCreate"]["issue"]
            issue["description"] = observed_separators(issue["description"])
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = observed


class FrozenV3:
    """Versioned preapproval links, list encoding and terminal document LF."""

    representation = "linear-markdown-v3"
    historical_representations = frozenset({"linear-markdown-v1", "linear-markdown-v2"})

    def render(self, prose):
        blocks, fence_starts = literal_blocks(prose.body)
        # Apply the existing list rules on the original offsets first. A list
        # marker introducing a fence belongs to its container, not its code.
        lines, offset = [], 0
        regions = iter(blocks.items())
        region = next(regions, None)
        for line in prose.body.split("\n"):
            end = offset + len(line) + 1
            while region is not None and region[1] <= offset:
                region = next(regions, None)
            literal = region is not None and region[0] < end
            if not literal or offset in fence_starts:
                line = re.sub(r"^( {0,3})- ", r"\1* ", line)
                if (
                    re.match(r"^ {0,3}\* ", line)
                    and len(lines) >= 2
                    and lines[-1] == ""
                    and re.match(r"^ {0,3}\* ", lines[-2])
                ):
                    lines.pop()
            lines.append(line)
            offset = end
        # List normalization changes offsets, so protect the final source map
        # used by the link scanner rather than reusing stale positions.
        body = explicit_https_links("\n".join(lines))
        # Linear drops terminal document newlines. Normalize only those LF bytes
        # before approval; internal lines, spaces and literal contents stay exact.
        return RenderedProse(prose.title, body.rstrip("\n"), self.representation)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("# Heading\nParagraph", "# Heading\n\nParagraph"),
        ("###### Heading ###\nText", "###### Heading ###\n\nText"),
        ("   ## Heading  \n Text  ", "   ## Heading  \n\n Text  "),
        ("Heading\n=======\nParagraph", "Heading\n=======\n\nParagraph"),
        ("Heading\n-------\nParagraph", "Heading\n-------\n\nParagraph"),
        ("First line\nsecond line\n===\nText", "First line\nsecond line\n===\n\nText"),
        ("# One\n## Two\nText", "# One\n\n## Two\n\nText"),
        ("# Heading\n\nText", "# Heading\n\nText"),
        ("# Heading\n \t\nText", "# Heading\n \t\nText"),
        ("# Heading", "# Heading"),
        ("Title\n===", "Title\n==="),
        ("## Heading\r\nText", "## Heading\r\n\r\nText"),
        ("Title\r---\rText", "Title\r---\r\rText"),
        ("# One\r\nText\n\n# Two\nNext", "# One\r\n\r\nText\n\n# Two\n\nNext"),
        ("## `literal # hash`\nText", "## `literal # hash`\n\nText"),
        ("# Heading\n```text\n# literal\ntext\n```", "# Heading\n\n```text\n# literal\ntext\n```"),
    ],
)
def test_hs02_heading_separators_preserve_source_and_are_idempotent(source, expected):
    renderer = LinearMarkdownRenderer()
    actual = renderer.render(TaskProse("Heading contract", source))
    assert actual == RenderedProse("Heading contract", expected, "linear-markdown-v4")
    assert renderer.render(TaskProse(actual.title, actual.body)) == actual


@pytest.mark.parametrize(
    "literal",
    [
        "```text\n# Heading\ntext\n```",
        "~~~text\nHeading\n---\ntext\n~~~",
        "> ~~~text\n> # Heading\n> text\n> ~~~",
        "1. ~~~text\n   # Heading\n   text\n   ~~~",
        "> 1. ```text\n>    Heading\n>    ---\n>    text\n>    ```",
        "    # Heading\n    text",
        "\tHeading\n\t---\n\ttext",
        "<pre>\n# Heading\ntext\n</pre>",
        "`# Heading`\ntext",
        '"# Heading"\ntext',
        "\\# Heading\ntext",
        "#not-heading\ntext",
        "####### not-heading\ntext",
        "text # not-heading\ntext",
        "> ## Heading\n>\n> text",
        "1. ## Heading\n\n   text",
        "> Heading\n> ---\n>\n> text",
        "> # Heading",
        "## Heading\n\n    # code\n    text",
    ],
)
def test_hs03_literals_nonheadings_and_separated_containers_stay_exact(literal):
    rendered = LinearMarkdownRenderer().render(TaskProse("Literal boundaries", literal))
    assert rendered.body == literal


@pytest.mark.parametrize(
    "source",
    [
        "> # Heading\n> text",
        "> Heading\n> ---\n> text",
        "1. # Heading\n   text",
        "* Heading\n  ---\n  text",
        "> * # Heading\n>   text",
        "> # Heading\nOutside quote",
        "1. # Heading\n2. next item",
    ],
)
def test_hs03_unseparated_container_headings_refuse_without_rewriting(source):
    with pytest.raises(ValidationError, match="container headings"):
        LinearMarkdownRenderer().render(TaskProse("Container boundary", source))


def test_hs01_hs04_writer_approves_exact_heading_bytes_before_http(composed):
    service, _, provider, _ = composed
    provider_heading_contract(provider)
    assert FrozenV3().render(TaskProse("Original", BODY)).body != observed_separators(BODY).rstrip(
        "\n"
    )
    ref, prepared = approve(service, BODY)
    view = service.writer.approvals_for_tasks(service.scope)
    prose, approval = view.prose(ref), view.verify_approval(ref)
    expected = observed_separators(BODY).rstrip("\n")
    assert prose.body == expected
    rendered = LinearMarkdownRenderer().render(prose)
    assert approval.representation == "linear-markdown-v4"
    assert approval.digest == prose_digest(prose, rendered)
    folder, _ = service.writer._session(prepared["writing_id"], service.scope)
    exact = ("# " + prose.title + "\n\n" + expected).encode()
    assert (folder / "draft.md").read_bytes() == (folder / "accepted.md").read_bytes() == exact
    assert prepared["packet"]["output"].encode() == exact
    request = {"writing_ref": ref, "operation_key": "heading-create"}
    first = service.call("task_submit", request)
    assert first["ok"] and first["outcome"] == "confirmed", first
    assert mutations(provider)[0]["variables"]["input"]["description"] == expected
    assert (
        first["data"]["body"]
        == provider.issues[first["data"]["issue_id"]]["description"]
        == expected
    )
    assert service.call("task_submit", request)["data"] == first["data"]
    assert len(mutations(provider)) == 1


def test_hs03_container_refusal_precedes_writing_and_provider_effects(composed):
    service, _, provider, _ = composed
    before = list(service.writer.root.iterdir()), list(provider.requests)
    with pytest.raises(ValidationError, match="container headings"):
        service.writer.prepare(
            service.scope, "issue_feature", TaskProse("Boundary", BODY + "\n> # H\n> p")
        )
    assert before == (list(service.writer.root.iterdir()), provider.requests)
    assert not service.core.list_tasks(service.handle)


@pytest.mark.parametrize("change", ["separator", "wording", "literal"])
def test_hs05_changed_provider_bytes_remain_uncertain(composed, change):
    service, _, provider, _ = composed
    ref, _ = approve(service, BODY)

    def changed(request, response):
        if "mutation CreateIssue" in request["query"]:
            issue = response["issueCreate"]["issue"]
            body = issue["description"]
            issue["description"] = {
                "separator": body.replace("## Description\n\n", "## Description\n"),
                "wording": body.replace("Separate sessions", "Shared sessions"),
                "literal": body.replace("--scope=session", "--scope=foreign"),
            }[change]
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = changed
    result = service.call("task_submit", {"writing_ref": ref, "operation_key": "changed-heading"})
    assert not result["ok"] and result["outcome"] == "uncertain", result
    assert service.provider.journal.sends(service.scope)[0]["error"] == "readback_conflict"
    assert len(mutations(provider)) == 1


def test_hs05_v3_confirmed_replay_keeps_old_bytes_even_if_current_rendering_refuses(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV3())
    ref, _ = approve(service, FEATURE_BODY + "\n> # Heading\n> exact quoted text")
    request = {"writing_ref": ref, "operation_key": "confirmed-v3"}
    first = service.call("task_submit", request)
    assert first["ok"] and first["outcome"] == "confirmed", first
    view = service.writer.approvals_for_tasks(service.scope)
    prose, signed = view.prose(ref), view.record(ref)
    before = list(provider.requests), service.core.list_tasks(service.handle)
    use_renderer(service, LinearMarkdownRenderer())
    with pytest.raises(ValidationError):
        service.core.context.renderer.render(prose)
    assert service.call("task_submit", request)["data"] == first["data"]
    assert before == (provider.requests, service.core.list_tasks(service.handle))
    assert view.record(ref) == signed
    with pytest.raises((ApprovalRequired, ValidationError)):
        service.core.intake.submit(service.handle, prose, ref, "new-key")
    with pytest.raises(IdempotencyConflict):
        service.core.intake.submit(
            service.handle,
            replace(prose, body=prose.body + "Changed"),
            ref,
            request["operation_key"],
        )


def test_hs05_v3_unused_approval_cannot_acquire_v4_meaning(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV3())
    ref, _ = approve(service, BODY)
    view = service.writer.approvals_for_tasks(service.scope)
    original = view.record(ref)
    use_renderer(service, LinearMarkdownRenderer())
    result = service.call("task_submit", {"writing_ref": ref, "operation_key": "unused-v3"})
    assert not result["ok"] and result["outcome"] == "denied", result
    assert view.record(ref) == original and not mutations(provider)
    assert not service.core.list_tasks(service.handle)


def test_hs01_hs05_v3_uncertain_heading_mismatch_stays_uncertain_after_upgrade(composed):
    service, _, provider, _ = composed
    use_renderer(service, FrozenV3())
    provider_heading_contract(provider)
    ref, _ = approve(service, BODY)
    request = {"writing_ref": ref, "operation_key": "uncertain-v3"}
    failed = service.call("task_submit", request)
    assert not failed["ok"] and failed["outcome"] == "uncertain", failed
    task = service.core.list_tasks(service.handle)[0]
    journal = service.provider.journal
    assert json.loads(journal.sends(service.scope)[0]["snapshot"])["body"] == observed_separators(
        task.body
    )
    before = (
        task,
        journal.sends(service.scope),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )
    use_renderer(service, LinearMarkdownRenderer())
    retry = service.call("task_submit", request)
    assert not retry["ok"] and retry["outcome"] == "denied", retry
    with pytest.raises(LinearError, match="readback_conflict"):
        Reconciliation(service.core, service.provider.client, journal).recover_outbound(
            service.handle, before[2][0].id
        )
    assert service.provider.run_once(service.handle) is None
    assert before == (
        service.core.get_task(service.handle, task.id),
        journal.sends(service.scope),
        service.core.sync.list_operations(service.handle),
        mutations(provider),
    )


def test_hs06_progress_approval_also_binds_prepared_heading_bytes(composed):
    from tests.application.test_linear_composition import approve as public_approve

    service, _, provider, _ = composed
    task_ref, _ = approve(service)
    submitted = service.call(
        "task_submit", {"writing_ref": task_ref, "operation_key": "progress-task"}
    )
    assert submitted["ok"]
    task = service.core.get_task(service.handle, submitted["data"]["id"])
    progress = public_approve(
        service,
        "Progress",
        "## Verified\nThe local check passed; delivery remains pending.",
        purpose="progress",
        task_id=task.id,
    )
    view = service.writer.approvals_for_progress(service.scope, task.id, task.prose_digest)
    assert (
        view.prose(progress).body
        == "## Verified\n\nThe local check passed; delivery remains pending."
    )
    assert view.verify_approval(progress).representation == "linear-markdown-v4"
    request = {"task_id": task.id, "writing_ref": progress, "operation_key": "heading-progress"}
    result = service.call("task_progress", request)
    assert result["ok"] and result["data"]["state"] == "confirmed", result
    assert len(provider.comments) == 1
    assert next(iter(provider.comments.values()))["body"] == view.prose(progress).body
    assert service.call("task_progress", request)["data"] == result["data"]
    assert len(provider.comments) == 1
