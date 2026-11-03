"""Actual local completed worker/check/Git flow with authored provider and work review."""

import json
import time
from pathlib import Path

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.application.merging.workflows import (
    CandidateChecks,
    MergeEvidence,
    MergePlan,
    MergeWorkflowService,
)
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.storage.checks import CheckStore
from ai_task_manager.storage.merging.journal import Journal
from tests.checks.fixtures import CHILD, FixtureExecutor, Guard, catalogs, discovery, sha, unit
from tests.runtime.helpers import ProviderFixture, RuntimeRig


class OutsideExecutor(FixtureExecutor):
    """Move owned completed-check evidence outside the immutable candidate input."""

    def __init__(self, evidence):
        super().__init__(None)
        self.evidence = evidence
        evidence.mkdir(mode=0o700)

    def execute(self, plan, group, names):
        result = super().execute(plan, group, names)
        paths = [self.root / "process-observations.json"]
        output = Path(self.records[-1]["output"])
        paths += [
            output,
            *(
                output.with_suffix(s)
                for s in (".intent.json", ".started.json", ".observation.json")
            ),
        ]
        mapping = []
        for source in paths:
            target = self.evidence / source.name
            raw = source.read_bytes()
            source.rename(target)
            assert target.read_bytes() == raw
            mapping.append({"from": str(source), "to": str(target), "sha256": sha(raw)})
        (self.evidence / (output.stem + "-mapping.json")).write_text(json.dumps(mapping))
        return result


