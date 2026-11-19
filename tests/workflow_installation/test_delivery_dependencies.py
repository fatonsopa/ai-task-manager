"""Existing Core observation API retains current stopped-worker dependency facts."""

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.worker_commits.helpers import WorkerFixture


@pytest.mark.parametrize(
    "case",
    ["owned_cancel", "foreign_cancel", "scope_fence", "human_hold", "revision", "expired", "graph_changed"],
)
def test_owned_publication_wait_close_preserves_current_and_real_refusal_fences(tmp_path, case):
    from contextlib import contextmanager
    from dataclasses import replace
    from threading import Event, get_ident
    from types import SimpleNamespace

    from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from ai_task_manager.adapters.linear.models import ExpectedBinding
    from ai_task_manager.domains.scheduling.models import Dependency, SchedulingBlocked
    from ai_task_manager.domains.work.errors import StaleVersion
    from tests.domains.helpers import Rig
    from tests.scheduling.helpers import declare

    root = (tmp_path / "owned").resolve()
    root.mkdir(mode=0o700)
    r = Rig(root / "core.sqlite")
    task, attempt = r.running()
    task, attempt = r.settle(task, attempt)
    row = {"task_id": task.id, "attempt_id": attempt.id,
           "prose_digest": task.prose_digest, "decision_revision": task.decision_revision}
    expected = ExpectedBinding(r.scope.principal, r.scope.workspace, "team", "TST", r.scope.project)
    source = DependencySource(
        r.core, r.handle, SimpleNamespace(expected=expected),
        DependencyReceipts(root / "dependencies.sqlite", root / "dependencies.key", clock=lambda: r.now),
    )
    authored = AuthoredDependencyCollection(r.core, r.handle)
    source.prepare = authored.prepare
    controller = DeliveryDependencies(r.core, r.handle, source, r.now + 120)
    boundary = source.publication_boundary
    entered, foreign_cancel = Event(), Event()
    main = get_ident()

    @contextmanager
    def observed(*args, **kwargs):
        if get_ident() != main:
            if case == "foreign_cancel":
                kwargs["cancelled"] = foreign_cancel
            entered.set()
        with boundary(*args, **kwargs):
            yield

    try:
        controller.start(row)
        before = r.core.scheduling.explain(r.handle, task.id)
        calls = authored.calls
        source.publication_boundary = observed
        with boundary(task.id):
            assert entered.wait(3), "The real collector must reach the held publication lock"
            if case == "foreign_cancel":
                foreign_cancel.set()
            elif case == "scope_fence":
                source.client.expected = replace(expected, team_id="foreign")
            elif case in {"human_hold", "revision"}:
                current = r.core.get_task(r.handle, task.id)
                proof = r.proof("human_decision", task.id, {
                    "previous_revision": current.decision_revision, "hold": case == "human_hold",
                })
                r.core.decisions.record(r.handle, task.id, current.version, proof, r.key())
            elif case == "expired":
                r.now = before["expires_at"]
            controller.close()
            assert not controller._thread.is_alive()
        source.client.expected = expected
        assert authored.calls == calls, "Cancelled collection must publish no observation"

        if case == "graph_changed":
            other = r.queued()
            declare(r.core, r.handle, r.current(task), r.core.scheduling.sources, r.now,
                    dependencies=(Dependency(other.id, "work_done"),))

        plan = SimpleNamespace(grant=SimpleNamespace(binding=SimpleNamespace(
            task_id=task.id, attempt_id=attempt.id,
        )))

        def publication_gate():
            controller.before_fence(plan)
            current = r.core.get_task(r.handle, task.id)
            current_attempt = r.core.get_attempt(r.handle, attempt.id)
            with r.core.execution.worker_commit_boundary(
                r.handle, task.id, attempt.id, current.version, current_attempt.version,
                current.decision_revision,
            ):
                pass

        if case == "owned_cancel":
            assert controller._error is None
            controller.check()
            controller.start(row)
            controller.check()
            assert r.core.scheduling.explain(r.handle, task.id)["source_revision"] > before["source_revision"]
            publication_gate()
        else:
            with pytest.raises((AccessDenied, SchedulingBlocked, StaleVersion)):
                publication_gate()
            if case in {"foreign_cancel", "scope_fence", "expired"}:
                error = controller._error
                assert error is not None
                with pytest.raises(AccessDenied):
                    controller.start(row)
                assert controller._error is error
    finally:
        controller.close()
        assert controller._thread is None or not controller._thread.is_alive()


