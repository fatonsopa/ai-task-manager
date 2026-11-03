"""Real completed Core worker and independent local candidate owner; no model acceptance."""

import hashlib
import json
import shlex

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.storage.merging.journal import Journal
from tests.runtime.helpers import RuntimeRig


class CandidateRig:
    def __init__(self, root, *, target="dev", diverge=False, deny=None):
        root.mkdir(mode=0o700)
        self.root = root
        self.runtime = RuntimeRig(root)
        self.r = self.runtime.rig
        self.dispatch = self.runtime.start()
        self.runtime.admit(self.dispatch)
        self.old = git(self.runtime.repo, "rev-parse", "HEAD")
        self.target = target
        git(self.runtime.repo, "branch", target, self.old)
        program = (
            "from pathlib import Path; import subprocess; "
            "Path('task.txt').write_text('worker committed change\\n'); "
            "subprocess.run(['git','add','task.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','Owned task commit'],check=True)"
        )
        command = self.runtime.command(self.dispatch, program)
        self.runtime.supervisor.execute(
            self.r.handle, self.dispatch, command, operation_key=self.r.key()
        )
        self.attempt = self.r.core.get_attempt(self.r.handle, self.dispatch.attempt_id)
        self.task = self.r.core.get_task(self.r.handle, self.attempt.task_id)
        assert self.attempt.state == "done" and self.dispatch.worker.process.returncode is not None
        self.pair = self.dispatch.resources
        self.tree = self.pair.worktree
        self.source_head = git(self.tree, "rev-parse", "HEAD")
        if diverge:
            git(self.runtime.repo, "checkout", "-q", target)
            (self.runtime.repo / "target.txt").write_text("target change\n")
            git(self.runtime.repo, "add", "target.txt")
            git(self.runtime.repo, "commit", "-qm", "Target commit")
        self.target_head = git(self.runtime.repo, "rev-parse", "refs/heads/" + target)
        hooks = self.runtime.repo / ".git/hooks"
        marker = root / "guard.log"
        for name in ("pre-commit", "commit-msg"):
            path = hooks / name
            code = (
                "#!/bin/sh\nprintf '%s\\n' "
                + shlex.quote(name)
                + " >> "
                + shlex.quote(str(marker))
                + "\n"
            )
            code += "exit " + ("1" if deny == name else "0") + "\n"
            path.write_text(code)
            path.chmod(0o700)
        home = root / "git-home"
        home.mkdir(mode=0o700)
        executable = Executable.capture("/usr/bin/git", GitProfile.environment(home))
        self.profile = GitProfile(root=root, executable=executable, home=home)
        tree_alias = resource_alias("worktree", self.pair.worktree_id)
        repo_alias = resource_alias("repository", str(self.runtime.repo))
        record = self.profile.enroll(
            self.tree,
            worktree_id=tree_alias,
            repository_id=repo_alias,
            target_ref="refs/heads/" + target,
        )
        self.binding = Binding(
            self.task.id,
            self.attempt.id,
            self.r.scope.principal,
            self.r.scope.logical_session,
            self.r.scope.project,
            self.task.prose_digest,
            hashlib.sha256(b"candidate runner provenance v1").hexdigest(),
            tree_alias,
            repo_alias,
            resource_alias("database", self.pair.database_id),
            record["configuration_digest"],
            self.r.key(),
        )
        decision = self.r.decision(self.task, "In Dev")
        self.grant = CandidateGrant(
            self.binding,
            "refs/heads/managed/" + self.attempt.id,
            self.source_head,
            self.target_head,
            self.task.version,
            self.attempt.version,
            self.task.decision_revision,
            decision,
            "lead-service:fixture",
        )
        directory = root / "candidates"
        directory.mkdir(mode=0o700)
        self.candidate_profile = CandidateProfile(
            self.profile,
            tree_alias,
            repo_alias,
            directory,
            required_hooks=("pre-commit", "commit-msg"),
        )
        self.journal = Journal(root / "candidate-state/journal.sqlite")
        self.authority = CoreCandidateAuthority(
            self.r.core, self.r.handle, self.runtime.resources, self.pair
        )
        self.service = CandidateService(
            self.journal, self.candidate_profile, self.authority, process_factory=CandidateProcesses
        )

    def close(self):
        for ref in list(self.service.grants) if hasattr(self, "service") else []:
            self.service.runner.reconcile(ref)
        self.runtime.close()
        data = {
            "all_workers_stopped": all(
                w.process.returncode is not None for w in self.runtime.runtime.workers.values()
            ),
            "live_candidate_processes": len(self.service.runner.live)
            if hasattr(self, "service")
            else 0,
        }
        (self.root / "candidate-cleanup.json").write_text(json.dumps(data))
        assert data["all_workers_stopped"] and not data["live_candidate_processes"]
