"""Concrete local Core/check/Git/resource chain; fixture provider/startup facts only."""

import json
from pathlib import Path

from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.adapters.git.publication import LocalGit
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.application.merging.authority import LeaseGrant, resource_alias
from ai_task_manager.application.merging.composition import CheckedMerge
from ai_task_manager.domains.checks.models import Scope
from ai_task_manager.domains.merging.contracts import Binding, Publication
from ai_task_manager.storage.checks import CheckStore
from ai_task_manager.storage.merging.journal import Journal
from tests.checks.fixtures import (
    CHILD,
    FixtureExecutor,
    Guard,
    catalogs,
    discovery,
    sha,
    unit,
    write_inputs,
)
from tests.runtime.helpers import RuntimeRig


class Chain:
    def __init__(self, root, *, run=True, value="4"):
        root.mkdir(mode=0o700)
        self.runtime = RuntimeRig(root)
        self.r = self.runtime.rig
        self.dispatch = self.runtime.start()
        self.attempt = self.runtime.admit(self.dispatch)
        self.task = self.r.core.get_task(self.r.handle, self.attempt.task_id)
        self.pair = self.dispatch.resources
        self.tree = self.pair.worktree
        self.old = self.runtime.resources.git.inspect(self.tree, "managed/" + self.attempt.id)[
            "head"
        ]
        from ai_task_manager.adapters.resources.git import git

        git(self.tree, "branch", "integration", self.old)
        self.units = (unit(value=value),)
        write_inputs(self.tree, self.units)
        (self.tree / "src/a.txt").write_text(value)
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-qm", "Authored checked fixture change")
        self.new = git(self.tree, "rev-parse", "HEAD")
        self.source = sha(CHILD)  # Runner provenance, separate from candidate/unit inputs.
        self.catalogs = catalogs(root, project=self.r.scope.project)
        self.store = CheckStore(root / "checks" / "results.sqlite")
        self.executor = FixtureExecutor(self.tree)
        self.checks = MergeChecks(
            self.store,
            Guard(source=self.source, root=self.tree),
            self.executor,
            catalogs=self.catalogs,
        )
        _, scope = self.r.core.session_binding(self.r.handle)
        self.scope = Scope(scope.principal, scope.project, scope.logical_session, self.task.id)
        from ai_task_manager.domains.checks.models import Availability, Change

        self.plan_ref = self.checks.prepare(
            self.scope,
            self.attempt.id,
            self.source,
            Change(
                Availability.KNOWN,
                ("src/a.txt", "tests/a.txt", "config/checks.json"),
                self.old,
                self.new,
            ),
            discovery(self.units),
            "actual-base-plan",
        )
        if run:
            self.checks.run(self.plan_ref, self.scope)
            # The authored executor retains output in its input tree. Preserve
            # those exact completed-process artifacts outside the Git candidate;
            # publication must still reject every unexpected untracked input.
            evidence = root / "check-process-evidence"
            evidence.mkdir(mode=0o700)
            relocated = []
            paths = [self.tree / "process-observations.json"]
            for record in self.executor.records:
                output = Path(record["output"])
                paths.extend(
                    [
                        output,
                        *(
                            output.with_suffix(suffix)
                            for suffix in (".intent.json", ".started.json", ".observation.json")
                        ),
                    ]
                )
            for source in paths:
                target = evidence / source.name
                value = source.read_bytes()
                source.rename(target)
                assert target.read_bytes() == value
                relocated.append({"from": str(source), "to": str(target), "sha256": sha(value)})
            (evidence / "relocations.json").write_text(json.dumps(relocated, indent=2))
        home = root / "git-home"
        home.mkdir(mode=0o700)
        executable = Executable.capture("/usr/bin/git", GitProfile.environment(home))
        self.profile = GitProfile(root=root, executable=executable, home=home)
        tree_alias = resource_alias("worktree", self.attempt.worktree_id)
        repo_alias = resource_alias("repository", str(self.runtime.repo))
        record = self.profile.enroll(
            self.tree,
            worktree_id=tree_alias,
            repository_id=repo_alias,
            target_ref="refs/heads/integration",
        )
        self.binding = Binding(
            self.task.id,
            self.attempt.id,
            scope.principal,
            scope.logical_session,
            scope.project,
            self.task.prose_digest,
            self.source,
            tree_alias,
            repo_alias,
            resource_alias("database", self.attempt.database_id),
            record["configuration_digest"],
            "fixture-publication",
        )
        self.request = Publication(
            "refs/heads/managed/" + self.attempt.id, self.old, self.new, self.new, "actual-range"
        )
        self.grant = LeaseGrant(
            self.binding,
            self.request,
            self.task.version,
            self.attempt.version,
            self.task.decision_revision,
        )
        self.journal = Journal(root / "merge" / "journal.sqlite")
        self.consumer = self.compose(self.grant)
        self.closed = False

    def compose(self, grant):
        return CheckedMerge(
            self.r.core,
            self.r.handle,
            grant,
            self.runtime.resources,
            self.pair,
            self.profile,
            self.journal,
            self.checks,
            self.plan_ref,
            publisher_factory=LocalGit,
        )

    def publish(self):
        ref = self.consumer.registration["ref"]
        return self.consumer.publish(self.journal.current(ref)["version"], "publish")

    def target(self):
        return self.profile.reference(self.tree, "refs/heads/integration")

    def close(self):
        if getattr(self, "closed", False):
            return
        try:
            worker = self.runtime.runtime.workers[self.attempt.id]
            if worker.process.returncode is None:
                outcome = self.runtime.supervisor.stop(
                    self.r.handle, self.dispatch, operation_key=self.r.key(), reason="fixture close"
                )
                state = {"quiescent": outcome.quiescent}
            else:
                observations = self.runtime.journal.observations(self.attempt.id, "runtime_outcome")
                last = observations[-1] if observations else {}
                state = {
                    "already_stopped": True,
                    "prior_quiescence": last.get("quiescent"),
                    "prior_outcome_ref": last.get("id"),
                }
            state["attempt"] = self.r.core.get_attempt(self.r.handle, self.attempt.id).state
        except Exception as error:
            self.runtime.close()
            state = {
                "settlement_pending": type(error).__name__,
                "reason": "Record the failed Core settlement without inventing a settled record",
            }
        self.runtime.close()
        state["all_workers_stopped"] = all(
            worker.process.returncode is not None
            for worker in self.runtime.runtime.workers.values()
        )
        (self.runtime.path / "composition-cleanup.json").write_text(json.dumps(state, indent=2))
        assert state["all_workers_stopped"]
        self.closed = True
