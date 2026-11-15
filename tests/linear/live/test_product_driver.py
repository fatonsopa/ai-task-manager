"""Actual Writer/product services over authored HTTP, not live host behavior."""

import hashlib
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.authority import DecisionAuthority, HostGrant
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.client import (
    AGENT_USERS,
    CREATE,
    NATIVE_TYPE_LABELS,
    NATIVE_TYPE_READ,
)
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.adapters.writing.service import WritingService
from ai_task_manager.application.core import Core
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.config.claim_store import enroll_claim_store, verify_claim_store
from ai_task_manager.config.files import FileSeal, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import FEATURE_BODY, Observations, reviewed_control
from tests.linear.helpers import FixtureRouter, LinearRig

from .ledger import DriverRefused
from .primary_driver import FixtureProgressFactory, OwnedProductTransport, PrimaryDriver
from .test_actual_adapter import adapter as adapter
from .test_actual_adapter import ready


@pytest.fixture
def product(adapter, tmp_path):
    admin, graphql, ledger, provider, endpoint, candidate, now = ready(adapter)
    folder = tmp_path / "product"
    folder.mkdir(mode=0o700)
    # Existing authored fixture bootstrap is separate from the new adapter run.
    # All subsequent product requests use the counted actual HTTP transport.
    linear = LinearRig(folder, provider)
    transport = OwnedProductTransport(
        ledger, HttpTransport(timeout=2, _test_endpoint=provider.endpoint)
    )
    linear.client.transport = transport
    linear.client.create_capability, linear.client.comment_capability = (
        graphql.product_capabilities()
    )
    package_path = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    assert package_path and Path(package_path).is_dir(), "Pinned current Writer is required"
    writer = WritingService(
        WriterPackage.capture(package_path, sys.executable),
        AuthorityJournal(folder / "writing.sqlite", folder / "writing.key"),
        folder / "writing",
        linear.renderer,
    )
    policy = writer.package.policy()
    core = Core(
        linear.db,
        policy,
        (linear.capacity,),
        principals=linear.journal,
        approvals=writer.approvals_for_tasks(),
        evidence=FixtureRouter(linear.journal, linear.proofs),
        renderer=linear.renderer,
        clock=lambda: linear.now,
    )
    authority = AuthorityJournal(folder / "authority.sqlite", folder / "authority.key")
    observations = Observations()
    # This explicit authored grant is not native/account acceptance. The actual
    # file seal and enrolled Core store remain active in this local composition.
    observations.grants["fixture-enrollment"] = HostGrant(
        linear.scope,
        linear.scope.logical_session,
        ("enqueue", "dequeue", "start"),
        "authored-fixture-enrollment",
        ledger.limits.deadline,
    )
    decisions = DecisionAuthority(authority, observations)
    Path(linear.db.path).chmod(0o600)
    enroll_claim_store(linear.db, authority)
    claim_store = verify_claim_store(linear.db, authority)
    config = folder / "fixture-composition.json"
    write_private(
        config,
        {
            "scope": asdict(linear.scope),
            "core_database": str(linear.db.path),
            "approval_policy": asdict(policy),
        },
    )
    outbound = OutboundDrain(core, linear.client, linear.journal)

    publisher = FixtureProgressFactory(core, linear.client, linear.journal)
    service = ApplicationService(
        core,
        linear.handle,
        writer=writer,
        decisions=decisions,
        grant_ref="fixture-enrollment",
        provider=outbound,
        publisher=publisher,
        seal=FileSeal(config),
        claim_store=claim_store,
    )
    driver = PrimaryDriver(service, admin, transport, client=linear.client)
    return SimpleNamespace(
        driver=driver,
        writer=writer,
        ledger=ledger,
        provider=provider,
        core=core,
        service=service,
        client=linear.client,
        transport=transport,
        admin=admin,
        observations=observations,
        config=config,
    )


