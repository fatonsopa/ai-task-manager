"""Local integer-project and actual Git fixtures; no host/provider acceptance."""

import hashlib
import os
import subprocess
import sys
from dataclasses import replace

from ai_task_manager.adapters.git.commits import WorkerGit
from ai_task_manager.adapters.workflows.repository import RepositoryInspector
from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort
from ai_task_manager.application.commits import WorkerCommits
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
from ai_task_manager.storage.workflows import WorkflowStore
from tests.runtime.helpers import ProviderFixture, RuntimeRig


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class FixedChecks:
    def __init__(self, source, *, fail=False):
        self.source = hashlib.sha256(source.read_bytes()).hexdigest()
        self.fail, self.calls, self.results = fail, 0, set()

    def execute_registered(self, reference, pair, boundary, effect_boundary, operation_key):
        assert reference == "verify" and pair.database.exists()
        assert hashlib.sha256((pair.worktree / "verify.py").read_bytes()).hexdigest() == self.source
        boundary()
        self.calls += 1
        completed = subprocess.run(
            [sys.executable, "-I", "verify.py", str(pair.database)],
            cwd=pair.worktree,
            capture_output=True,
            timeout=5,
        )
        if self.fail or completed.returncode:
            raise RuntimeError("Authored independent check failed")
        self.results.add((pair.attempt_id, reference))

    def verified_result(self, attempt_id, reference, *, operation_key=None):
        assert (attempt_id, reference) in self.results
        return {"verified": True}


class WorkerFixture:
    def __init__(
        self,
        path,
        *,
        faults=None,
        git_faults=None,
        check_failure=False,
        program=None,
        resource_locks=None,
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.rr = RuntimeRig(path)
        self.rig = r = self.rr.rig
        source = self.rr.repo / "verify.py"
        source.write_text(
            "import sqlite3, sys\n"
            "from pathlib import Path\n"
            "namespace = {}\n"
            "exec(compile(Path('result.py').read_text(), 'result.py', 'exec'), namespace)\n"
            "assert namespace['total']([2, 5, -1]) == 6\n"
            "with sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True) as db:\n"
            "    assert len(db.execute('SELECT attempt_id FROM fixture_owner').fetchall()) == 1\n"
        )
        git(self.rr.repo, "add", "verify.py")
        git(self.rr.repo, "commit", "-qm", "Independent integer project")
        self.base = git(self.rr.repo, "rev-parse", "HEAD")
        self.task = r.queued()
        self.commands = LocalCommands(
            {
                "write": (
                    (
                        sys.executable,
                        "-I",
                        "-c",
                        program
                        or "from pathlib import Path;"
                        "Path('result.py').write_text('def total(values):\\n"
                        "    return sum(values)\\n')",
                    ),
                    {"PATH": os.defpath},
                    2,
                )
            }
        )
        self.runtime = LocalSupervisorPort(self.rr.supervisor, self.commands)
        self.store = WorkflowStore(path / "workflow.sqlite")
        self.plan = WorkflowPlan(
            "integer",
            "write",
            "fixture",
            self.base,
            ("result.py",),
            ("result.py",),
            r.now + 120,
            commit_checks=("verify",),
            resource_locks=resource_locks,
        )
        self.plans = WorkflowPlans((self.plan,))
        self.checks = FixedChecks(source, fail=check_failure)
        self.git = WorkerGit(self.rr.journal, faults=git_faults)
        self.commits = WorkerCommits(
            r.core, r.handle, self.runtime, self.rr.journal, self.git, checks=self.checks
        )
        self.driver = WorkflowDriver(
            r.core,
            r.handle,
            self.store,
            self.plans,
            runtime=self.runtime,
            repository=RepositoryInspector(self.runtime),
            provider=ProviderFixture(r),
            commits=self.commits,
            decisions=lambda scope, task, target, action: r.decision(task, target),
            faults=faults,
        )
        self.ref = self.driver.begin(self.task.id, self.plan.id, "integer-workflow")

    def execute_only(self, reference=None):
        reference = reference or self.ref
        old = self.driver.plans

        # Original saved plan stays identical; bounded driver interruption follows execute.
        class StopAfterExecution:
            def hit(self, stage):
                if stage == "after:execute":
                    raise RuntimeError("Lost execution reply")

        self.driver.faults = StopAfterExecution()
        self.driver.run(reference)
        self.driver.faults = None
        owner = "fixture-reconcile"
        self.store.claim(reference, self.rig.scope, owner)
        row = self.store.read(reference, self.rig.scope)
        task = self.rig.core.get_task(self.rig.handle, row["task_id"])
        result = self.driver._reconcile(row, task, "execute", row["steps"]["execute"])
        self.store.finish(reference, owner, "execute", result, attempt_id=result["attempt_id"])
        self.store.release(reference, owner)
        self.driver.plans = old
        return self.store.read(reference, self.rig.scope)

    def close(self):
        self.rr.close()

    def replan(self, **kwargs):
        self.driver.plans = WorkflowPlans((replace(self.plan, **kwargs),))
