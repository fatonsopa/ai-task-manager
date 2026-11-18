"""Real startup/Core/identity/source ordering; native transport is controlled.

The signed source uses an actual completed Git publication. This control does
not qualify a native CLI, model, account or live provider.
"""

import json
import os
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.startup import HostStartup
from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
from ai_task_manager.adapters.linear.dependencies.source import DependencySource, Preparation
from ai_task_manager.adapters.proofs.router import EvidenceRoute
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.domains.scheduling.models import Dependency, DependencySnapshot
from ai_task_manager.domains.scheduling.resources import AdmissionResources
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import CapacityPolicy, TaskProse
from ai_task_manager.installation.workflows.member_evidence import MemberMilestones
from ai_task_manager.supervisor.managed import ManagedWorkers
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.merge.workflows.helpers import Flow
from tests.runtime.helpers import ProviderFixture
from tests.scheduling.test_authority_coordinator import AuthoredContinuity, AuthoredObservationGuard


@pytest.mark.parametrize("condition", ["current"])
def test_managed_identity_waits_for_start_readback(tmp_path, monkeypatch, condition):
    flow = Flow(tmp_path / "owned")
    flow.check_inputs.select_retained = flow.check_inputs.discover
    delivered = flow.run()
    assert delivered["ok"] and delivered["data"]["state"] == "done", delivered
    r, runtime = flow.r, flow.runtime
    r.capacity = CapacityPolicy("claude", 2, 2)
    evidence = r.core.context.evidence
    r.core = r.new_core()
    r.core.context.evidence = evidence
    prose = TaskProse("Consume published merge", "Retain the original current dependency.", False)
    task = r.core.intake.submit(r.handle, prose, r.approval(prose), r.key())
    r.resource_sources.rows[task.id] = AdmissionResources(
        r.scope, task.id, task.prose_digest, task.decision_revision, "authored-start", (), ()
    )
    task = r.transition(r.confirm(task), "Todo", "enqueue")
    predecessor = r.core.get_task(r.handle, flow.task.id)
    ref = next(ref for ref, (fact, _) in flow.evidence.prepared.items() if fact.kind == "merge")
    code = SimpleNamespace(
        evidence=flow.evidence,
        issuer=None,
        prepare_merge_milestone=lambda reference, plan: flow.workflow.prepare_merge_milestone(
            delivered["data"]["workflow"], reference
        ),
    )
    member = SimpleNamespace(
        spec=SimpleNamespace(
            data={"scope": asdict(r.scope), "task_id": predecessor.id, "plan_ref": flow.plan.id},
            check=lambda: None,
        ),
        review_issuer=None,
        service=SimpleNamespace(
            workflows=SimpleNamespace(
                driver=SimpleNamespace(
                    code_delivery=code, plans=SimpleNamespace(get=lambda _: flow.plan)
                )
            )
        ),
    )
    receipts = DependencyReceipts(
        tmp_path / "deps.sqlite", tmp_path / "deps.key", clock=lambda: r.now
    )
    source = DependencySource(
        r.core,
        r.handle,
        SimpleNamespace(expected=SimpleNamespace(team_id="authored")),
        receipts,
        milestones=((task.prose_digest, predecessor.prose_digest),),
    )
    r.core.scheduling.sources = receipts

    def collect(task_id, key, *, expected_version=None, decision_revision=None):
        current = r.core.get_task(r.handle, task_id)
        graph = r.core.scheduling.explain(r.handle, task_id)["graph_version"]
        begun = receipts.begin(r.scope, current, graph, key)
        flow.workflow.prepare_merge_milestone(delivered["data"]["workflow"], ref)
        snapshot = DependencySnapshot(
            r.scope,
            task_id,
            current.prose_digest,
            current.issue_id,
            source.source_id,
            begun["generation"],
            r.now,
            r.now + 30,
            "complete",
            "normal",
            (Dependency(predecessor.id, "code_merged", evidence_refs=(("merge", ref),)),),
            graph,
        )
        reference = receipts.finish(
            r.scope, task_id, key, begun["context"], snapshot, {"authored": True}
        )
        return Preparation(task_id, current.version, current.decision_revision, reference, key)

    source._prepare = collect
    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key", clock=lambda: r.now)
    deadline, failures, phases, natives = r.now + 120, [], [], []

    class Native:
        install_identity_observations = ManagedNativeHost.install_identity_observations
        start_identity_observations = ManagedNativeHost.start_identity_observations
        prepare_local_milestones = ManagedNativeHost.prepare_local_milestones

        def __init__(self, binding):
            self.binding, self.journal, self.identity, self._identity_bound = (
                binding,
                journal,
                None,
                None,
            )
            self.authority = None
            self.stopped = Event()
            self.startup = SimpleNamespace(
                model_guard=AuthoredObservationGuard(self, lambda: r.now)
            )
            self.expires = r.now + 60
            observer = object.__new__(NativeIdentityObservations)
            observer.native = self
            observer.journal = journal
            observer.execution_not_after = deadline
            observer.prepare = lambda **_: SimpleNamespace(
                observation=SimpleNamespace(expires_at=self.expires)
            )

            def refresh(*_, **__):
                self.expires = r.now + 60
                return observer.prepare()

            observer.refresh = refresh
            observer.revoke = lambda: self.stopped.set()
            identity = IdentityRefresh(
                observer, tmp_path / "identity", 2, on_failure=lambda e: failures.append(str(e))
            )
            self.permits = SimpleNamespace(identity_observations=lambda _: identity)

        def initialize(self):
            return "authored-ready"

        def verify_active(self):
            if self.stopped.is_set():
                raise AccessDenied("Controlled native stopped")

        def admit(self, handle, dispatch, attempt):
            assert r.core.get_task(handle, task.id).sync_state == "confirmed"
            r.core.execution.validate_work_lease(handle, attempt.id, attempt.version)

        def stop(self):
            self.stopped.set()
            if self.identity:
                self.identity.close()
            return SimpleNamespace(confirmed=True)

    def continuity(native, *, identities=None):
        value = AuthoredContinuity(native, lambda: r.now)
        value.identities = identities
        return value

    monkeypatch.setattr(
        "ai_task_manager.adapters.hosts.bridge.continuity.NativeContinuity", continuity
    )

    def launch(binding, pair):
        value = Native(binding)
        natives.append(value)
        return value

    factory = SimpleNamespace(
        journal=journal,
        check_admission=lambda _: None,
        configuration_digest=lambda *_: "a" * 64,
        launch=launch,
        reconcile_stopped=lambda _: True,
    )

    class Startup:
        def check_prepared_host_ready(self, observed):
            b = natives[-1].binding
            assert observed.scope == b.scope and observed.attempt_id == b.attempt_id
            assert observed.worker_id == b.worker_id and observed.expires_at > r.now
            natives[-1].verify_active()
            return observed

        def verify_host_ready(self, _):
            b = natives[-1].binding
            return HostStartup(
                b.scope,
                b.attempt_id,
                b.worker_id,
                b.host,
                os.getpid(),
                "controlled-native",
                "controlled-session",
                b.principal_ref,
                b.configuration_digest,
                "authored-ready",
                r.now + 60,
            )

    proofs = RuntimeProofs(runtime.journal, tmp_path / "runtime.key", host_startups=Startup())
    routes = [route for route in r.core.context.evidence.routes if route.prefix != "runtime-proof:"]
    r.core.context.evidence = MemberMilestones(
        [*routes, EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs)], (member,)
    )
    source.refresh(task.id, "initial-source")
    provider = ProviderFixture(r)
    confirm = provider.confirm_pending

    def readback(handle, task_id):
        current = r.core.get_task(handle, task_id)
        assert current.sync_state == "pending" and current.execution_state == "starting"
        native = natives[-1]
        r.now += 25
        phases.append(
            {
                "pending": True,
                "collector_started": native.identity._thread is not None,
                "C_work_leases": sum(
                    e.task_id == task.id and e.kind == "work_lease_granted"
                    for e in r.core.notifications.list_events(handle)
                ),
            }
        )
        # Allow exactly one normal collector tick; no model or provider sleep.
        Event().wait(1.2)
        return confirm(handle, task_id)

    provider.confirm_pending = readback
    workers = ManagedWorkers(
        r.core,
        runtime.journal,
        runtime.resources,
        runtime.runtime,
        proofs,
        provider,
        factory,
        deadline=deadline,
        principal_ref="controlled-native",
        dependencies=source,
    )
    outcome = None
    try:
        try:
            workers.start(
                r.handle,
                task.id,
                "claude",
                expected_version=task.version,
                decision_revision=task.decision_revision,
                decision_ref=r.decision(task, "In Progress"),
                operation_key="controlled-start",
            )
            outcome = "running"
        except (AccessDenied, RuntimeUncertain) as error:
            outcome = str(error)
        facts = {
            "scope": (
                "Actual managed/Core/IdentityRefresh/signed source/Git; controlled native port"
            ),
            "condition": condition,
            "phases": phases,
            "identity_failures": failures,
            "outcome": outcome,
        }
        (tmp_path / "startup-transition-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert outcome == "running" and not failures, facts
        assert phases == [{"pending": True, "collector_started": False, "C_work_leases": 0}], facts
    finally:
        for native in natives:
            native.stop()
        for authority in workers._freshness.values():
            authority.close()
        runtime.close()
        flow.close()