@pytest.mark.parametrize("case", ["current", "expired", "foreign", "collection_expired"])
def test_delivery_dependency_collection_never_revives_expired_or_foreign_authority(tmp_path, case):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        before = r.core.scheduling.explain(r.handle, row["task_id"])
        if case == "expired":
            r.now = before["expires_at"]
        elif case == "foreign":
            row = dict(row, prose_digest="0" * 64)
        elif case == "collection_expired":
            original = source.prepare

            def delayed(*args, **kwargs):
                value = original(*args, **kwargs)
                r.now = before["expires_at"]
                return value

            source.prepare = delayed
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        if case == "current":
            task_before = r.core.get_task(r.handle, row["task_id"])
            controller.start(row)
            controller.check()
            after = r.core.scheduling.explain(r.handle, row["task_id"])
            assert after["graph_version"] == before["graph_version"]
            assert after["source_revision"] > before["source_revision"]
            assert r.core.get_task(r.handle, row["task_id"]) == task_before
        else:
            with pytest.raises(AccessDenied):
                controller.start(row)
            after = r.core.scheduling.explain(r.handle, row["task_id"])
            assert after["source_revision"] == before["source_revision"]
    finally:
        if controller is not None:
            controller.close()
            assert controller._thread is None or not controller._thread.is_alive()
        fixture.close()


def test_delivery_close_during_collection_publishes_no_new_observation(tmp_path):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        original = source.prepare
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        before = r.core.scheduling.explain(r.handle, row["task_id"])

        def close_during_read(*args, **kwargs):
            value = original(*args, **kwargs)
            controller.close()
            return value

        source.prepare = close_during_read
        controller.start(row)
        assert controller._thread is None
        assert (
            r.core.scheduling.explain(r.handle, row["task_id"])["source_revision"]
            == before["source_revision"]
        )
    finally:
        if controller is not None:
            controller.close()
        fixture.close()


def test_own_unit_progress_during_collection_keeps_exact_scope_and_graph(tmp_path):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        original = source.prepare
        before = r.core.scheduling.explain(r.handle, row["task_id"])
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter

        # Authored unit fact exercises version progress only, not semantic acceptance.
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute(
                    route.prefix,
                    route.kinds | {"work_unit_complete"}
                    if route.prefix == "fixture:"
                    else route.kinds,
                    route.verifier,
                )
                for route in r.core.context.evidence.routes
            ]
        )

        progressed = []

        def complete_during_read(*args, **kwargs):
            value = original(*args, **kwargs)
            if not progressed:
                progressed.append(True)
                r.complete(r.core.get_task(r.handle, row["task_id"]))
            return value

        source.prepare = complete_during_read
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        controller.start(row)
        controller.check()
        current = r.core.get_task(r.handle, row["task_id"])
        assert current.completed_work == current.required_work
        assert (
            r.core.scheduling.explain(r.handle, current.id)["graph_version"]
            == before["graph_version"]
        )
    finally:
        if controller is not None:
            controller.close()
        fixture.close()


