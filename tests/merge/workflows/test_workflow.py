"""Actual public local code delivery; provider and semantic review remain authored."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.domains.work.errors import WorkError
from tests.merge.f1_fixtures import FaultOnce
from tests.merge.workflows.helpers import Flow


@pytest.fixture
def flow(tmp_path):
    value = object.__new__(Flow)
    try:
        value.__init__(tmp_path / "owned")
        yield value
    finally:
        if hasattr(value, "runtime"):
            value.close()


@pytest.mark.parametrize("target", ["integration", "release-candidate"])
def test_public_run_actual_completed_worker_checks_publication_and_in_dev(tmp_path, target):
    value = object.__new__(Flow)
    try:
        value.__init__(tmp_path / "independent", target=target)
        result = value.run()
        assert result["ok"] and result["data"]["state"] == "done", result
        task = value.r.core.get_task(value.r.handle, value.task.id)
        assert task.delivered_once and task.observed_status == "In Dev"
        assert value.r.core.get_attempt(value.r.handle, value.attempt.id).state == "done"
        with pytest.raises(WorkError):
            value.r.core.execution.validate_work_lease(
                value.r.handle, value.attempt.id, value.attempt.version
            )
        row = value.workflow.store.read(result["data"]["workflow"])
        completed_steps = [
            change["step"]
            for event in value.journal.events(result["data"]["workflow"])
            if event["kind"] == "merge_workflow"
            for change in json.loads(event["payload"])["changed_steps"]
            if change["result_recorded"]
        ]
        assert completed_steps == [
            "candidate",
            "check_plan",
            "checks",
            "publication_intent",
            "publication_fence",
            "publication",
            "delivery",
            "confirmation",
        ]
        assert set(row["steps"]) == set(completed_steps)
        candidate = row["steps"]["candidate"]["result"]
        assert candidate["commit"] == git(value.tree, "rev-parse", "refs/heads/" + target)
        assert Path(candidate["path"]).is_dir() and value.pair.database.is_file()
        assert git(value.tree, "rev-parse", "HEAD") == value.new
        assert len(value.executor.records) == 1
        assert value.executor.records[0]["completion"]["group"]["confirmed_stop"]
        assert value.workflow.timings and all(t["seconds"] >= 0 for t in value.workflow.timings)
        (value.root / "workflow-result.json").write_text(
            json.dumps(
                {
                    "setup_seconds": value.setup_seconds,
                    "worker_commit_seconds": value.worker_commit_seconds,
                    "steps": value.workflow.timings,
                    "publication": row["steps"]["publication"]["result"],
                    "check_executions": value.executor.records,
                    "scope": "actual local mechanical flow; authored provider/work-review facts",
                },
                indent=2,
            )
        )
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_committed_public_replay_and_resume_after_delivery_do_not_repeat_effects(flow):
    first = flow.run()
    assert first["data"]["state"] == "done", first
    ref = first["data"]["workflow"]
    candidate = flow.workflow.store.read(ref)["steps"]["candidate"]["result"]
    before = (len(flow.journal.events(candidate["operation_ref"])), len(flow.executor.records))
    assert flow.run() == first
    assert flow.resume(ref) == first
    assert before == (
        len(flow.journal.events(candidate["operation_ref"])),
        len(flow.executor.records),
    )
    assert not flow.run("different-key")["ok"]


@pytest.mark.parametrize(
    "stage",
    [
        "candidate",
        "check_plan",
        "checks",
        "publication_intent",
        "publication_fence",
        "publication",
        "delivery",
        "confirmation",
    ],
)
def test_lost_stage_reply_reconciles_exact_durable_effect(flow, stage):
    flow.workflow.faults = FaultOnce("after_" + stage)
    first = flow.run()
    assert not first["ok"]
    ref = next(iter(flow.workflow.references))
    assert flow.workflow.store.read(ref)["steps"][stage]["result"] is None
    resumed = flow.resume(ref)
    assert resumed["data"]["state"] == "done", resumed
    assert len(flow.executor.records) == 1
    events = flow.r.core.notifications.list_events(flow.r.handle)
    assert (
        sum(e.kind == "transition_requested" and e.payload.get("cause") == "merge" for e in events)
        == 1
    )


@pytest.mark.parametrize(
    "change",
    ["hold", "stale_version", "stale_decision", "nonhuman", "foreign_task", "foreign_plan"],
)
def test_current_authority_refuses_before_candidate_effect(flow, change):
    if change == "hold":
        flow.runtime.hold(flow.dispatch)
    elif change == "stale_version":
        flow.plan = replace(
            flow.plan, grant=replace(flow.grant, task_version=flow.grant.task_version - 1)
        )
    elif change == "stale_decision":
        flow.plan = replace(
            flow.plan, grant=replace(flow.grant, decision_revision=flow.grant.decision_revision + 1)
        )
    elif change == "nonhuman":
        grant = replace(
            flow.grant, decision_ref=flow.r.decision(flow.task, "In Dev", actor="model")
        )
        flow.plan = replace(flow.plan, grant=grant, require_human_acceptance=True)
    if change in {"stale_version", "stale_decision", "nonhuman"}:
        # Trusted composition binds the immutable original plan. Changing only
        # the controller's derived current plan cannot replace that authority.
        flow.workflow.close()
        flow.workflow = flow.compose()
        flow.app.workflows = flow.workflow
    data = {"task_id": flow.task.id, "plan_ref": flow.plan.id, "operation_key": "public-merge"}
    if change == "foreign_task":
        data["task_id"] = "foreign"
    if change == "foreign_plan":
        data["plan_ref"] = "foreign"
    result = flow.app.call("workflow_run", data)
    assert not result["ok"], result
    if change in {"stale_version", "stale_decision", "nonhuman"}:
        assert (
            result["error"]["message"]
            == {
                "stale_version": "Task version has changed",
                "stale_decision": "A newer human decision takes precedence",
                "nonhuman": "Configured explicit human acceptance is required",
            }[change]
        )
    assert not list(flow.candidate_profile.root.iterdir())
    assert not flow.executor.records
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old


def test_missing_semantic_work_cannot_be_inferred_from_checks(tmp_path):
    value = object.__new__(Flow)
    try:
        value.__init__(tmp_path / "owned", completed=False)
        result = value.run()
        assert not result["ok"] and "semantic" in result["error"]["message"]
        assert not list(value.candidate_profile.root.iterdir()) and not value.executor.records
    finally:
        if hasattr(value, "runtime"):
            value.close()


@pytest.mark.parametrize("dirty", [False, True])
def test_target_checkout_in_another_worktree_is_explicitly_refused(flow, dirty):
    from ai_task_manager.domains.work.errors import AccessDenied

    other = flow.root / "target-checkout"
    git(flow.tree, "worktree", "add", str(other), flow.target)
    if dirty:
        (other / "untracked.txt").write_text("retain this dirty checkout")
    result = flow.run()
    assert not result["ok"], result
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old
    assert git(other, "rev-parse", "HEAD") == flow.old
    if dirty:
        assert (other / "untracked.txt").read_text() == "retain this dirty checkout"
    assert flow.pair.database.is_file()
    (consumer,) = flow.workflow.consumers.values()
    operation = flow.journal.current(consumer.registration["ref"])
    assert operation["facts"]["publication_intent"]["mode"] == "cas"
    assert "local_git" not in operation["facts"]
    # A clean checkout also reaches this deliberate guard before any Git intent.
    with pytest.raises(AccessDenied, match="CAS cannot update a checked-out target"):
        consumer.service.publisher._validate(operation, consumer.grant.publication)


def test_catalog_drift_after_physical_checks_denies_publication(flow):
    original = flow.checks.run

    def changed(*args):
        result = original(*args)
        flow.catalogs._bindings[flow.r.scope.project][0].path.write_text("{}")
        return result

    flow.checks.run = changed
    result = flow.run()
    assert not result["ok"]
    assert len(flow.executor.records) == 1
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old


def test_pending_workflow_owner_does_not_retry_or_spawn(flow):
    ref = flow.workflow.store.create(flow.plan, flow.candidate_profile, "public-merge")
    owner = flow.workflow.store.claim(ref)
    try:
        assert not flow.run()["ok"]
        assert not list(flow.candidate_profile.root.iterdir())
    finally:
        flow.workflow.store.release(ref, owner)


def test_retained_owner_compatibility_deadline_and_cleanup_contract(flow):
    assert isinstance(flow.workflow, WorkflowService)
    flow.workflow.check_owner_deadline(flow.plan.deadline)
    with pytest.raises(WorkError):
        flow.workflow.check_owner_deadline(flow.plan.deadline - 1)
    result = flow.run()
    assert result["data"]["state"] == "done", result
    closed = flow.workflow.close()
    assert closed and all(r["candidate_children"] == r["publication_children"] == 0 for r in closed)
    assert flow.pair.database.is_file()
    candidate = flow.workflow.store.read(result["data"]["workflow"])["steps"]["candidate"]["result"]
    assert Path(candidate["path"]).is_dir()


@pytest.mark.parametrize("change", ["source", "target", "check_source", "database", "human"])
def test_post_check_drift_refuses_before_publication_and_retains_resources(flow, change):
    original = flow.checks.run
    moved = flow.root / "retained-database.sqlite"

    def changed(*args):
        result = original(*args)
        if change == "source":
            git(flow.tree, "update-ref", flow.grant.source_ref, flow.old, flow.new)
        elif change == "target":
            git(flow.tree, "update-ref", "refs/heads/" + flow.target, flow.new, flow.old)
        elif change == "check_source":
            flow.guard.source = "0" * 64
        elif change == "human":
            flow.runtime.hold(flow.dispatch)
        else:
            flow.pair.database.rename(moved)
        return result

    flow.checks.run = changed
    try:
        result = flow.run()
        assert not result["ok"], result
        assert not any(
            row["facts"].get("local_git") for row in flow.journal.for_attempt(flow.attempt.id)
        )
        assert flow.r.core.get_task(flow.r.handle, flow.task.id).observed_status == "In Progress"
        assert flow.pair.worktree.is_dir()
        if change == "database":
            assert moved.is_file()
        else:
            assert flow.pair.database.is_file()
    finally:
        if moved.exists():
            moved.rename(flow.pair.database)


def test_unavailable_impact_is_not_silently_replaced_with_an_empty_plan(flow):
    from ai_task_manager.domains.checks.models import Availability
    from tests.checks.fixtures import discovery

    flow.check_inputs.discover = lambda candidate: discovery(status=Availability.UNAVAILABLE)
    result = flow.run()
    assert not result["ok"], result
    assert not flow.executor.records
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old


def test_conflict_is_retained_and_never_published(tmp_path):
    value = object.__new__(Flow)
    try:
        value.__init__(tmp_path / "owned", conflict=True)
        result = value.run()
        assert result["ok"] and result["outcome"] == "blocked", result
        assert result["data"]["reason"] == "candidate_conflict"
        assert not value.executor.records
        assert git(value.tree, "rev-parse", "refs/heads/" + value.target) == value.old
        assert value.pair.database.is_file()
    finally:
        if hasattr(value, "runtime"):
            value.close()


@pytest.mark.parametrize("change", ["expiry", "metadata", "wrong_binding"])
def test_actual_publication_witness_requires_current_exact_preparation(flow, change):
    from ai_task_manager.domains.merging.candidates import CandidateResult

    flow.workflow.faults = FaultOnce("after_publication")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    row = flow.workflow.store.read(ref)
    candidate = CandidateResult(**row["steps"]["candidate"]["result"])
    consumer = flow.workflow.consumers[ref]
    publication = consumer.service.publisher.reconcile(consumer.registration["ref"])
    check_ref = row["steps"]["check_plan"]["result"]["ref"]
    proof = flow.evidence.prepare(
        flow.r.core, flow.r.handle, flow.plan, candidate, publication, consumer, check_ref
    )
    assert flow.evidence.verify_evidence(proof).payload["commit"] == candidate.commit
    if change == "expiry":
        flow.r.now += 6
    elif change == "metadata":
        target = Path(flow.candidate_profile.source["common"]) / ("refs/heads/" + flow.target)
        target.write_bytes(target.read_bytes())
    else:
        consumer.grant = replace(
            consumer.grant, binding=replace(consumer.grant.binding, principal_id="foreign")
        )
        with pytest.raises(WorkError):
            flow.evidence.prepare(
                flow.r.core, flow.r.handle, flow.plan, candidate, publication, consumer, check_ref
            )
        return
    with pytest.raises(WorkError):
        flow.evidence.verify_evidence(proof)
    assert flow.r.core.get_task(flow.r.handle, flow.task.id).observed_status == "In Progress"


def test_delivery_recovery_requires_exact_evidence_reference_not_only_decision(flow):
    result = flow.run()
    assert result["data"]["state"] == "done", result
    row = flow.workflow.store.read(result["data"]["workflow"])
    task = flow.r.core.get_task(flow.r.handle, flow.task.id)
    intent = row["steps"]["delivery"]
    assert flow.workflow._delivery_event(task, intent)
    altered = dict(intent, payload=dict(intent["payload"], merge_ref="foreign-merge-proof"))
    assert not flow.workflow._delivery_event(task, altered)


def test_actual_retained_workflow_owner_accepts_code_service_and_public_client(flow):
    import json
    import shutil
    import tempfile
    import threading
    import time
    from uuid import uuid4

    from ai_task_manager.adapters.decisions.authority import DecisionAuthority
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.workflow_control.client import WorkflowClient
    from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner
    from ai_task_manager.config.claim_store import enroll_claim_store, verify_claim_store
    from ai_task_manager.config.files import write_private

    root = Path(tempfile.mkdtemp(prefix="aitm-merge-owner-", dir=Path("/tmp").resolve()))
    identity = root.stat().st_dev, root.stat().st_ino
    stop, ready = threading.Event(), threading.Event()
    failures = []
    thread = None
    try:
        authority = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
        # Enrollment requires installer-prepared private state; the generic
        # internal Core fixture does not prepare file permissions for an owner.
        Path(flow.r.core.context.db.path).chmod(0o600)
        enroll_claim_store(flow.r.core.context.db, authority)
        flow.app.claim_store = verify_claim_store(flow.r.core.context.db, authority)
        flow.app.decisions = DecisionAuthority(authority)
        config = root / "config.json"
        write_private(
            config,
            {
                "core_database": str(flow.r.core.context.db.path),
                "workflow_control": {
                    "record": str(root / "record.json"),
                    "endpoint": str(root / "owner.sock"),
                    "instance": str(uuid4()),
                },
            },
        )
        owner = WorkflowOwner(flow.app, config, deadline=time.time() + 30)

        def serve():
            try:
                owner.serve(stop, ready.set)
            except BaseException as error:
                failures.append(type(error).__name__)

        thread = threading.Thread(target=serve, name="owned-code-merge-owner")
        thread.start()
        assert ready.wait(3)
        from dataclasses import asdict

        client = WorkflowClient(config, asdict(flow.r.scope))
        result = client.forward(
            "workflow_run",
            {
                "task_id": flow.task.id,
                "plan_ref": flow.plan.id,
                "operation_key": "owner-code-merge",
            },
        )
        assert result["data"]["state"] == "done", result
        ref = result["data"]["workflow"]
        assert client.forward("workflow_resume", {"workflow_ref": ref}) == result
    finally:
        stop.set()
        if thread is not None:
            thread.join(5)
        facts = {
            "thread_stopped": thread is None or not thread.is_alive(),
            "failures": failures,
            "socket_absent": not (root / "owner.sock").exists(),
            "path": str(root),
        }
        (flow.root / "retained-owner-result.json").write_text(json.dumps(facts, indent=2))
        if facts["thread_stopped"] and facts["socket_absent"]:
            assert (root.stat().st_dev, root.stat().st_ino) == identity
            shutil.rmtree(root)
        assert facts["thread_stopped"] and facts["socket_absent"] and not failures


def test_actual_failed_check_never_permits_publication(tmp_path):
    value = object.__new__(Flow)
    try:
        value.__init__(tmp_path / "owned", value="5")
        result = value.run()
        assert (
            result["data"]["state"] == "blocked" and result["data"]["reason"] == "checks_failed"
        ), result
        assert value.executor.records[0]["completion"]["returncode"] == 1
        assert git(value.tree, "rev-parse", "refs/heads/" + value.target) == value.old
        assert value.pair.database.is_file()
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_fresh_controller_recovers_committed_publication_without_new_git_child(flow):
    flow.workflow.faults = FaultOnce("after_publication")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    consumer = flow.workflow.consumers[ref]
    pub_ref = consumer.registration["ref"]
    before = flow.journal.current(pub_ref)["facts"]["local_git"]["children"]
    flow.workflow = flow.compose()
    flow.app.workflows = flow.workflow
    resumed = flow.resume(ref)
    assert resumed["data"]["state"] == "done", resumed
    assert flow.journal.current(pub_ref)["facts"]["local_git"]["children"] == before
    assert len(flow.executor.records) == 1


def test_actual_publication_fences_new_work_before_provider_status_advances(flow):
    flow.workflow.faults = FaultOnce("after_publication")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    candidate = flow.workflow.store.read(ref)["steps"]["candidate"]["result"]
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == candidate["commit"]
    task = flow.r.core.get_task(flow.r.handle, flow.task.id)
    assert task.observed_status == "In Progress" and not task.delivered_once
    before = flow.r.core.get_attempt(flow.r.handle, flow.attempt.id)
    assert before.state == "done"
    with pytest.raises(WorkError):
        flow.r.core.execution.reserve(
            flow.r.handle,
            task.id,
            "fixture",
            task.version,
            task.decision_revision,
            flow.r.decision(task, "In Progress"),
            "publication-fence-probe",
        )


def test_pending_fence_is_committed_before_actual_gated_git_release(flow):
    from ai_task_manager.adapters.git.publication import LocalGit
    from ai_task_manager.storage import issue_claims, merging_fences

    seen = []

    def barrier():
        task = flow.r.core.get_task(flow.r.handle, flow.task.id)
        with flow.r.core.context.db.transaction() as conn:
            rows = merging_fences.pending(conn, issue_claims.identity(flow.r.scope, task.issue_id))
            assert len(rows) == 1
        with pytest.raises(WorkError):
            flow.r.core.execution.reserve(
                flow.r.handle,
                task.id,
                "fixture",
                task.version,
                task.decision_revision,
                flow.r.decision(task, "In Progress"),
                "release-barrier-reserve",
            )
        seen.append(rows[0]["context"])

    flow.workflow.publisher_factory = lambda j, p, b: LocalGit(
        j, p, b, faults=FaultOnce("before_release", barrier)
    )
    result = flow.run()
    assert result["data"]["state"] == "done", result
    assert len(seen) == 1
    (flow.root / "publication-release-barrier.json").write_text(json.dumps(seen, indent=2))


def test_actual_publication_fact_survives_later_hold_without_advancing_status(flow):
    from ai_task_manager.adapters.git.publication import LocalGit
    from ai_task_manager.storage import issue_claims

    flow.workflow.publisher_factory = lambda j, p, b: LocalGit(
        j,
        p,
        b,
        faults=FaultOnce("after_effect_observation", lambda: flow.runtime.hold(flow.dispatch)),
    )
    result = flow.run()
    assert not result["ok"]
    task = flow.r.core.get_task(flow.r.handle, flow.task.id)
    assert task.human_hold and task.observed_status == "In Progress"
    with flow.r.core.context.db.transaction() as conn:
        assert issue_claims.known_delivered(conn, flow.r.scope, task)
    release = flow.r.proof(
        "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": False}
    )
    task = flow.r.core.decisions.record(flow.r.handle, task.id, task.version, release, flow.r.key())
    with pytest.raises(WorkError):
        flow.r.core.execution.reserve(
            flow.r.handle,
            task.id,
            "fixture",
            task.version,
            task.decision_revision,
            flow.r.decision(task, "In Progress"),
            "released-hold-cannot-reopen-merged-work",
        )


def test_pending_fence_survives_binding_map_change_and_foreign_context_refuses(flow):
    from ai_task_manager.application.publication_fences import PublicationContext
    from ai_task_manager.storage import issue_claims, merging_fences

    flow.workflow.faults = FaultOnce("after_publication_fence")
    assert not flow.run()["ok"]
    task = flow.r.core.get_task(flow.r.handle, flow.task.id)
    with flow.r.core.context.db.transaction() as conn:
        key = issue_claims.identity(flow.r.scope, task.issue_id)
        row = merging_fences.pending(conn, key)[0]
        conn.execute("DELETE FROM issue_bindings WHERE identity_key=?", (key,))
    context = PublicationContext(**row["context"])
    grant = flow.grant
    with pytest.raises(WorkError):
        with flow.r.core.execution.merge_snapshot_boundary(
            flow.r.handle,
            task.id,
            flow.attempt.id,
            task.version,
            flow.attempt.version,
            task.decision_revision,
            grant.decision_ref,
            publication_context=replace(context, binding_digest="0" * 64),
        ):
            raise AssertionError("foreign publication authority entered")
    with flow.r.core.context.db.transaction() as conn:
        # Canonical provider/workspace/issue authority is independent of a
        # mutable principal/session binding and of current issue_bindings rows.
        other = replace(flow.r.scope, principal="other", logical_session="other")
        with pytest.raises(WorkError):
            issue_claims.require_open(conn, other, task.issue_id)
    with pytest.raises(WorkError):
        flow.r.core.execution.reserve(
            flow.r.handle,
            task.id,
            "fixture",
            task.version,
            task.decision_revision,
            flow.r.decision(task, "In Progress"),
            "canonical-pending-probe",
        )
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old


def test_expired_prepared_intent_refuses_before_pending_fence_or_target_effect(flow):
    from ai_task_manager.storage import issue_claims, merging_fences

    flow.workflow.faults = FaultOnce("after_publication_intent")
    assert not flow.run()["ok"]
    intent_refs = [
        reference
        for reference, (fact, _) in flow.evidence.prepared.items()
        if fact.kind == "publication_intent"
    ]
    assert len(intent_refs) == 1
    expired = intent_refs[0]
    before_calls = flow.guard.calls
    flow.r.now += 6
    ref = next(iter(flow.workflow.references))
    grant = flow.grant
    with pytest.raises(WorkError):
        flow.r.core.publications.begin(
            flow.r.handle,
            grant.binding.task_id,
            grant.binding.attempt_id,
            grant.task_version,
            grant.attempt_version,
            grant.decision_revision,
            grant.decision_ref,
            expired,
            "consume-expired-prepared-intent",
        )
    with flow.r.core.context.db.transaction() as conn:
        assert not merging_fences.pending(
            conn, issue_claims.identity(flow.r.scope, flow.task.issue_id)
        )
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old
    resumed = flow.resume(ref)
    assert resumed["data"]["state"] == "done", resumed
    assert flow.guard.calls > before_calls
    assert flow.evidence.prepared[expired][0].expires_at > flow.r.now


def test_committed_begin_and_confirmation_replay_after_expiry_have_no_new_effect(flow):
    from ai_task_manager.application.publication_fences import PublicationContext

    result = flow.run()
    assert result["data"]["state"] == "done", result
    ref = result["data"]["workflow"]
    row = flow.workflow.store.read(ref)
    context = PublicationContext(**row["steps"]["publication_fence"]["result"])
    before = flow.r.core.notifications.list_events(flow.r.handle)
    flow.r.now += 6
    g = flow.grant
    assert (
        flow.r.core.publications.begin(
            flow.r.handle,
            g.binding.task_id,
            g.binding.attempt_id,
            g.task_version,
            g.attempt_version,
            g.decision_revision,
            g.decision_ref,
            row["steps"]["publication_intent"]["result"]["ref"],
            ref + ":fence",
        )
        == context
    )
    assert (
        flow.r.core.publications.confirm(
            flow.r.handle,
            g.binding.task_id,
            context,
            row["steps"]["delivery"]["payload"]["merge_ref"],
            ref + ":publication-confirmed",
        )["state"]
        == "committed"
    )
    assert before == flow.r.core.notifications.list_events(flow.r.handle)
    with pytest.raises(WorkError):
        with flow.r.core.execution.merge_snapshot_boundary(
            flow.r.handle,
            g.binding.task_id,
            g.binding.attempt_id,
            g.task_version,
            g.attempt_version,
            g.decision_revision,
            g.decision_ref,
            publication_context=context,
        ):
            raise AssertionError("delivered context cannot authorize another publication")


def test_pending_without_actual_success_cannot_be_confirmed_or_silently_released(flow):
    from ai_task_manager.application.publication_fences import PublicationContext
    from ai_task_manager.storage import issue_claims, merging_fences

    flow.workflow.faults = FaultOnce("after_publication_fence")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    row = flow.workflow.store.read(ref)
    with flow.r.core.context.db.transaction() as conn:
        pending = merging_fences.pending(
            conn, issue_claims.identity(flow.r.scope, flow.task.issue_id)
        )[0]
    context = PublicationContext(**pending["context"])
    with pytest.raises(WorkError):
        flow.r.core.publications.confirm(
            flow.r.handle,
            flow.task.id,
            context,
            row["steps"]["publication_intent"]["result"]["ref"],
            "intent-is-not-success",
        )
    assert not hasattr(flow.r.core.publications, "release")
    with flow.r.core.context.db.transaction() as conn:
        assert merging_fences.pending(conn, issue_claims.identity(flow.r.scope, flow.task.issue_id))
    assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old


def test_permanent_publication_fact_allows_new_authorized_status_finalization(flow):
    from ai_task_manager.adapters.git.publication import LocalGit
    from ai_task_manager.domains.merging.candidates import CandidateResult
    from ai_task_manager.domains.work.models import Cause, IssueStatus
    from tests.runtime.helpers import ProviderFixture

    flow.workflow.publisher_factory = lambda j, p, b: LocalGit(
        j,
        p,
        b,
        faults=FaultOnce("after_effect_observation", lambda: flow.runtime.hold(flow.dispatch)),
    )
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    row = flow.workflow.store.read(ref)
    candidate = CandidateResult(**row["steps"]["candidate"]["result"])
    consumer = flow.workflow.consumers[ref]
    task = flow.r.core.get_task(flow.r.handle, flow.task.id)
    release = flow.r.proof(
        "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": False}
    )
    task = flow.r.core.decisions.record(flow.r.handle, task.id, task.version, release, flow.r.key())
    publication = consumer.service.publisher.reconcile(consumer.registration["ref"])
    before = flow.journal.current(consumer.registration["ref"])["facts"]["local_git"]["children"]
    proof = flow.evidence.prepare(
        flow.r.core,
        flow.r.handle,
        flow.plan,
        candidate,
        publication,
        consumer,
        row["steps"]["check_plan"]["result"]["ref"],
    )
    task = flow.r.core.transitions.transition(
        flow.r.handle,
        task.id,
        IssueStatus.IN_DEV,
        Cause.MERGE,
        task.version,
        task.decision_revision,
        flow.r.decision(task, "In Dev"),
        {"merge": proof},
        "new-authorized-delivery-finalization",
    )
    task = ProviderFixture(flow.r).confirm_pending(flow.r.handle, task.id)
    assert task.delivered_once and task.observed_status == "In Dev"
    assert (
        flow.journal.current(consumer.registration["ref"])["facts"]["local_git"]["children"]
        == before
    )
    assert flow.pair.database.is_file()
