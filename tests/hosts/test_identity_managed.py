"""Production worker/accounting/identity graph over an owned authored CLI."""

import hashlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.accounting import (
    AccountingPlan,
    HostInvocationAccounting,
)
from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
from ai_task_manager.adapters.hosts.bridge.policy import (
    NativePolicy,
    NativeProfile,
    clean_environment,
    policy_digest,
)
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.hosts.accounting_fixtures import FixtureAccounting
from tests.hosts.test_composition import UnusedCommands
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.runtime.helpers import ProviderFixture, RuntimeRig


class FixtureAdmission:
    """No conformance assertion: checks the actual authored auth-status trace."""

    def __init__(self, executable, account):
        self.executable, self.account = executable, account

    def check_admission(self, plan):
        assert plan.host == "claude" and plan.model == "fixture-requested-model"

    def verify_policy(self, policy, binding, plan, controller):
        assert policy.executable == self.executable
        assert policy.model == plan.model and policy.effort == plan.effort

    def verify_authenticated(self, policy, binding, journal):
        for row in reversed(journal._history("trace")):
            data = row["payload"]
            if data.get("configuration_digest") == binding.configuration_digest:
                if data.get("auth_method") == "claude.ai":
                    journal._read(row["ref"])
                    if data.get("account_hash") == self.account:
                        return
                    break
        raise AccessDenied("No actual authored account-status observation")