def approved(product, *, purpose="task", task=None, other_scope=None):
    body = FEATURE_BODY.replace(
        "Separate sessions need",
        "Fixture run: " + product.ledger.run_id + "\n\nSeparate sessions need",
    )
    if purpose == "progress":
        body = (
            "Fixture run: "
            + product.ledger.run_id
            + "\n\nThe fixture task is in Backlog. No worker has started.\n"
        )
    prose = TaskProse("Isolate fixture task ownership", body, False, ("verify",))
    if other_scope is None:
        data = {
            "kind": "issue_feature",
            "purpose": purpose,
            "title": prose.title,
            "body": prose.body,
            "code_bearing": False,
            "required_work": ["verify"],
        }
        if task is not None:
            data["task_id"] = task.id
        prepared = product.driver.writing("writing_prepare", data)
        assert prepared["ok"] and prepared["data"]["mechanical_passed"]
        row = prepared["data"]
        review = product.driver.writing(
            "writing_review",
            {"writing_id": row["writing_id"], "review": reviewed_control(row["packet"])},
        )
        assert review["ok"]
        finalized = product.driver.writing("writing_finalize", {"writing_id": row["writing_id"]})
        assert finalized["ok"], finalized
        return finalized["data"]
    row = product.writer.prepare(other_scope, "issue_feature", prose)
    product.writer.review(other_scope, row["writing_id"], reviewed_control(row["packet"]))
    return product.writer.finalize(other_scope, row["writing_id"])


def submit(product):
    writing = approved(product)
    result = product.driver.invoke(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "fixture-intake"}
    )
    assert result["ok"] and result["outcome"] == "confirmed", result
    task = product.core.get_task(product.service.handle, result["data"]["id"])
    return writing, result, task


def test_a29_a31_a33_actual_writer_service_http_intake_and_exact_handoff(product):
    writing, result, task = submit(product)
    handoff = writing["handoff"]
    assert hashlib.sha256(handoff["message"].encode()).hexdigest() == handoff["message_sha256"]
    assert (
        hashlib.sha256(Path(handoff["destination"]).read_bytes()).hexdigest()
        == handoff["output_sha256"]
    )
    assert task.observed_status == "Backlog" and task.issue_id
    row = product.ledger.get("product:issue:" + task.issue_id)
    assert row["state"] == "confirmed" and row["candidate"] == task.issue_id
    request = next(
        request for request, _ in product.provider.requests if request["query"] == CREATE
    )
    assert request["variables"]["input"]["id"] == task.issue_id
    assert request["variables"]["input"]["description"] == task.body
    assert result["data"]["id"] == task.id
    with product.core.context.db.transaction() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("wrong", ["missing", "other_scope", "altered"])
def test_a30_bad_current_writing_reference_cannot_create_an_issue(product, wrong):
    if wrong == "missing":
        reference = "writing-task:missing"
    else:
        scope = (
            replace(product.service.scope, logical_session="authored-other-session")
            if wrong == "other_scope"
            else None
        )
        writing = approved(product, other_scope=scope)
        reference = writing["approval_ref"]
        if wrong == "altered":
            Path(writing["handoff"]["destination"]).write_text("ALTERED_AUTHORED_ARTIFACT")
    before = len(product.provider.requests)
    result = product.driver.invoke(
        "task_submit", {"writing_ref": reference, "operation_key": "bad-current-reference"}
    )
    assert not result["ok"] and not product.provider.issues
    assert len(product.provider.requests) == before


def test_a30_progress_approval_cannot_authorize_task_creation(product):
    _, _, task = submit(product)
    progress = approved(product, purpose="progress", task=task)
    before = len(product.provider.requests)
    result = product.driver.invoke(
        "task_submit", {"writing_ref": progress["approval_ref"], "operation_key": "wrong-purpose"}
    )
    assert (
        not result["ok"]
        and len(product.provider.issues) == 1
        and len(product.provider.requests) == before
    )


def test_a32_progress_uses_actual_publisher_author_issue_and_body_readback(product):
    _, _, task = submit(product)
    writing = approved(product, purpose="progress", task=task)
    result = product.driver.invoke(
        "task_progress",
        {
            "task_id": task.id,
            "writing_ref": writing["approval_ref"],
            "operation_key": "fixture-progress",
        },
    )
    assert result["ok"] and result["outcome"] == "confirmed", result
    publication = result["data"]
    assert publication["state"] == "confirmed" and publication["comment_id"]
    comment = product.client.read_comment(publication["comment_id"])
    assert (
        comment.issue.id == task.issue_id and comment.author_id == product.service.scope.principal
    )
    assert (
        comment.body
        == product.writer.approvals_for_progress(product.service.scope, task.id, task.prose_digest)
        .prose(writing["approval_ref"])
        .body
    )
    assert product.ledger.resource(comment.id)["kind"] == "comment"


