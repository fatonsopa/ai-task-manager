"""Real ledger/Core composition with authored authority observations; no native calls."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.accounting import (
    AccountingPlan,
    HostInvocationAccounting,
)
from ai_task_manager.adapters.hosts.bridge.invocations import InitializationInvocation
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.domains.work.errors import AccessDenied, InvalidTransition
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.domains.helpers import Rig as CoreRig
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start as start_composed
from tests.test_build_control_policy_amendment import Rig as ControlRig


class AuthoredAdmission:
    """Mechanical port control, never a production native-conformance receipt."""

    def __init__(self):
        self.allow, self.authenticated = True, False

    def check_admission(self, plan):
        if not self.allow:
            raise AccessDenied("Authored admission refusal")

    def verify_authenticated(self, policy, binding, journal):
        if not self.authenticated:
            raise AccessDenied("Authored authentication refusal")

    def verify_policy(self, policy, binding, plan, controller):
        assert (policy.model, policy.effort) == (plan.model, plan.effort)


@pytest.fixture
def graph(tmp_path):
    budget = tmp_path / "budget"
    budget.mkdir()
    ledger = ControlRig(budget)
    ledger.now = datetime.now(UTC)
    ledger.amend()
    ledger.batch(count=20, seconds=120, budget="0")
    core = CoreRig(tmp_path / "core.sqlite")
    core.now = ledger.now.timestamp()
    core.capacity = CapacityPolicy("claude", 2, 2)
    core.core = core.new_core()
    task = core.queued()
    decision = core.decision(task, "In Progress")
    attempt = core.core.execution.reserve(
        core.handle,
        task.id,
        "claude",
        task.version,
        task.decision_revision,
        decision,
        "authored-accounting-reserve",
    )
    runtime = RuntimeJournal(tmp_path / "runtime.sqlite", clock=lambda: core.now)
    deadline = core.now + 100
    owner = runtime.claim(attempt, asdict(core.scope), deadline=deadline)
    session, scope = core.core.session_binding(core.handle)
    binding = HostBinding(
        scope,
        session,
        attempt.id,
        owner["worker_id"],
        "claude",
        "authored-principal",
        "a" * 64,
        deadline,
    )
    policy = SimpleNamespace(
        host="claude",
        model="claude-opus-5-5",
        effort="high",
        configuration_digest=binding.configuration_digest,
    )
    plan = AccountingPlan(
        "batch",
        "mechanical",
        task.id,
        "claude",
        policy.model,
        "high",
        "authored-included-billing-control",
        deadline,
        90,
        15,
        30,
        60,
    )
    journal = HostJournal(tmp_path / "host" / "journal.sqlite", tmp_path / "host" / "key")
    admission = AuthoredAdmission()
    adapter = HostInvocationAccounting(
        ledger.current, core.core, core.handle, runtime, journal, plan, admissions=admission
    )
    return SimpleNamespace(
        ledger=ledger,
        core=core,
        runtime=runtime,
        binding=binding,
        policy=policy,
        plan=plan,
        journal=journal,
        admission=admission,
        adapter=adapter,
        attempt=attempt,
    )


def start(permit):
    # Exercise the real ledger boundary without asserting a fabricated process.
    with permit.launch_boundary() as report:
        report(process_pid=None, process_start_identity=None)


def calls(g):
    return {
        row["metadata"]["invocation_binding"]["purpose"]: row
        for row in g.ledger.current.snapshot()["calls"]
    }


@pytest.mark.parametrize(
    "change",
    [
        {"host_seconds": 0},
        {"deadline": float("inf")},
        {"effort": ""},
        {"assignments": ("a", "a")},
        {"host": "fixture"},
    ],
)
def test_invalid_plan_refuses(graph, change):
    with pytest.raises(AccessDenied):
        replace(graph.plan, **change)


def test_missing_admission_refuses(graph):
    g = graph
    with pytest.raises(AccessDenied):
        HostInvocationAccounting(
            g.ledger.current,
            g.core.core,
            g.core.handle,
            g.runtime,
            g.journal,
            g.plan,
            admissions=None,
        )


def test_actual_admission_refusal_precedes_reservation(graph):
    g = graph
    g.admission.allow = False
    with pytest.raises(AccessDenied):
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert calls(g) == {}


@pytest.mark.parametrize("field", ["worker_id", "configuration_digest", "core_session"])
def test_core_runtime_scope_mismatch_refuses(graph, field):
    g = graph
    foreign = replace(g.binding, **{field: "foreign"})
    with pytest.raises(AccessDenied):
        g.adapter.authorize_initialization(g.policy, foreign)
    assert calls(g) == {}


def test_initialization_issues_exact_scoped_capabilities_and_retains_slot(graph):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    assert isinstance(pair, InitializationInvocation)
    assert pair.native.binding.purpose == "host"
    assert pair.relay.binding.purpose == "relay"
    assert pair.relay.binding.deadline <= pair.native.binding.deadline <= g.binding.deadline
    start(pair.native)
    start(pair.relay)
    g.adapter.record_initialization(
        pair, outcome="observed_native_startup", observation_ref="authored-observation", detail=None
    )
    assert calls(g)["host"]["status"] == "running"
    assert calls(g)["host"]["worker_state"] == "running"
    assert calls(g)["relay"]["metadata"]["parent_call_id"] == pair.native.reference


def test_auth_completion_uses_independent_stop_and_does_not_prove_subscription(graph):
    g = graph
    g.adapter.authorize_initialization(g.policy, g.binding)
    permit = g.adapter.authorize_auth(g.policy, g.binding)
    start(permit)
    g.adapter.record_auth(g.binding, {"outcome": "succeeded", "group": {"confirmed_stop": True}})
    assert calls(g)["auth"]["process_stopped"] is True
    with pytest.raises(AccessDenied, match="authentication"):
        g.adapter.authorize_neutral_start(g.policy, g.binding)
    assert "neutral" not in calls(g)


def test_neutral_completion_records_only_turn_and_retains_host_capacity(graph):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    start(pair.native)
    start(pair.relay)
    g.admission.authenticated = True
    permit = g.adapter.authorize_neutral_start(g.policy, g.binding)
    start(permit)
    g.adapter.record_neutral_start(
        permit, outcome="observed_turn", result=None, observation_ref="authored-result"
    )
    assert calls(g)["neutral"]["status"] == "succeeded"
    assert calls(g)["host"]["worker_state"] == "running"
    with pytest.raises(AccessDenied):
        g.adapter.record_neutral_start(
            permit, outcome="observed_turn", result=None, observation_ref="authored-result"
        )


def test_assignment_requires_actual_core_work_lease(graph):
    g = graph
    g.adapter.authorize_initialization(g.policy, g.binding)
    with pytest.raises(InvalidTransition):
        g.adapter.authorize("assignment", g.attempt)
    assert "assignment" not in calls(g)


def running(g):
    """Authored authority observations exercise genuine Core policy transitions."""
    r = g.core
    task = r.core.get_task(r.handle, g.plan.task_id)
    decision = r.decision(task, "In Progress")
    startup = r.proof(
        "worker_start",
        g.attempt.id,
        {
            "task_id": task.id,
            "host": "claude",
            "worker_id": g.binding.worker_id,
        },
    )
    r.core.execution.start(
        r.handle,
        g.attempt.id,
        task.version,
        task.decision_revision,
        startup,
        decision,
        "authored-accounting-start",
    )
    task = r.confirm(r.core.get_task(r.handle, task.id))
    resources = r.proof(
        "resources_ready",
        g.attempt.id,
        {
            "task_id": task.id,
            "host": "claude",
            "worker_id": g.binding.worker_id,
            "ready": True,
            "worktree_id": "authored-tree",
            "database_id": "authored-db",
        },
    )
    return r.core.execution.running(
        r.handle, g.attempt.id, task.version, task.decision_revision, resources, "authored-running"
    )


def test_assignment_uses_real_lease_and_duplicate_refuses(graph):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    start(pair.native)
    start(pair.relay)
    attempt = running(g)
    permit = g.adapter.authorize("assignment", attempt)
    start(permit)
    g.adapter.record(permit, outcome="observed_turn", detail=None)
    assert calls(g)["assignment"]["status"] == "succeeded"
    assert calls(g)["host"]["worker_state"] == "running"
    with pytest.raises(AccessDenied):
        g.adapter.authorize("assignment", attempt)


def test_missing_native_conformance_stays_refused(graph):
    g = graph
    with pytest.raises(AccessDenied, match="conformance"):
        g.adapter.require_native_conformance(g.policy, g.binding)
    assert calls(g) == {}


@pytest.mark.parametrize("confirmed", [False, True])
def test_stop_preserves_unknown_and_releases_only_confirmed_owner(graph, confirmed):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    start(pair.native)
    start(pair.relay)
    g.adapter.record_host_stop(g.binding, GroupStop(confirmed, GroupObservation(confirmed)))
    assert calls(g)["host"]["process_stopped"] is confirmed
    assert calls(g)["host"]["worker_state"] == ("released" if confirmed else "uncertain")
    if not confirmed:
        g.adapter.record_host_stop(g.binding, GroupStop(True, GroupObservation(True)))
        assert calls(g)["host"]["outcome"] == "unknown"
        assert calls(g)["host"]["process_stopped"] is True


def test_untyped_stop_cannot_release_host_capacity(graph):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    start(pair.native)
    with pytest.raises(AccessDenied):
        g.adapter.record_host_stop(g.binding, SimpleNamespace(confirmed=True))
    assert calls(g)["host"]["worker_state"] == "running"


def test_deadline_and_pointer_drift_refuse(graph):
    g = graph
    g.ledger.now = datetime.fromtimestamp(g.plan.deadline, UTC)
    with pytest.raises(AccessDenied):
        g.adapter.check_admission("claude")
    g.adapter.admissions = AuthoredAdmission()
    with pytest.raises(AccessDenied, match="composition"):
        g.adapter.check_admission("claude")


def test_foreign_permit_cannot_record_turn(graph):
    g = graph
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    with pytest.raises(AccessDenied):
        g.adapter.record(pair.native, outcome="observed_turn", detail=None)


def test_required_journal_failure_cannot_complete_invocation(graph, monkeypatch):
    g = graph
    g.adapter.authorize_initialization(g.policy, g.binding)
    g.admission.authenticated = True
    permit = g.adapter.authorize_neutral_start(g.policy, g.binding)
    start(permit)

    def failed(*args, **kwargs):
        raise OSError("Authored durable journal failure")

    monkeypatch.setattr(g.journal, "_append", failed)
    with pytest.raises(OSError):
        g.adapter.record(permit, outcome="observed_turn", detail=None)
    assert calls(g)["neutral"]["status"] == "running"


def test_real_producers_use_adapter_through_owned_fixture_host(composition):
    """Actual local process/tool loop; provider/native messages remain authored controls."""
    value = composition()
    runtime, task, _, workers, factory, provider, _ = value
    prior = factory.permits

    class FixtureAdmission:
        def check_admission(self, plan):
            prior.check_admission(plan.host)

        def verify_authenticated(self, policy, binding, journal):
            raise AssertionError("Codex fixture never runs a Claude neutral turn")

        def verify_policy(self, policy, binding, plan, controller):
            assert policy.model == "synthetic-no-model"

        def require_native_conformance(self, policy, binding, journal):
            return prior.require_native_conformance(policy, binding)

    plan = AccountingPlan(
        "batch",
        "composed",
        task.id,
        "codex",
        "synthetic-no-model",
        "high",
        "authored-billing-port-control",
        workers.deadline,
        28,
        15,
        10,
        20,
        ("synthetic-accounting",),
    )
    adapter = HostInvocationAccounting(
        prior.accounting.control,
        runtime.rig.core,
        runtime.rig.handle,
        runtime.journal,
        factory.journal,
        plan,
        admissions=FixtureAdmission(),
    )
    factory.permits = adapter
    workers.executions.accounting = adapter
    attempt = start_composed(value)
    assert attempt.state == "running" and provider.calls == [task.id]
    workers.execute_reviewed(runtime.rig.handle, attempt.id, "assignment", "adapter-assignment")
    assert runtime.rig.core.get_attempt(runtime.rig.handle, attempt.id).state == "done"
    assert factory.owned[attempt.id].process._closed
    recorded = prior.accounting.control.snapshot()["calls"]
    assert {c["metadata"]["invocation_binding"]["purpose"] for c in recorded} == {
        "host",
        "relay",
        "auth",
        "assignment",
    }
    assert all(c["status"] == "succeeded" and c["process_stopped"] for c in recorded)
