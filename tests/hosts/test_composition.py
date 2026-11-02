"""Production object graph with authored native/provider controls, not host acceptance."""

import hashlib
import sys
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.codex_home import CodexHomeBoundary
from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
from ai_task_manager.adapters.hosts.bridge.policy import (
    NativePolicy,
    clean_environment,
    policy_digest,
)
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.managed import ManagedWorkers
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.fixture_cli import ACCOUNT
from tests.runtime.helpers import ProviderFixture, RuntimeRig


class AuthoredPermits:
    """Exact trusted-port signatures; admits only the synthetic test configuration."""

    def __init__(self, deny=False):
        self.deny, self.outcomes = deny, []

    def check_admission(self, host):
        if self.deny or host != "codex":
            raise AccessDenied("Synthetic conformance control refused")

    def authorize_initialization(self, policy, binding):
        assert policy.executable.name == "synthetic-cli"
        return "synthetic-initialization"

    def record_initialization(self, permit, *, outcome, observation_ref, detail):
        self.outcomes.append(("initialization", outcome))

    def authorize_auth(self, policy, binding):
        assert policy.executable.name == "synthetic-cli"

    def record_auth(self, binding, completion):
        self.outcomes.append(("authored-status-control", completion["outcome"]))

    def require_native_conformance(self, policy, binding):
        assert policy.executable.name == "synthetic-cli"
        return "authored-fixture-control-not-native-conformance"

    def authorize_neutral_start(self, policy, binding):
        raise AssertionError("No neutral model turn is allowed in this fixture")

    def record_neutral_start(self, permit, *, outcome, result, observation_ref):
        raise AssertionError("No actual neutral model result exists")

    def record_host_stop(self, binding, stop):
        self.outcomes.append(("stop", stop.confirmed))


class AuthoredAccounting:
    def __init__(self):
        self.outcomes = []

    def authorize(self, accounting_ref, attempt):
        assert accounting_ref == "synthetic-accounting"
        return "synthetic-accounting"

    def record(self, permit, *, outcome, detail):
        self.outcomes.append(outcome)


class UnusedCommands:
    def execute_registered(self, reference, resources, boundary, effect_boundary, operation_key):
        raise AssertionError("No command was registered for this constructor fixture")

    def reconcile_stopped(self, attempt_id):
        return True


class AuthoredProvider(ProviderFixture):
    def __init__(self, rig, fail=False):
        super().__init__(rig)
        self.fail, self.calls = fail, []

    def confirm_pending(self, handle, task_id):
        task = self.rig.core.get_task(handle, task_id)
        assert task.execution_state == "starting"
        self.calls.append(task_id)
        if self.fail:
            raise RuntimeUncertain("Synthetic provider readback unavailable")
        return super().confirm_pending(handle, task_id)


