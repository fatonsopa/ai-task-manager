"""Production worker/accounting/identity graph over an owned authored CLI."""

import hashlib
import sys
import time
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.accounting import (
    AccountingPlan,
    HostInvocationAccounting,
)
from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
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


def test_v209_real_managed_consumer_uses_accounted_identity_and_joins_it(tmp_path, journal):
    root = tmp_path / "graph"
    root.mkdir(mode=0o700)
    runtime = RuntimeRig(root / "runtime")
    r = runtime.rig
    r.now = time.time()
    r.capacity = CapacityPolicy("claude", 2, 2)
    r.core = r.new_core()
    home = root / "home"
    home.mkdir(mode=0o700)
    (home / ".claude_roni").mkdir(mode=0o700)
    executable = root / "authored-claude"
    executable.write_text(f"#!{sys.executable}\n" + Path(__file__).with_name(
        "fixture_claude_bootstrap.py").read_text())
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

    budget = FixtureAccounting(root / "accounting")
    task = r.queued(code=False)
    deadline = r.now + 40
    plan = AccountingPlan(
        "batch", "managed-v2", task.id, "claude", "fixture-requested-model", "high",
        "authored-included-account-control", deadline, 35, 5, 8, 15,
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
    try:
        attempt = workers.start(
            r.handle, task.id, "claude", expected_version=task.version,
            decision_revision=task.decision_revision, decision_ref=r.decision(task, "In Progress"),
            operation_key="managed-v2-start",
        )
        native = factory.owned[attempt.id]
        assert workers._freshness[attempt.id].continuity.identities is native.identity.observations
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