@pytest.mark.parametrize("boundary", ["original", "headroom", "expired", "account"])
def test_v209_real_managed_consumer_uses_accounted_identity_and_joins_it(
    tmp_path, journal, monkeypatch, boundary
):
    root = tmp_path / "graph"
    root.mkdir(mode=0o700)
    runtime = RuntimeRig(root / "runtime")
    r = runtime.rig
    r.now = time.time()
    if boundary != "original":
        journal.clock = lambda: r.now
    r.capacity = CapacityPolicy("claude", 2, 2)
    r.core = r.new_core()
    home = root / "home"
    home.mkdir(mode=0o700)
    (home / ".claude_roni").mkdir(mode=0o700)
    executable = root / "authored-claude"
    program = Path(__file__).with_name("fixture_claude_bootstrap.py").read_text()
    if boundary == "account":
        program = program.replace('MODE = "normal"', 'MODE = "account-change"')
    executable.write_text(f"#!{sys.executable}\n" + program)
    executable.chmod(0o700)
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    account = claude_account_hash(
        {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
    )

    def policy(host, pair):
        env = clean_environment(home, claude_profile=home / ".claude_roni")
        return NativePolicy(
            host, executable, sha, env, pair.worktree,
            policy_digest(host, sha, env, pair.worktree, {},
                          model="fixture-requested-model", effort="high"), (),
            tuple("mcp__aitm__" + name for name in (
                "read_file", "write_file", "run_command", "acknowledge_event")),
            "fixture-requested-model", "high",
        )

    budget = FixtureAccounting(
        root / "accounting", maximum_seconds=60 if boundary == "original" else 180
    )
    if boundary != "original":
        budget.control.clock = lambda: datetime.fromtimestamp(r.now, UTC)
    task = r.queued(code=False)
    deadline = r.now + (40 if boundary == "original" else 180)
    plan = AccountingPlan(
        "batch", "managed-v2", task.id, "claude", "fixture-requested-model", "high",
        "authored-included-account-control", deadline,
        35 if boundary == "original" else 150, 5, 8, 15,
        identity_refreshes=("identity-1",),
    )
    accounting = HostInvocationAccounting(
        budget.control, r.core, r.handle, runtime.journal, journal, plan,
        admissions=FixtureAdmission(executable, account),
    )
    factory = ManagedHostFactory(
        r.core, runtime.resources, journal, root / "native", policy, accounting,
        {"claude": account},
        model_profiles={"claude": NativeProfile("fixture-requested-model", "high")},
    )
    proofs = RuntimeProofs(
        runtime.journal, root / "runtime-proof.key", host_startups=factory.startup_verifier,
    )
    r.core.context.evidence = EvidenceRouter([
        EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
        EvidenceRoute("fixture:", frozenset({"provider_status", "decision", "human_decision"}),
                      r.proofs),
    ])
    workers = ManagedWorkers(
        r.core, runtime.journal, runtime.resources, runtime.runtime, proofs,
        ProviderFixture(r), factory, deadline=deadline, principal_ref="fixture-principal",
        dependencies=AuthoredDependencyCollection(r.core, r.handle),
    )
    factory.commands = workers
    if boundary != "original":
        launch = factory.launch

        def aged_startup(binding, pair):
            native = launch(binding, pair)
            initialize = native.initialize

            def initialized():
                result = initialize()
                # Reproduce the normal path with 18s of original identity
                # remaining: valid to the old 15s helper, short of work's21s.
                age = 61 if boundary == "expired" else 42
                while age:
                    step = min(10, age)
                    r.now += step
                    # Preserve ordinary startup dependency refresh while the
                    # unchanged account observation ages across its boundary.
                    workers._startup_freshness[binding.attempt_id].refresh()
                    age -= step
                if boundary == "account":
                    (pair.worktree / "account-switch").write_text("authored switch")
                return result

            native.initialize = initialized
            return native

        monkeypatch.setattr(factory, "launch", aged_startup)
    try:
        if boundary in {"expired", "account"}:
            with pytest.raises(AccessDenied):
                workers.start(
                    r.handle, task.id, "claude", expected_version=task.version,
                    decision_revision=task.decision_revision,
                    decision_ref=r.decision(task, "In Progress"),
                    operation_key="managed-v2-start",
                )
            assert not workers.dispatches
            assert all(
                event.kind != "work_lease_granted"
                for event in r.core.notifications.list_events(r.handle)
            )
            return
        attempt = workers.start(
            r.handle, task.id, "claude", expected_version=task.version,
            decision_revision=task.decision_revision, decision_ref=r.decision(task, "In Progress"),
            operation_key="managed-v2-start",
        )
        native = factory.owned[attempt.id]
        assert workers._freshness[attempt.id].continuity.identities is native.identity.observations
        if boundary == "headroom":
            refreshed = [c for c in budget.control.snapshot()["calls"]
                         if c["call_id"].endswith(":identity-1")]
            assert len(refreshed) == 1 and refreshed[0]["process_stopped"] is True
            assert native.identity.observations.prepare().observation.expires_at > r.now + 38
            assert workers._freshness[attempt.id]._failure is None
            workers.executions = ReviewedExecutions(
                journal,
                {"assignment": ModelWork(task.id, "claude", "Authored normal work", "assignment")},
                accounting,
                UnusedCommands(),
            )
            workers.execute_reviewed(r.handle, attempt.id, "assignment", "normal-headroom-work")
            assert r.core.get_attempt(r.handle, attempt.id).state == "done"
            assert native.process._closed and not native.identity._thread.is_alive()
            assert not workers.failure_diagnostics
            return
        previous = native.identity.observations.prepare()
        native.identity.observations.refresh(native.directory / "manual-fresh-auth")
        previous.check()  # Overlapping proof remains valid through its own expiry.
        r.core.execution.validate_work_lease(r.handle, attempt.id, attempt.version)
        refreshed = [c for c in budget.control.snapshot()["calls"]
                     if c["call_id"].endswith(":identity-1")]
        assert len(refreshed) == 1 and refreshed[0]["process_stopped"] is True
        assert refreshed[0]["metadata"]["parent_call_id"] == (
            native.process.invocation.native.reference
        )
        stopped = workers.stop(r.handle, attempt.id, operation_key="managed-v2-stop")
        assert stopped.state == "paused" and not native.identity._thread.is_alive()
    finally:
        for native in factory.owned.values():
            if native.process is not None and not native.process._closed:
                assert native.stop().confirmed
        runtime.close()
