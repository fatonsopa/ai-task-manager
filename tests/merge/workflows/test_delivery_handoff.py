"""Actual local publication proves exact collector-to-canonical-fence handoff."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.application.publication_fences import PublicationContext
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.installation.workflows.code_delivery import DeliveryResources
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.merge.workflows.helpers import Flow
from tests.merge.workflows.test_candidate_review import Captures
from tests.merge.workflows.test_workflow import FaultOnce
from tests.scheduling.helpers import snapshot


@pytest.fixture
def owned(tmp_path):
    flow = Flow(tmp_path / "owned")
    flow.check_inputs.select_retained = flow.check_inputs.discover
    source = AuthoredDependencyCollection(flow.r.core, flow.r.handle)
    lifecycle = DeliveryDependencies(flow.r.core, flow.r.handle, source, flow.plan.deadline)
    lifecycle.bind(
        {
            "task_id": flow.task.id,
            "attempt_id": flow.attempt.id,
            "prose_digest": flow.task.prose_digest,
            "decision_revision": flow.task.decision_revision,
        }
    )
    resources = DeliveryResources(
        flow.runtime.resources, lambda: flow.r.now, flow.plan.deadline, lifecycle
    )
    flow.candidate.authority.resources = resources
    reviews = CodeCandidateReviews(
        flow.r.core,
        flow.r.handle,
        flow.candidate,
        ("src/a.txt", "tests/a.txt", "config/checks.json"),
        Captures(lambda: flow.r.now),
    )
    flow.workflow = flow.compose(candidate_reviews=reviews, publication_lifecycle=lifecycle)
    flow.app.workflows = flow.workflow
    try:
        yield flow, lifecycle
    finally:
        lifecycle.close()
        assert lifecycle._thread is None or not lifecycle._thread.is_alive()
        flow.close()


def test_normal_publication_handoff_and_canonical_committed_inspection(owned):
    flow, lifecycle = owned
    result = flow.run()
    assert result["data"]["state"] == "done", result
    ref = result["data"]["workflow"]
    row = flow.workflow.store.read(ref)
    context = PublicationContext(**row["steps"]["publication_fence"]["result"])
    fact = flow.r.core.publications.inspect(flow.r.handle, flow.task.id, context)
    assert fact["state"] == "committed"
    fact["intent"]["task_id"] = "mutated-local-copy"
    assert (
        flow.r.core.publications.inspect(flow.r.handle, flow.task.id, context)["intent"]["task_id"]
        == flow.task.id
    )
    assert lifecycle._thread is None or not lifecycle._thread.is_alive()
    before = flow.r.core.notifications.list_events(flow.r.handle)
    flow.r.now += 31
    g = flow.grant
    assert (
        flow.r.core.publications.begin(
            flow.r.handle,
            flow.task.id,
            flow.attempt.id,
            g.task_version,
            g.attempt_version,
            g.decision_revision,
            g.decision_ref,
            row["steps"]["publication_intent"]["result"]["ref"],
            ref + ":fence",
        )
        == context
    )
    assert flow.resume(ref)["data"]["state"] == "done"
    assert flow.r.core.notifications.list_events(flow.r.handle) == before
    with pytest.raises(WorkError):
        with flow.r.core.execution.merge_snapshot_boundary(
            flow.r.handle,
            flow.task.id,
            flow.attempt.id,
            flow.grant.task_version,
            flow.grant.attempt_version,
            flow.grant.decision_revision,
            flow.grant.decision_ref,
            publication_context=context,
        ):
            pytest.fail("Committed inspection cannot authorize another effect")
    with pytest.raises(WorkError):
        flow.r.core.publications.inspect(
            flow.r.handle, flow.task.id, replace(context, binding_digest="0" * 64)
        )
    foreign = flow.r.session(replace(flow.r.scope, logical_session="foreign"))
    with pytest.raises(WorkError):
        flow.r.core.publications.inspect(foreign, flow.task.id, context)


@pytest.mark.parametrize("case", ["expired", "invalidated", "foreign", "failed_observer"])
def test_new_publication_refuses_without_current_original_graph(owned, case):
    flow, lifecycle = owned
    flow.workflow.faults = FaultOnce("after_publication_intent")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    intent = next(
        reference
        for reference, (fact, _) in flow.evidence.prepared.items()
        if fact.kind == "publication_intent"
    )
    if case == "expired":
        flow.r.now += 31
    elif case == "invalidated":
        with flow.r.db.transaction() as conn:
            conn.execute(
                "UPDATE scheduling_attempts SET invalidated_by='authored-invalidated' "
                "WHERE attempt_id=?",
                (flow.attempt.id,),
            )
    elif case == "foreign":
        value = snapshot(flow.r.core, flow.r.handle, flow.task, flow.r.now)
        value = replace(value, scope=replace(flow.r.scope, principal="foreign"))
        reference = flow.r.scheduling_sources.issue(value)
        with pytest.raises(WorkError):
            flow.r.core.scheduling.observe(
                flow.r.handle,
                flow.task.id,
                flow.task.version,
                flow.task.decision_revision,
                reference,
                "foreign-graph",
            )
        with pytest.raises(WorkError):
            lifecycle.adopt_fence(
                flow.plan, PublicationContext("fabricated", "0" * 64, flow.new), "0" * 64, flow.new
            )
        return
    else:
        lifecycle._error = AccessDenied("Authored unavailable current source")
    flow.workflow.faults = None
    if case == "failed_observer":
        assert not flow.resume(ref)["ok"]
    else:
        g = flow.grant
        with pytest.raises(WorkError):
            flow.r.core.publications.begin(
                flow.r.handle,
                flow.task.id,
                flow.attempt.id,
                g.task_version,
                g.attempt_version,
                g.decision_revision,
                g.decision_ref,
                intent,
                "new-fence-without-current-graph",
            )
    with flow.r.db.transaction() as conn:
        from ai_task_manager.storage import issue_claims, merging_fences

        assert not merging_fences.pending(
            conn, issue_claims.identity(flow.r.scope, flow.task.issue_id)
        )


@pytest.mark.parametrize("cut", ["after_publication_fence", "after_publication"])
def test_saved_exact_publication_context_resumes_without_generic_authority(owned, cut):
    flow, lifecycle = owned
    flow.workflow.faults = FaultOnce(cut)
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    row = flow.workflow.store.read(ref)
    candidate = row["steps"]["candidate"]["result"]
    flow.r.now += 31
    flow.workflow.faults = None
    result = flow.resume(ref)
    assert result.get("ok"), result
    assert result["data"]["state"] == "done", result
    assert flow.workflow.store.read(ref)["steps"]["candidate"]["result"] == candidate
    assert lifecycle._thread is None or not lifecycle._thread.is_alive()


def _published_milestone_case(tmp_path, condition):
    """Real saved Git publication/merge producer and signed source; no provider refetch."""
    import json
    from dataclasses import asdict
    from types import SimpleNamespace
    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from ai_task_manager.application.service.facade import ApplicationService
    from ai_task_manager.domains.scheduling.models import Dependency, DependencySnapshot
    from ai_task_manager.domains.scheduling.resources import AdmissionResources
    from ai_task_manager.domains.work.models import TaskProse
    from ai_task_manager.installation.workflows.member_evidence import MemberMilestones

    flow = Flow(tmp_path / "queue-publication")
    try:
        # The existing authored selector binds checks to the actual retained
        # candidate before the real check-store/file verification is repeated.
        flow.check_inputs.select_retained = flow.check_inputs.discover
        delivered = flow.run()
        assert delivered["ok"] and delivered["data"]["state"] == "done", delivered
        r = flow.r
        predecessor = r.core.get_task(r.handle, flow.task.id)
        reference = next(ref for ref, (fact, _) in flow.evidence.prepared.items() if fact.kind == "merge")
        delivery = SimpleNamespace(
            evidence=flow.evidence, issuer=None,
            prepare_merge_milestone=lambda ref, plan: flow.workflow.prepare_merge_milestone(
                delivered["data"]["workflow"], ref
            ),
        )
        member = SimpleNamespace(
            spec=SimpleNamespace(data={"scope": asdict(r.scope), "task_id": predecessor.id,
                                      "plan_ref": flow.plan.id}, check=lambda: None),
            review_issuer=None,
            service=SimpleNamespace(workflows=SimpleNamespace(driver=SimpleNamespace(
                code_delivery=delivery, plans=SimpleNamespace(get=lambda ref: flow.plan)))),
        )
        r.core.context.evidence = MemberMilestones(r.core.context.evidence.routes, (member,))
        prose = TaskProse("Inspect the published dependency", "Retain its current queue explanation.", condition.startswith("delivery:"))
        task = r.core.intake.submit(r.handle, prose, r.approval(prose), r.key())
        r.resource_sources.rows[task.id] = AdmissionResources(
            r.scope, task.id, task.prose_digest, task.decision_revision, "authored-queue", (), (),
        )
        task = r.transition(r.confirm(task), "Todo", "enqueue")
        receipts = DependencyReceipts(tmp_path / "provider-window.sqlite", tmp_path / "provider-window.key",
                                     clock=lambda: r.now)
        # Any provider collection would fail: this producer owns only an already
        # signed original observation and the existing local publication.
        client = SimpleNamespace(expected=SimpleNamespace(team_id="authored-current-team"))
        source = DependencySource(r.core, r.handle, client, receipts,
                                  milestones=((task.prose_digest, predecessor.prose_digest),))
        r.core.scheduling.sources = receipts
        current = r.core.scheduling.explain(r.handle, task.id)
        begun = receipts.begin(r.scope, task, current["graph_version"], "original-provider-observation")
        observed = DependencySnapshot(
            r.scope, task.id, task.prose_digest, task.issue_id, source.source_id, begun["generation"],
            r.now, r.now + 30, "complete", "normal",
            (Dependency(predecessor.id, "code_merged", evidence_refs=(("merge", reference),)),),
            current["graph_version"],
        )
        signed = receipts.finish(r.scope, task.id, "original-provider-observation", begun["context"],
                                 observed, {"scope": "authored original provider window; no refetch"})
        r.core.scheduling.observe(r.handle, task.id, task.version, task.decision_revision,
                                  signed, "publish-original-provider-observation")
        if condition.startswith(("lease:", "auth:", "assignment:", "admit:", "delivery:")):
            prefix, selected = condition.split(":", 1)
            return _native_lease_case(flow, task, source, signed, observed, reference, tmp_path, selected, consumer=prefix)
        app = ApplicationService(r.core, r.handle, dependencies=source)
        initial = app.call("task_show", {"task_id": task.id})
        expiry = flow.evidence.prepared[reference][0].expires_at
        r.now = observed.expires_at if condition == "stale" else expiry + 1
        if condition == "held":
            hold = r.proof("human_decision", predecessor.id,
                           {"previous_revision": predecessor.decision_revision, "hold": True})
            r.core.decisions.record(r.handle, predecessor.id, predecessor.version, hold, r.key())
        elif condition == "target":
            # Even a still-unexpired cached witness must reject actual target change.
            r.now = observed.observed_at
            target = git(flow.runtime.repo, "rev-parse", "refs/heads/integration")
            git(flow.runtime.repo, "update-ref", "refs/heads/integration", flow.old, target)
        expired = app.call("task_show", {"task_id": task.id})
        # Exercise list preparation independently, after show's fresh local proof
        # expires again, without going backwards inside any authority window.
        r.now = max(r.now, flow.evidence.prepared[reference][0].expires_at + 1)
        listed = app.call("task_list", {})
        listed_view = next(row for row in listed["data"] if row["id"] == task.id)
        preparation_failure = None
        try:
            source.prepare_current_milestones(None, task_id=task.id)
        except WorkError as error:
            preparation_failure = {"type": type(error).__name__, "message": str(error)}
        restored = app.call("task_show", {"task_id": task.id})
        facts = {"scope": "Controlled local publication/query; not a proven G12 live expiry cause",
                 "condition": condition,
                 "provider_expiry": observed.expires_at, "clock": r.now, "old_local_expiry": expiry,
                 "new_local_expiry": flow.evidence.prepared[reference][0].expires_at,
                 "preparation_failure": preparation_failure,
                 "initial": initial, "after_local_expiry": expired,
                 "list_after_local_expiry": listed_view, "after_existing_preparation": restored}
        (tmp_path / "lead-projection-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert initial["data"]["scheduling"]["state"] == "ready", facts
        if condition == "current":
            assert restored["data"]["scheduling"]["state"] == "ready", facts
            assert expired["data"]["scheduling"]["state"] == "ready", facts
            assert listed_view["scheduling"]["state"] == "ready", facts
        else:
            assert restored["data"]["scheduling"]["state"] != "ready", facts
            assert expired["data"]["scheduling"]["state"] != "ready", facts
            assert listed_view["scheduling"]["state"] != "ready", facts
            assert preparation_failure is not None, facts
    finally:
        flow.close()


@pytest.mark.parametrize("condition", ["current", "stale", "held", "target"])
def test_public_queue_queries_reprepare_only_current_local_merge_milestone(tmp_path, condition):
    return _published_milestone_case(tmp_path, condition)


def _native_lease_case(flow, task, source, signed, observed, reference, tmp_path, condition, *, consumer="lease"):
    import json
    import os
    import sys
    import time
    import traceback
    from contextlib import nullcontext
    from threading import Event
    from types import SimpleNamespace
    from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.adapters.hosts.bridge.records import HostBinding
    from ai_task_manager.adapters.hosts.transports.process import HostProcess
    from ai_task_manager.adapters.linear.dependencies.source import Preparation
    from ai_task_manager.supervisor.authority import WorkAuthorities
    from ai_task_manager.supervisor.models import Command
    from tests.scheduling.test_authority_coordinator import AuthoredContinuity, AuthoredObservationGuard

    r = flow.r
    dispatch = flow.runtime.start(task)
    attempt = flow.runtime.admit(dispatch)
    task = r.core.get_task(r.handle, task.id)
    host = object.__new__(ManagedNativeHost)
    sid, scope = r.core.session_binding(r.handle)
    host.binding = HostBinding(scope, sid, attempt.id, attempt.worker_id, attempt.host,
                               "authored-principal", "a" * 64, r.now + 100)
    host.core = r.core
    host.journal = HostJournal(tmp_path / "native.sqlite", tmp_path / "native.key", clock=lambda: r.now)
    host.policy = SimpleNamespace(check=lambda: None)  # Authored mechanical ports; no model qualification.
    host._source_audit_boundary = lambda **kwargs: nullcontext()
    host._flush_source_observations = lambda: None
    host.stopped = Event()
    host.identity = None
    host.startup = SimpleNamespace(assert_stable=lambda: None,
                                   model_guard=AuthoredObservationGuard(host, lambda: r.now))
    registry = WorkAuthorities(r.core, source)
    r.core.scheduling.renewals = registry
    host.authority = registry.attach(r.handle, attempt, host, AuthoredContinuity(host, lambda: r.now))
    collections, local_preparations = [], []
    original_prepare = flow.workflow.prepare_merge_milestone

    def local_prepare(*args):
        local_preparations.append(True)
        return original_prepare(*args)

    flow.workflow.prepare_merge_milestone = local_prepare

    def original_observation(task_id, key, *, expected_version, decision_revision):
        # The initial mechanical collection publishes a fresh signed generation
        # with the same facts and unchanged original provider-window ceiling.
        assert task_id == task.id
        collections.append(key)
        current_view = r.core.scheduling.explain(r.handle, task_id)
        current_ref = source.receipts.published_reference(scope, task_id, current_view["source_revision"])
        source.receipts.verify_snapshot(current_ref)
        current = r.core.get_task(r.handle, task_id)
        graph = r.core.scheduling.explain(r.handle, task_id)["graph_version"]
        begun = source.receipts.begin(scope, current, graph, key)
        value = replace(observed, source_revision=begun["generation"], previous_graph_version=graph)
        reference = source.receipts.finish(scope, task_id, key, begun["context"], value,
                                           {"scope": "authored initial continuity collection"})
        return Preparation(task_id, expected_version, decision_revision, reference, key)

    source.prepare = original_observation
    host.authority.refresh()
    if consumer == "auth":
        return _identity_auth_case(flow, host, attempt, observed, reference, tmp_path, condition)
    if consumer == "delivery":
        return _stopped_delivery_case(flow, host, dispatch, attempt, observed, reference, tmp_path)
    if consumer in {"assignment", "admit"}:
        return _additional_lease_case(flow, host, dispatch, attempt, observed, reference, tmp_path, consumer)
    output = tmp_path / "native-output"
    output.mkdir(mode=0o700)
    command = Command((sys.executable, "-I", "-u", "-c",
                       "import time;print('{\"type\":\"fixture\"}',flush=True);"
                       "print('{\"type\":\"fixture\"}',flush=True);time.sleep(10)"),
                      tmp_path, {"PATH": os.defpath}, 15)
    process = HostProcess(command, output, deadline=time.time() + 15)
    process.boundary = lambda: host._current_lease(r.handle, attempt)
    stop = None
    try:
        process.start()
        first = process.read(1)
        expiry = flow.evidence.prepared[reference][0].expires_at
        r.now = observed.expires_at if condition == "stale" else expiry + 1
        if condition == "held":
            predecessor = r.core.get_task(r.handle, flow.task.id)
            proof = r.proof("human_decision", predecessor.id,
                            {"previous_revision": predecessor.decision_revision, "hold": True})
            r.core.decisions.record(r.handle, predecessor.id, predecessor.version, proof, r.key())
        elif condition == "target":
            from ai_task_manager.adapters.resources.git import git

            r.now = observed.observed_at
            target = git(flow.runtime.repo, "rev-parse", "refs/heads/integration")
            git(flow.runtime.repo, "update-ref", "refs/heads/integration", flow.old, target)
        failure = None
        try:
            second = process.read(1)
        except WorkError as error:
            second = None
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        restored = None
        if condition == "current":
            host.authority._prepare_milestones()
            restored = host._current_lease(r.handle, attempt)
        facts = {"scope": "Real native process read/Core/signed dependency/Git producer; policy/continuity authored",
                 "provider_expiry": observed.expires_at, "local_expiry": expiry, "clock": r.now,
                 "condition": condition, "collections": len(collections),
                 "local_preparations": len(local_preparations),
                 "first": first, "second": second, "failure": failure,
                 "restored_attempt": restored.id if restored else None, "attempt": attempt.id}
        (tmp_path / "lease-milestone-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert first == {"type": "fixture"} and len(collections) == 1, facts
        if condition == "current":
            assert restored.id == attempt.id and len(local_preparations) == 1, facts
            assert failure is None and second == {"type": "fixture"}, facts
        else:
            assert failure is not None and second is None, facts
    finally:
        stop = process.stop()
        assert stop.confirmed


@pytest.mark.parametrize("condition", ["current", "stale", "held", "target"])
def test_native_read_prepares_current_local_merge_milestone(tmp_path, condition):
    return _published_milestone_case(tmp_path, "lease:" + condition)


def _identity_auth_case(flow, host, attempt, observed, reference, tmp_path, condition):
    import json
    import traceback
    from contextlib import contextmanager
    from types import SimpleNamespace
    from ai_task_manager.adapters.hosts.bridge.auth_probe import ClaudeAuthProbe
    from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
    from ai_task_manager.adapters.hosts.bridge.identity_accounting import IdentityAuthPermits

    class Claimed(Exception):
        pass

    r, claims, protected = flow.r, [], [False]
    preparation = host.authority._prepare_milestones

    def prepare():
        assert not protected[0], "Local preparation entered the identity effect boundary"
        return preparation()

    host.authority._prepare_milestones = prepare
    parent = SimpleNamespace(reference="authored-existing-host-parent")

    @contextmanager
    def identity_boundary(invocation, *, not_after, cancelled=None):
        assert invocation is parent and not_after <= host.binding.deadline
        protected[0] = True
        try:
            yield
        finally:
            protected[0] = False

    parent.issuer = SimpleNamespace(identity_boundary=identity_boundary)

    def owner(policy, binding):
        assert policy is host.policy and binding is host.binding
        return r.core.get_attempt(r.handle, attempt.id)

    def claim(policy, binding, purpose, seconds, **context):
        assert protected[0] and purpose == "auth" and seconds == 15
        assert context["reference"] == "authored-identity-once"
        claims.append(context["reference"])
        raise Claimed()  # Exact finite accounting boundary, before any auth subprocess.

    accounting = SimpleNamespace(
        core=r.core, handle=r.handle, plan=SimpleNamespace(identity_refreshes=("authored-identity-once",), auth_seconds=15),
        _owner=owner, _claim=claim, _initialization=SimpleNamespace(native=parent),
    )
    permits = IdentityAuthPermits(accounting, host)
    observer = object.__new__(NativeIdentityObservations)
    observer.native = host
    observer.execution_not_after = host.binding.deadline
    observer.journal = host.journal
    from threading import Lock
    from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
    observer._refresh_lock = Lock()
    observer._revoked = False
    observer.prepare = lambda **kwargs: SimpleNamespace(observation=SimpleNamespace(expires_at=observed.expires_at + 10))
    observer._invocation = SimpleNamespace(native=parent)
    observer._probe = ClaudeAuthProbe(host.journal, permits)
    permits.bind(observer)
    host.expected_account = "authored-current-account"
    expiry = flow.evidence.prepared[reference][0].expires_at
    r.now = observed.expires_at if condition == "stale" else expiry + 1
    if condition == "held":
        predecessor = r.core.get_task(r.handle, flow.task.id)
        proof = r.proof("human_decision", predecessor.id,
                        {"previous_revision": predecessor.decision_revision, "hold": True})
        r.core.decisions.record(r.handle, predecessor.id, predecessor.version, proof, r.key())
    elif condition == "target":
        from ai_task_manager.adapters.resources.git import git

        r.now = observed.observed_at
        target = git(flow.runtime.repo, "rev-parse", "refs/heads/integration")
        git(flow.runtime.repo, "update-ref", "refs/heads/integration", flow.old, target)
    elif condition == "revoked":
        host.authority._stop.set()
    failure = None
    try:
        helper = IdentityRefresh(observer, tmp_path / "identity-once", 1)
        helper.ensure_current(minimum_remaining_seconds=21)
    except Claimed:
        pass
    except WorkError as error:
        failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    facts = {"scope": "Actual identity refresh/auth probe/permit/Core; native identity/policy and claim ports authored",
             "clock": r.now, "provider_expiry": observed.expires_at, "local_expiry": expiry,
             "failure": failure, "claim_attempts": len(claims), "condition": condition}
    (tmp_path / "identity-lease-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    if condition == "current":
        assert failure is None and len(claims) == 1, facts
    else:
        assert failure is not None and not claims, facts


@pytest.mark.parametrize("condition", ["current", "stale", "held", "target", "revoked"])
def test_identity_auth_prepares_current_local_merge_milestone(tmp_path, condition):
    return _published_milestone_case(tmp_path, "auth:" + condition)


def _additional_lease_case(flow, host, dispatch, attempt, observed, reference, tmp_path, consumer):
    import json
    import traceback
    from types import SimpleNamespace
    from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting
    from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
    from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway
    from ai_task_manager.adapters.hosts.bridge.workspace import Workspace

    class Claimed(Exception):
        pass

    r, claims = flow.r, []
    expiry = flow.evidence.prepared[reference][0].expires_at
    failure, succeeded = None, False
    if consumer == "assignment":
        def claim(*args, **kwargs):
            claims.append(True)
            raise Claimed()

        a = SimpleNamespace(_current=lambda: None, core=r.core, handle=r.handle,
                            plan=SimpleNamespace(assignments=("assignment",), assignment_seconds=15),
                            _owner_binding=host.binding, _policy=host.policy, _calls={"host": "authored-parent"},
                            _claim=claim)
        a.authorize = lambda ref, current: HostInvocationAccounting.authorize(a, ref, current)
        original = host.journal.intend

        def delayed(key, value):
            result = original(key, value)
            if key == "execution:assignment-after-intent":
                r.now = expiry + 1
            return result

        host.journal.intend = delayed
        executions = ReviewedExecutions(host.journal,
            {"assignment": ModelWork(attempt.task_id, attempt.host, "Never submitted.", "assignment")},
            a, SimpleNamespace())
        try:
            executions.execute(handle=r.handle, attempt=attempt, resources=dispatch.resources,
                               host=host, command_ref="assignment", operation_key="assignment-after-intent",
                               boundary=lambda: host._current_lease(r.handle, attempt), effect_boundary=lambda: None)
        except Claimed:
            succeeded = True
        except WorkError as error:
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    else:
        r.now = expiry + 1
        gateway = ManagedToolGateway(r.core, r.handle, dispatch, Workspace(dispatch.resources.worktree),
                                     flow.runtime.resources, host.journal, deadline=host.binding.deadline,
                                     authority=host.authority)
        try:
            gateway.admit()
            succeeded = True
        except WorkError as error:
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    facts = {"consumer": consumer, "clock": r.now, "provider_expiry": observed.expires_at,
             "local_expiry": expiry, "failure": failure, "claim_attempts": len(claims), "succeeded": succeeded}
    (tmp_path / "lease-consumer-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    assert failure is None and succeeded, facts


@pytest.mark.parametrize("consumer", ["assignment", "admit"])
def test_trusted_work_lease_handoff_prepares_local_milestone(tmp_path, consumer):
    return _published_milestone_case(tmp_path, consumer + ":current")


def _stopped_delivery_case(flow, host, dispatch, attempt, observed, reference, tmp_path):
    import json
    import traceback
    from ai_task_manager.installation.workflows.code_delivery import DeliveryResources
    from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies

    r = flow.r
    flow.runtime.supervisor.execute(r.handle, dispatch, flow.runtime.command(dispatch), operation_key=r.key())
    task = r.core.get_task(r.handle, attempt.task_id)
    lifecycle = DeliveryDependencies(r.core, r.handle, host.authority.registry.dependencies, host.binding.deadline)
    lifecycle.bind({"task_id": task.id, "attempt_id": attempt.id, "prose_digest": task.prose_digest,
                    "decision_revision": task.decision_revision})
    lifecycle._mode = "observing"
    try:
        lifecycle._refresh()
        expiry = flow.evidence.prepared[reference][0].expires_at
        r.now = expiry + 1
        resources = DeliveryResources(flow.runtime.resources, lambda: r.now, host.binding.deadline, lifecycle)
        failure = None
        try:
            resources.verify(dispatch.resources)
        except WorkError as error:
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        facts = {"clock": r.now, "local_expiry": expiry, "provider_expiry": observed.expires_at,
                 "attempt_state": r.core.get_attempt(r.handle, attempt.id).state, "failure": failure}
        (tmp_path / "stopped-delivery-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert failure is None, facts
    finally:
        lifecycle.close()


def test_stopped_delivery_reprepares_original_local_milestone(tmp_path):
    return _published_milestone_case(tmp_path, "delivery:current")