class Flow:
    def __init__(
        self, root, *, target="integration", completed=True, human=True, conflict=False, value="4"
    ):
        setup_started = time.monotonic()
        root.mkdir(mode=0o700)
        self.root, self.runtime = root, RuntimeRig(root)
        self.r = self.runtime.rig
        self.dispatch = self.runtime.start(self.r.queued(code=True))
        self.runtime.admit(self.dispatch)
        self.pair = self.dispatch.resources
        self.tree = self.pair.worktree
        self.old = git(self.tree, "rev-parse", "HEAD")
        git(self.tree, "branch", target, self.old)
        program = (
            "from pathlib import Path; import subprocess; "
            + "; ".join(
                "Path("
                + repr(name)
                + ").parent.mkdir(parents=True,exist_ok=True); Path("
                + repr(name)
                + ").write_text("
                + repr(text)
                + ")"
                for name, text in [
                    ("src/a.txt", value),
                    ("tests/a.txt", "4"),
                    ("config/checks.json", "{}"),
                ]
            )
            + "; subprocess.run(['git','add','.'],check=True); "
            "subprocess.run(['git','commit','-qm','Owned worker change'],check=True)"
        )
        commit_started = time.monotonic()
        self.runtime.supervisor.execute(
            self.r.handle,
            self.dispatch,
            self.runtime.command(self.dispatch, program),
            operation_key=self.r.key(),
        )
        self.worker_commit_seconds = time.monotonic() - commit_started
        self.attempt = self.r.core.get_attempt(self.r.handle, self.dispatch.attempt_id)
        self.task = self.r.core.get_task(self.r.handle, self.attempt.task_id)
        assert self.attempt.state == "done" and self.dispatch.worker.process.returncode is not None
        self.new = git(self.tree, "rev-parse", "HEAD")
        if conflict:
            git(self.runtime.repo, "checkout", "-q", target)
            p = self.runtime.repo / "src/a.txt"
            p.parent.mkdir()
            p.write_text("conflicting target")
            git(self.runtime.repo, "add", ".")
            git(self.runtime.repo, "commit", "-qm", "Target conflict")
            git(self.runtime.repo, "checkout", "-q", "master")
            self.old = git(self.tree, "rev-parse", "refs/heads/" + target)
        self.evidence = MergeEvidence(clock=lambda: self.r.now)
        self.r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, self.runtime.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"provider_status", "decision", "human_decision", "work_unit_complete"}
                    ),
                    self.r.proofs,
                ),
                EvidenceRoute("merge-publication:", frozenset({"merge"}), self.evidence),
            ]
        )
        if completed:
            self.task = self.r.complete(self.task)
        home = root / "git-home"
        home.mkdir(mode=0o700)
        self.profile = GitProfile(
            root=root,
            executable=Executable.capture("/usr/bin/git", GitProfile.environment(home)),
            home=home,
        )
        tree_id, repo_id = (
            resource_alias("worktree", self.pair.worktree_id),
            resource_alias("repository", str(self.runtime.repo)),
        )
        record = self.profile.enroll(
            self.tree, worktree_id=tree_id, repository_id=repo_id, target_ref="refs/heads/" + target
        )
        directory = root / "candidates"
        directory.mkdir(mode=0o700)
        self.candidate_profile = CandidateProfile(self.profile, tree_id, repo_id, directory)
        self.binding = Binding(
            self.task.id,
            self.attempt.id,
            self.r.scope.principal,
            self.r.scope.logical_session,
            self.r.scope.project,
            self.task.prose_digest,
            sha(CHILD),
            tree_id,
            repo_id,
            resource_alias("database", self.pair.database_id),
            record["configuration_digest"],
            "candidate:workflow",
        )
        self.grant = CandidateGrant(
            self.binding,
            "refs/heads/managed/" + self.attempt.id,
            self.new,
            self.old,
            self.task.version,
            self.attempt.version,
            self.task.decision_revision,
            self.r.decision(self.task, "In Dev", actor="human" if human else "model"),
            "lead:workflow-fixture",
        )
        self.journal = Journal(root / "merge/journal.sqlite")
        self.candidate = CandidateService(
            self.journal,
            self.candidate_profile,
            CoreCandidateAuthority(self.r.core, self.r.handle, self.runtime.resources, self.pair),
            process_factory=CandidateProcesses,
        )
        self.catalogs = catalogs(root, project=self.r.scope.project)
        self.executor = OutsideExecutor(root / "check-evidence")
        self.guard = Guard(source=sha(CHILD))
        self.checks = MergeChecks(
            CheckStore(root / "checks/results.sqlite"),
            self.guard,
            self.executor,
            catalogs=self.catalogs,
        )
        self.units = (unit(value=value),)

        def discover(candidate):
            self.guard.root = self.executor.root = Path(candidate.path)
            return discovery(self.units)

        self.check_inputs = CandidateChecks(self.checks, discover)
        self.plan = MergePlan("installed-code-merge", self.grant, 2000)
        self.workflow = self.compose()
        self.app = ApplicationService(self.r.core, self.r.handle, workflows=self.workflow)
        self.target = target
        self.setup_seconds = time.monotonic() - setup_started

    def compose(self, **kwargs):
        return MergeWorkflowService(
            self.r.core,
            self.r.handle,
            self.plan,
            self.candidate,
            self.check_inputs,
            ProviderFixture(self.r),
            self.evidence,
            clock=lambda: self.r.now,
            **kwargs,
        )

    def run(self, key="public-merge"):
        return self.app.call(
            "workflow_run",
            {"task_id": self.task.id, "plan_ref": self.plan.id, "operation_key": key},
        )

    def resume(self, reference):
        return self.app.call("workflow_resume", {"workflow_ref": reference})

    def close(self):
        if hasattr(self, "workflow"):
            self.workflow.close()
        if hasattr(self, "candidate"):
            for ref in list(self.candidate.runner.live):
                self.candidate.runner.reconcile(ref)
            assert not self.candidate.runner.live
        self.runtime.close()
        assert all(w.process.returncode is not None for w in self.runtime.runtime.workers.values())
        (self.root / "workflow-cleanup.json").write_text(
            json.dumps(
                {
                    "all_workers_stopped": True,
                    "candidate_children": 0,
                    "publication_children": sum(
                        len(c.service.publisher._live) for c in self.workflow.consumers.values()
                    )
                    if hasattr(self, "workflow")
                    else 0,
                }
            )
        )
