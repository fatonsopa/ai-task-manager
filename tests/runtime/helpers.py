"""Actual local runtime fixtures; no provider credential or model execution."""

import os
import subprocess
import sys

import pytest

from ai_task_manager.adapters.process.runtime import ProcessRuntime
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.accounting import MechanicalCommands
from ai_task_manager.supervisor.dispatch import WorkerSupervisor
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.models import Command, RuntimeRefused
from tests.domains.helpers import Rig


class ProviderFixture:
    """Trusted deterministic provider oracle, not live Linear evidence."""

    def __init__(self, rig):
        self.rig = rig

    def confirm_pending(self, handle, task_id):
        return self.rig.confirm(self.rig.core.get_task(handle, task_id), handle=handle)


class FixtureReleasePolicy:
    """Trusted test configuration bound to this fixture's physical root and repository."""

    def __init__(self, root, repository):
        self.root = root.resolve()
        self.repository = repository.resolve()
        self.root_identity = self.root.stat().st_ino

    def authorize(self, core, handle, attempt, resources):
        if (
            self.root.stat().st_ino != self.root_identity
            or not resources["worktree"]["path"].startswith(str(self.root / "trees") + "/")
            or not resources["database"]["path"].startswith(str(self.root / "databases") + "/")
            or resources["worktree"]["common"] != str(self.repository / ".git")
            or resources["database"]["owner"] != attempt.id
        ):
            raise RuntimeRefused("Fixture release policy does not own these resources")


class RuntimeRig:
    def __init__(self, path):
        self.path = path
        self.rig = Rig(path / "core.sqlite")
        self.rig.capacity = CapacityPolicy("fixture", 2, 2)
        self.rig.core = self.rig.new_core()
        self.rig.key = lambda: f"fixture:{self._next()}"
        self.journal = RuntimeJournal(path / "runtime.sqlite", clock=lambda: self.rig.now)
        self.proofs = RuntimeProofs(
            self.journal, path / "controller-only" / "runtime.key", fixture_hosts={"fixture"}
        )
        self.rig.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, self.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset({"provider_status", "decision", "human_decision"}),
                    self.rig.proofs,
                ),
            ]
        )
        self.repo = path / "source"
        self.repo.mkdir()
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.invalid"),
            ("commit", "--allow-empty", "-qm", "Fixture baseline"),
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.resources = ResourceManager(
            self.journal, path / "owned", self.repo, metadata_paths=(self.rig.db.path,)
        )
        self.resources.release_policy = FixtureReleasePolicy(self.path / "owned", self.repo)
        self.runtime = ProcessRuntime(self.journal, path / "outputs")
        self.supervisor = WorkerSupervisor(
            self.rig.core,
            self.journal,
            self.resources,
            self.runtime,
            self.proofs,
            ProviderFixture(self.rig),
            MechanicalCommands(),
        )
        self.dispatches = []

    def _next(self):
        self.rig.serial += 1
        return self.rig.serial

    def start(self, task=None, *, key=None, host="fixture"):
        r = self.rig
        task = task or r.queued()
        decision = r.decision(task, "In Progress")
        dispatch = self.supervisor.start(
            r.handle,
            task.id,
            host,
            expected_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=decision,
            operation_key=key or r.key(),
            deadline=r.now + 120,
        )
        self.dispatches.append(dispatch)
        return dispatch

    def admit(self, dispatch):
        return self.supervisor.admit(self.rig.handle, dispatch, operation_key=self.rig.key())

    def command(self, dispatch, program="print('fixture complete')", *, timeout=2):
        command = Command(
            (sys.executable, "-c", program),
            dispatch.resources.worktree,
            {"PATH": os.defpath},
            timeout,
        )
        self.supervisor.accounting = MechanicalCommands([command])
        return command

    def hold(self, dispatch):
        r = self.rig
        attempt = r.core.get_attempt(r.handle, dispatch.attempt_id)
        task = r.core.get_task(r.handle, attempt.task_id)
        ref = r.proof(
            "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": True}
        )
        return r.core.decisions.record(r.handle, task.id, task.version, ref, r.key())

    def close(self):
        for worker in self.runtime.workers.values():
            if worker.process.returncode is None:
                self.runtime.stop(worker, reason="fixture cleanup")


@pytest.fixture
def rr(tmp_path):
    fixture = RuntimeRig(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()