@pytest.fixture
def composition(tmp_path, journal):
    instances = []

    def create(*, deny=False, provider_fail=False, early_tool=False):
        root = tmp_path / ("graph-" + str(len(instances)))
        root.mkdir(mode=0o700)
        r = RuntimeRig(root / "runtime")
        r.rig.now = journal.clock()
        fixture_proofs = r.rig.proofs
        r.rig.capacity = CapacityPolicy("codex", 2, 2)
        r.rig.core = r.rig.new_core()
        home, profile = root / "home", root / "private-codex"
        home.mkdir(mode=0o700)
        profile.mkdir(mode=0o700)
        for name in ("auth.json", "config.toml"):
            (profile / name).write_text("{}" if name == "auth.json" else "")
            (profile / name).chmod(0o600)
        settings = CodexSettings(CodexHomeBoundary(profile), "synthetic-no-model", "high")
        source = Path(__file__).with_name("fixture_cli.py").read_text()
        source = source.replace("ADVERSARIAL_INIT = False", f"ADVERSARIAL_INIT = {early_tool!r}")
        executable = root / "synthetic-cli"
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o700)
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

        def policy(host, pair):
            env = clean_environment(home, codex_home=profile)
            fingerprint = policy_digest(
                host,
                executable_hash,
                env,
                pair.worktree,
                {"codex_settings": settings.fingerprint},
                model=settings.model,
                effort=settings.effort,
            )
            return NativePolicy(
                host,
                executable,
                executable_hash,
                env,
                pair.worktree,
                fingerprint,
                (),
                model=settings.model,
                effort=settings.effort,
                codex_settings=settings,
            )

        permits, accounting = AuthoredPermits(deny), AuthoredAccounting()
        factory = ManagedHostFactory(
            r.rig.core,
            r.resources,
            journal,
            root / "native",
            policy,
            permits,
            {"codex": digest(ACCOUNT)},
        )
        proofs = RuntimeProofs(
            r.journal,
            root / "runtime-proof.key",
            host_startups=factory.startup_verifier,
        )
        r.rig.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset({"provider_status", "decision", "human_decision"}),
                    fixture_proofs,
                ),
            ]
        )
        provider = AuthoredProvider(r.rig, provider_fail)
        task = r.rig.queued()
        decision = r.rig.decision(task, "In Progress")
        executions = ReviewedExecutions(
            journal,
            {
                "assignment": ModelWork(
                    task.id, "codex", "Synthetic owned edit.", "synthetic-accounting"
                )
            },
            accounting,
            UnusedCommands(),
        )
        workers = ManagedWorkers(
            r.rig.core,
            r.journal,
            r.resources,
            r.runtime,
            proofs,
            provider,
            factory,
            deadline=r.journal.clock() + 30,
            principal_ref="fixture-principal",
            executions=executions,
        )
        factory.commands = workers
        value = (r, task, decision, workers, factory, provider, accounting)
        instances.append(value)
        return value

    yield create
    for r, _, _, workers, factory, _, _ in instances:
        for attempt_id, native in factory.owned.items():
            if native.process is not None and not native.process._closed:
                native.stop()
        r.close()


def start(value):
    r, task, decision, workers, _, _, _ = value
    return workers.start(
        r.rig.handle,
        task.id,
        "codex",
        expected_version=task.version,
        decision_revision=task.decision_revision,
        decision_ref=decision,
        operation_key="synthetic-start",
    )


def test_real_production_graph_admits_owned_edit_and_settles_actual_groups(composition):
    value = composition()
    r, task, _, workers, factory, provider, accounting = value
    attempt = start(value)
    assert attempt.state == "running" and provider.calls == [task.id]
    assert not (workers.dispatches[attempt.id].resources.worktree / "owned.py").exists()
    workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "synthetic-assignment")
    assert (
        workers.dispatches[attempt.id].resources.worktree / "owned.py"
    ).read_text() == "answer = 42\n"
    assert r.rig.core.get_attempt(r.rig.handle, attempt.id).state == "done"
    assert factory.owned[attempt.id].process._closed
    assert accounting.outcomes == ["observed_turn"]


@pytest.mark.parametrize("case", ["provider_fail", "early_tool"])
def test_composed_refusal_has_no_assigned_work_and_stops_actual_native_group(composition, case):
    value = composition(**{case: True})
    r, task, _, _, factory, _, accounting = value
    with pytest.raises((AccessDenied, RuntimeUncertain)):
        start(value)
    current = r.rig.core.get_task(r.rig.handle, task.id)
    assert current.execution_state == "failed" and current.observed_status == "Todo"
    assert not accounting.outcomes
    for native in factory.owned.values():
        assert native.process._closed
        assert not (native.pair.worktree / "owned.py").exists()


def test_real_factory_guard_refuses_before_any_attempt_or_process(composition):
    value = composition(deny=True)
    r, _, _, _, factory, provider, _ = value
    with pytest.raises(AccessDenied, match="conformance"):
        start(value)
    with r.rig.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert not factory.owned and not provider.calls