@pytest.mark.parametrize("fault", ["enrollment", "scope", "config", "claim_store"])
def test_a32_changed_publication_authority_refuses_before_comment_effect(product, fault):
    _, _, task = submit(product)
    writing = approved(product, purpose="progress", task=task)
    if fault == "enrollment":
        del product.observations.grants["fixture-enrollment"]
    elif fault == "scope":
        grant = product.observations.grants["fixture-enrollment"]
        product.observations.grants["fixture-enrollment"] = replace(
            grant, scope=replace(grant.scope, principal=str(uuid4()))
        )
    elif fault == "config":
        write_private(product.config, {"changed_composition": True}, replace=True)
    else:
        path = Path(product.core.context.db.path)
        replacement = path.with_name("replacement.sqlite")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
    before = len(product.provider.requests)
    data = {
        "task_id": task.id,
        "writing_ref": writing["approval_ref"],
        "operation_key": "refused-fixture-progress",
    }
    if fault == "claim_store":
        with pytest.raises(AccessDenied, match="claim store changed"):
            product.driver.invoke("task_progress", data)
    else:
        result = product.driver.invoke("task_progress", data)
        assert not result["ok"] and result["outcome"] == "denied", result
    assert len(product.provider.requests) == before and product.provider.comments == {}


def test_a34_committed_same_key_replay_after_artifact_cleanup_has_no_effect(product):
    writing, first, _ = submit(product)
    Path(writing["handoff"]["destination"]).unlink()
    before = len(product.provider.requests)
    replay = product.driver.invoke(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "fixture-intake"}
    )
    assert (
        replay == first
        and len(product.provider.requests) == before
        and len(product.provider.issues) == 1
    )


def test_a30_no_raw_product_create_without_existing_service_context(product):
    before = len(product.provider.requests)
    with pytest.raises(DriverRefused, match="service_context"):
        product.transport.send(CREATE, {"input": {}}, product.client.credentials())
    assert len(product.provider.requests) == before and not product.provider.issues


def test_a26_archive_only_ledger_owned_writer_created_fixture_issue(product):
    _, _, task = submit(product)
    product.admin.cleanup("archive-issue", task.issue_id, "archive")
    assert product.ledger.resource(task.issue_id)["state"] == "archived"
    assert product.provider.issues[task.issue_id]["archivedAt"]


def test_a35_cross_scope_service_cannot_be_composed_with_the_fixture(product):
    before = len(product.provider.requests)
    other = SimpleNamespace(scope=replace(product.service.scope, principal=str(uuid4())))
    with pytest.raises(DriverRefused, match="composition"):
        PrimaryDriver(other, product.admin, product.transport, client=product.client)
    assert len(product.provider.requests) == before


@pytest.mark.parametrize("paginated_metadata", [False, True])
def test_a34_actual_product_create_reply_loss_uses_existing_reconciliation(
    product, paginated_metadata
):
    writing = approved(product)
    underlying = product.transport.transport

    class LoseReply:
        def send(self, query, variables, credentials):
            response = underlying.send(query, variables, credentials)
            if query == CREATE:
                raise LinearError("authored_product_reply_loss", uncertain=True)
            return response

    product.transport.transport = LoseReply()
    result = product.driver.invoke(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "lost-intake"}
    )
    assert (
        not (result["ok"] and result["outcome"] == "confirmed")
        and len(product.provider.issues) == 1
    )
    operation = next(
        op
        for op in product.core.sync.list_operations(product.service.handle)
        if op.kind == "create_backlog"
    )
    before = len(product.provider.requests)
    key = "product:issue:" + operation.id
    with product.transport.possible_effect_readback(key):
        for query, variables in (
            (CREATE, {"input": {"id": operation.id}}),
            (AGENT_USERS, {"after": 1}),
            (AGENT_USERS, {"after": None, "foreign": True}),
            (NATIVE_TYPE_LABELS, {"after": 1}),
            (NATIVE_TYPE_READ, {"id": str(uuid4())}),
        ):
            with pytest.raises(DriverRefused, match="recovery_context_mismatch"):
                product.transport.send(query, variables, product.client.credentials())
    assert len(product.provider.requests) == before
    if paginated_metadata:

        def paginate(query, variables, result):
            key = {AGENT_USERS: "users", NATIVE_TYPE_LABELS: "issueLabels"}.get(query)
            if key:
                page = result[key]
                first = variables["after"] is None
                assert first or variables["after"] == "authored-next-metadata-page"
                page["nodes"] = page["nodes"][:1] if first else page["nodes"][1:]
                page["pageInfo"] = {
                    "hasNextPage": first,
                    "endCursor": "authored-next-metadata-page" if first else None,
                }
            return result

        product.provider.transform = paginate
    task = product.driver.recover_issue(operation.id)
    assert task.issue_id == operation.id and task.observed_status == "Backlog"
    assert product.ledger.resource(task.issue_id)["kind"] == "issue"
    assert sum(request["query"] == CREATE for request, _ in product.provider.requests) == 1