@pytest.mark.parametrize(
    "case", ["current", "expired", "unavailable", "graph_changed", "task_changed", "deadline"]
)
def test_retained_installed_code_plan_checks_current_dependencies_before_candidate(tmp_path, case):
    from dataclasses import asdict, replace
    from types import SimpleNamespace

    from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
    from ai_task_manager.application.merging.candidates import (
        CandidateService,
        CoreCandidateAuthority,
    )
    from ai_task_manager.application.workflows.models import WorkflowPlan
    from ai_task_manager.domains.scheduling.models import Dependency
    from ai_task_manager.installation.workflows.code_delivery import InstalledCodeDelivery
    from ai_task_manager.storage.workflows import WorkflowStore
    from tests.merge.workflows.helpers import Flow

    flow = Flow(tmp_path / "retained", human=False)
    delivery = None
    try:
        r = flow.r
        source = AuthoredDependencyCollection(r.core, r.handle)
        store = WorkflowStore(flow.root / "workflow.sqlite")
        plan = WorkflowPlan(
            "installed-retained",
            "unused-author",
            "fixture",
            flow.old,
            ("src/a.txt", "tests/a.txt", "config/checks.json"),
            ("src/a.txt",),
            flow.plan.deadline,
        )
        reference = store.create(r.scope, flow.task, plan, "original-code-key")
        owner = "retained-code-controller"
        store.claim(reference, r.scope, owner)
        commit = {
            "state": "committed",
            "branch": "managed/" + flow.attempt.id,
            "commit": flow.new,
            "attempt_id": flow.attempt.id,
            "task_id": flow.task.id,
        }
        registration = SimpleNamespace(
            fingerprint="authored-code-registration",
            check=lambda: None,
            require=lambda *_: None,
        )
        runtime = SimpleNamespace(pair=lambda attempt: flow.pair)
        delivery = InstalledCodeDelivery(
            r.core,
            r.handle,
            store,
            runtime,
            flow.runtime.resources,
            registration,
            SimpleNamespace(deadline=flow.plan.deadline),
            None,
            None,
            None,
            None,
            (),
            source,
        )
        # Restore the actual merge consumer with the SAME newly constructed guard.
        # The authored registration/runtime ports cannot launch another author.
        flow.candidate = CandidateService(
            flow.journal,
            flow.candidate_profile,
            CoreCandidateAuthority(r.core, r.handle, delivery.resources, flow.pair),
            process_factory=CandidateProcesses,
        )
        component = flow.compose(publication_lifecycle=delivery.freshness)
        component.successor_merge = None
        flow.workflow = component
        delivery.components[reference] = component
        selected = {
            "registration": registration.fingerprint,
            "commit": commit,
            "plan": asdict(component.original_plan),
        }
        for name, payload, result in (
            ("worker-commit", {"attempt_id": flow.attempt.id}, commit),
            ("code-plan", selected, {"installed": True}),
        ):
            store.intend(reference, owner, name, payload)
            store.finish(reference, owner, name, result, attempt_id=flow.attempt.id)
        row = store.read(reference, r.scope)
        saved_steps = row["steps"]
        before = r.core.scheduling.explain(r.handle, flow.task.id)
        if case == "expired":
            r.now = before["expires_at"]
        elif case == "deadline":
            r.now = flow.plan.deadline
        elif case == "unavailable":
            source.fail = True
        elif case == "task_changed":
            row = dict(row, prose_digest="0" * 64)
        elif case == "graph_changed":
            other = r.queued()
            original = source.prepare

            def changed(*args, **kwargs):
                prepared = original(*args, **kwargs)
                snapshot = r.core.scheduling.sources.receipts[prepared.snapshot_ref]
                r.core.scheduling.sources.receipts[prepared.snapshot_ref] = replace(
                    snapshot, dependencies=(Dependency(other.id, "work_done"),)
                )
                return prepared

            source.prepare = changed
        if case == "current":
            result = delivery.advance(row, plan, owner)
            assert result["state"] == "done"
            assert source.calls > 0
            assert r.core.get_task(r.handle, flow.task.id).delivered_once
            assert len(flow.executor.records) == 1
        else:
            with pytest.raises(AccessDenied):
                delivery.advance(row, plan, owner)
            assert not flow.executor.records
            assert not tuple((flow.root / "candidates").iterdir())
            assert not r.core.get_task(r.handle, flow.task.id).delivered_once
        after = store.read(reference, r.scope)
        for name in ("worker-commit", "code-plan"):
            assert after["steps"][name] == saved_steps[name]
        assert len(flow.runtime.runtime.workers) == 1
    finally:
        if delivery is not None:
            delivery.close()
        flow.close()
