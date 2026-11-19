"""Sealed selection and resource drift controls independent of semantic model acceptance."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows.code_delivery import (
    ResourceExecutor,
    ResourceReads,
    ResourceReviews,
)
from ai_task_manager.installation.workflows.code_registration import CodeRegistration
from ai_task_manager.installation.workflows.registry import NativeRegistry
from tests.domains.helpers import Rig


def test_sealed_code_registration_refuses_scope_policy_and_port_replacement(tmp_path):
    tmp_path.chmod(0o700)
    r = Rig(tmp_path / "core.sqlite")
    task = r.task()
    path = tmp_path / "policy.json"
    data = {
        "reference": "code",
        "scope": asdict(r.scope),
        "task_id": task.id,
        "prose_digest": task.prose_digest,
        "target_ref": "refs/heads/integration",
        "require_human_acceptance": False,
    }
    write_private(path, data)
    registration = CodeRegistration(path, lambda context: None)
    registry = NativeRegistry(code_registrations=(registration,))
    assert registry.require_code("code") is registration
    registration.require(r.core, r.handle, task)
    with pytest.raises(AccessDenied):
        registration.require(r.core, r.handle, replace(task, code_bearing=False))
    foreign = r.session(replace(r.scope, logical_session="foreign"))
    with pytest.raises(AccessDenied):
        registration.require(r.core, foreign, task)
    registration.prepare = lambda context: "replacement"
    with pytest.raises(AccessDenied):
        registration.check()
    with pytest.raises(AccessDenied):
        NativeRegistry(code_registrations=(registration, registration))
    extra = tmp_path / "extra.json"
    write_private(extra, dict(data, passed=True))
    with pytest.raises(AccessDenied):
        CodeRegistration(extra, lambda context: None)


@pytest.mark.parametrize("consumer", ["review", "read", "proof", "execute"])
def test_waiting_consumer_revalidates_before_returning_evidence(consumer):
    class Resources:
        valid = True
        calls = 0

        def verify(self, pair):
            self.calls += 1
            if not self.valid:
                raise EvidenceRequired("Authored resource replacement while waiting")

    resources, pair = Resources(), object()

    def wait(*args, **kwargs):
        resources.valid = False
        return "unusable-evidence"

    source = SimpleNamespace(
        resolve=wait,
        execute=wait,
        store=object(),
        ensure_parent=lambda: None,
        read=wait,
        proofs=wait,
    )
    with pytest.raises(EvidenceRequired, match="replacement"):
        if consumer == "review":
            ResourceReviews(source, resources, pair).resolve(object())
        elif consumer == "execute":
            ResourceExecutor(source, resources, pair).execute(object(), "group", ("unit",))
        else:
            reads = ResourceReads(source, resources, pair)
            method = reads.read if consumer == "read" else reads.proofs
            method(object(), object(), after_wait=lambda: None)
    assert resources.calls >= 2


def test_resource_reads_preserve_original_finite_wait_recheck_callback():
    order = []
    resources = SimpleNamespace(verify=lambda pair: order.append("resource"))

    class Reads:
        store = object()

        def read(self, *args, after_wait):
            order.append("wait")
            after_wait()
            return "record"

    wrapper = ResourceReads(Reads(), resources, object())
    assert wrapper.read("ref", "scope", after_wait=lambda: order.append("source")) == "record"
    assert order == ["resource", "wait", "resource", "source", "resource", "resource"]


def test_delivery_resource_budget_refuses_expiry_during_wait_before_consumption():
    from contextlib import contextmanager

    from ai_task_manager.installation.workflows.code_delivery import DeliveryResources

    now = [1.0]

    class Resources:
        git = object()

        @contextmanager
        def use(self, pair):
            yield

        def verify(self, pair):
            return "physical"

    resources = DeliveryResources(Resources(), lambda: now[0], 2.0)
    with resources.use(object()):
        assert resources.verify(object()) == "physical"
    with pytest.raises(AccessDenied, match="deadline"):
        with resources.use(object()):
            now[0] = 2.0
            resources.verify(object())


@pytest.mark.parametrize("case", ["delay", "pending", "hold", "revision"])
def test_unit_proof_refresh_survives_prior_unit_and_resource_wait(tmp_path, monkeypatch, case):
    """Actual advance/issuer/Core; local worker and signed authored review, no model."""
    import inspect
    import json
    from collections import Counter

    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.adapters.workflows.committed import CommittedRepositoryInspector
    from ai_task_manager.adapters.workflow_reviews.code_worker_artifacts import CodeWorkerArtifacts
    from ai_task_manager.application.workflows.models import WorkflowPlan
    from ai_task_manager.domains.work.errors import WorkError
    from ai_task_manager.installation.workflows.code_delivery import InstalledCodeDelivery
    from ai_task_manager.storage.workflows import WorkflowStore
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.workflow_reviews.helpers import ReviewRig

    original_queued = Rig.queued
    with monkeypatch.context() as patch:
        patch.setattr(Rig, "queued", lambda self, **kw: original_queued(
            self, **{**kw, "units": ("first", "second")}
        ))
        rig = ReviewRig(tmp_path / "owned", code=True)
    delivery = None
    try:
        r, pair = rig.r, rig.dispatch.resources
        plan = WorkflowPlan("units", "assignment", rig.attempt.host,
                            git(pair.worktree, "rev-parse", "HEAD^"),
                            ("report.txt",), ("report.txt",), r.now + 100,
                            resource_locks=())
        store = WorkflowStore(rig.root / "outer.sqlite")
        ref = store.create(r.scope, rig.task, plan, "original-units")
        owner = "unit-control"
        store.claim(ref, r.scope, owner)
        receipt = {"task_id": rig.task.id, "attempt_id": rig.attempt.id,
                   "state": "committed", "commit": rig.saved["source_head"],
                   "tree": git(pair.worktree, "rev-parse", "HEAD^{tree}")}
        store.intend(ref, owner, "worker-commit", {})
        store.finish(ref, owner, "worker-commit", receipt, attempt_id=rig.attempt.id)
        policy = rig.root / "code-policy.json"
        write_private(policy, {"reference": "units", "scope": asdict(r.scope),
                              "task_id": rig.task.id, "prose_digest": rig.task.prose_digest,
                              "target_ref": "refs/heads/integration",
                              "require_human_acceptance": False})
        registration = CodeRegistration(policy, lambda *_: pytest.fail("Merge preparation"))
        criteria = {rig.task.id: (("request", "The artifact addresses the declared request"),)}
        rig.plan = plan
        inspector = CommittedRepositoryInspector(rig.view, lambda _: receipt)
        rig.saved = inspector.inspect(rig.task, rig.attempt, plan)
        store.intend(ref, owner, "verify", {"attempt_id": rig.attempt.id})
        store.finish(ref, owner, "verify", rig.saved)
        rig.artifacts = CodeWorkerArtifacts(r.core, r.handle, store, inspector,
                                           rig.view, {rig.task.id: plan}, criteria)
        rig.install(suffix="two-units")
        delivery = InstalledCodeDelivery(
            r.core, r.handle, store, rig.view, rig.runtime.resources, registration,
            SimpleNamespace(deadline=plan.deadline), rig.source, rig.issuer_key,
            lambda *_: None, None, criteria, AuthoredDependencyCollection(r.core, r.handle),
        )
        captured_before = rig.binding.capture_path.read_bytes()

        class UnitsReached(Exception):
            pass

        class Interrupted(Exception):
            pass

        monkeypatch.setattr(delivery, "_component", lambda *a: (_ for _ in ()).throw(UnitsReached()))
        actual_complete = r.core.execution.complete_unit
        calls, interrupted, resource_waits = Counter(), [False], []

        def complete(handle, task_id, unit, *args):
            if case == "pending" and unit == "second" and not interrupted[0]:
                interrupted[0] = True
                raise Interrupted()
            value = actual_complete(handle, task_id, unit, *args)
            calls[unit] += 1
            if unit == "first":
                r.now += 6  # Completed prior-unit processing exceeds the five-second witness.
                if case in {"hold", "revision"}:
                    decision = r.proof("human_decision", value.id,
                                       {"previous_revision": value.decision_revision,
                                        "hold": case == "hold"})
                    r.core.decisions.record(handle, value.id, value.version, decision, r.key())
            return value

        resource_type = type(delivery.resources)
        verify = resource_type.verify

        def delayed_resource_check(resources, pair):
            # Exercise the existing resource check immediately before unit consumption.
            if (resources is delivery.resources
                    and inspect.currentframe().f_back.f_code.co_name == "complete"
                    and calls["first"]):
                r.now += 6
                resource_waits.append(r.now)
            return verify(resources, pair)

        monkeypatch.setattr(r.core.execution, "complete_unit", complete)
        monkeypatch.setattr(resource_type, "verify", delayed_resource_check)
        pending = None
        if case == "pending":
            with pytest.raises(Interrupted):
                delivery.advance(store.read(ref, r.scope), plan, owner)
            pending = store.read(ref, r.scope)["steps"]["unit:second"]["payload"]
        if case in {"hold", "revision"}:
            with pytest.raises(WorkError):
                delivery.advance(store.read(ref, r.scope), plan, owner)
            assert r.core.get_task(r.handle, rig.task.id).completed_work == ("first",)
        else:
            with pytest.raises(UnitsReached):
                delivery.advance(store.read(ref, r.scope), plan, owner)
            assert r.core.get_task(r.handle, rig.task.id).completed_work == ("first", "second")
            assert calls == {"first": 1, "second": 1} and resource_waits
            current = store.read(ref, r.scope)
            if pending:
                assert current["steps"]["unit:second"]["payload"] == pending
            with pytest.raises(UnitsReached):
                delivery.advance(current, plan, owner)
            assert calls == {"first": 1, "second": 1}
        assert rig.binding.capture_path.read_bytes() == captured_before
        assert len(rig.runtime.runtime.workers) == 1
        (tmp_path / "unit-proof-observations.json").write_text(json.dumps({
            "case": case, "completed_units": dict(calls), "resource_waits": resource_waits,
            "clock": r.now, "pending_intent": pending,
            "review_context": rig.inspection.digest, "author_processes": 1,
            "semantic_capture_unchanged": True,
        }, indent=2))
    finally:
        if delivery is not None:
            prepared = delivery.issuer._prepared if delivery.issuer is not None else {}
            (tmp_path / "unit-proof-last-boundary.json").write_text(json.dumps({
                "clock": rig.r.now,
                "prepared": [{"reference": ref, "expires_at": value[2],
                              "unit": json.loads(value[0])["payload"]["unit"]}
                             for ref, value in prepared.items()],
                "completed_work": rig.r.core.get_task(rig.r.handle, rig.task.id).completed_work,
                "advance_source": inspect.getfile(InstalledCodeDelivery),
            }, indent=2))
            delivery.freshness.close()
        rig.close()
