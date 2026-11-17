"""Actual local Git/database/child continuation with authored task authority."""

import os
import secrets
import sqlite3
import sys
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.adapters.workflows.repository import RepositoryInspector
from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
from ai_task_manager.application.workflows.recovery import SuccessorAuthorization, admit_successor
from ai_task_manager.domains.work.models import Evidence, SessionHandle
from ai_task_manager.storage.workflows import WorkflowStore
from tests.runtime.helpers import ProviderFixture, RuntimeRig


@pytest.mark.parametrize("code, prior_lineage", [(False, False), (True, False), (True, True)])
def test_stopped_active_successor_uses_original_dirty_files_and_database(
    tmp_path, code, prior_lineage, monkeypatch
):
    rr = RuntimeRig(tmp_path)
    r = rr.rig
    try:
        if code:
            from tests.worker_commits.helpers import git

            r.now = time.time()
            for name, value in [
                ("src/a.txt", "0"), ("tests/a.txt", "4"), ("config/checks.json", "{}")
            ]:
                path = rr.repo / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(value)
            verify = rr.repo / "verify.py"
            verify.write_text(
                "from pathlib import Path\n"
                "assert Path('result.txt').read_text() == 'retain this partial work\\n'\n"
            )
            git(rr.repo, "add", ".")
            git(rr.repo, "commit", "-qm", "Owned continuation check")
        task = r.queued(code=code, units=("first", "remaining"))
        original = rr.start(task)
        rr.admit(original)
        task = r.core.get_task(r.handle, task.id)
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"provider_status", "decision", "human_decision", "work_unit_complete"}
                    ),
                    r.proofs,
                ),
            ]
        )
        r.core.execution.complete_unit(
            r.handle,
            task.id,
            "first",
            task.version,
            task.decision_revision,
            r.proof(
                "work_unit_complete", task.id, {"prose_digest": task.prose_digest, "unit": "first"}
            ),
            r.key(),
        )
        pair = original.resources
        (pair.worktree / "partial.txt").write_text("retain this partial work\n")
        with sqlite3.connect(pair.database) as db:
            db.execute("CREATE TABLE partial(value TEXT)")
            db.execute("INSERT INTO partial VALUES('retained')")
        rr.supervisor.stop(r.handle, original, operation_key=r.key())
        task = r.core.get_task(r.handle, task.id)
        assert task.execution_state == "paused"
        base = rr.resources.git.inspect(pair.worktree, "managed/" + original.attempt_id)["head"]
        old = WorkflowPlan(
            "old",
            "continue",
            "fixture",
            base,
            ("partial.txt", "result.txt", "src/a.txt") if code else ("partial.txt", "result.txt"),
            ("result.txt",),
            r.now - 1,
            commit_checks=("verify",) if code else (),
        )
        fresh = replace(old, id="fresh", deadline=r.now + 100, max_steps=2)
        store = WorkflowStore(tmp_path / "workflow.sqlite")
        predecessor = store.create(r.scope, task, old, "original-key", "old-runtime")
        store.claim(predecessor, r.scope, "old-owner")
        store.intend(predecessor, "old-owner", "start", {"original": True})
        store.finish(
            predecessor,
            "old-owner",
            "start",
            {"attempt_id": original.attempt_id},
            attempt_id=original.attempt_id,
        )
        store.release(predecessor, "old-owner")
        old_row = store.read(predecessor, r.scope)
        if prior_lineage:
            store.lineage(
                task.id, original.attempt_id,
                rr.resources.git.inspect(pair.worktree, "managed/" + original.attempt_id),
            )
        with store.transaction() as conn:
            original_lineage = [tuple(row) for row in conn.execute("SELECT * FROM lineage")]
        decision = r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=r.now + 120,
        )
        a = SuccessorAuthorization(
            r.key(),
            r.scope,
            predecessor,
            task.id,
            task.version,
            0,
            fresh.id,
            fresh.digest,
            "fresh-key",
            r.now + 100,
            decision,
            ("remaining",),
        )
        r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", r.scope, task.id, asdict(a), a.expires_at
        )
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "provider_status",
                            "decision",
                            "human_decision",
                            "workflow_successor",
                            "work_unit_complete",
                        }
                    ),
                    r.proofs,
                ),
            ]
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        r.core.revoke_session(r.handle)
        commands = LocalCommands(
            {
                "continue": (
                    (
                        sys.executable,
                        "-I",
                        "-c",
                        "from pathlib import Path;"
                        "Path('result.txt').write_text(Path('partial.txt').read_text());"
                        + ("Path('src/a.txt').write_text('4')" if code else "pass"),
                    ),
                    {"PATH": os.defpath},
                    2,
                )
            }
        )
        runtime = LocalSupervisorPort(rr.supervisor, commands)
        boundary = SimpleNamespace(
            check=lambda *_: None, check_locked=lambda *_: None, boundary=lambda *_: nullcontext()
        )
        ref = admit_successor(
            r.core, r.handle, handle, store, a, fresh, runtime.binding(task, fresh), boundary
        )
        rr.resources.successor_sources[task.id] = original.attempt_id
        driver = WorkflowDriver(
            r.core,
            handle,
            store,
            WorkflowPlans((fresh,)),
            runtime=runtime,
            repository=RepositoryInspector(runtime),
            provider=ProviderFixture(r),
            decisions=lambda scope, task, target, action: r.decision(task, target),
        )
        result = driver.run(ref)
        assert result["state"] == "checkpoint", result
        current = store.read(ref, r.scope)
        new_attempt = r.core.get_attempt(handle, current["attempt_id"])
        assert new_attempt.id != original.attempt_id and new_attempt.state == "done"
        inherited = runtime.pair(new_attempt.id)
        assert inherited.worktree == pair.worktree and inherited.database == pair.database
        assert (inherited.worktree / "result.txt").read_text() == "retain this partial work\n"
        with sqlite3.connect(pair.database) as db:
            assert db.execute("SELECT value FROM partial").fetchall() == [("retained",)]
            assert db.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                (original.attempt_id,)
            ]
        assert r.core.get_task(handle, task.id).completed_work == ("first",)
        if not code:
            from ai_task_manager.adapters.resources.consumers import consumer_boundary
            from ai_task_manager.supervisor.models import RuntimeRefused

            fence = rr.resources.root / "consumers" / (original.attempt_id + ".lock")
            retained = fence.with_suffix(".retained")
            identity = fence.stat().st_dev, fence.stat().st_ino
            receipts = rr.journal.observations(new_attempt.id, "consumer_fence")
            fence.rename(retained)
            try:
                with pytest.raises(RuntimeRefused, match="boundary is unavailable"):
                    with rr.resources.use(inherited):
                        pytest.fail("Missing original coordination cannot admit a consumer")
                assert not fence.exists()
                assert rr.journal.observations(new_attempt.id, "consumer_fence") == receipts
            finally:
                retained.rename(fence)
            with consumer_boundary(
                rr.resources.root, original.attempt_id, existing_only=True
            ) as (physical, created):
                assert not created and (physical["device"], physical["inode"]) == identity
                rr.resources.verify(inherited)
        if code:
            # R06: the installed delivery consumer sees the new attempt's full
            # own lifecycle, rather than only the final author checkpoint.
            from tests.scheduling.helpers import known_declaration

            current = r.core.get_task(handle, task.id)
            current = r.core.execution.complete_unit(
                handle,
                task.id,
                "remaining",
                current.version,
                current.decision_revision,
                r.proof(
                    "work_unit_complete",
                    task.id,
                    {"unit": "remaining", "prose_digest": current.prose_digest},
                ),
                r.key(),
            )
            known_declaration(r, current, handle)
            r.core.scheduling.successor_delivery(handle, task.id, new_attempt.id)
            with r.core.execution.worker_commit_boundary(
                handle,
                task.id,
                new_attempt.id,
                current.version,
                new_attempt.version,
                current.decision_revision,
            ):
                pass
            assert len(rr.runtime.workers) == 2
            from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGitContainer
            from ai_task_manager.application.commits import WorkerCommits
            from ai_task_manager.domains.work.errors import AccessDenied
            from ai_task_manager.installation.workflows.worker_resources import (
                ResourceWorkerCommits,
            )
            from tests.checks.fixtures import sha
            from tests.worker_commits.helpers import FixedChecks
            from tests.workflow_installation.code_controls import IMAGE

            docker = Path("/usr/local/bin/docker").resolve()
            endpoint = "unix://" + str(Path.home().joinpath(".docker/run/docker.sock").resolve())
            container = WorkerGitContainer(
                rr.journal, tmp_path / "contained-git", docker=docker,
                docker_sha256=sha(docker.read_bytes()), endpoint=endpoint, image=IMAGE,
                resource_root=rr.resources.root, deadline=time.time() + 120,
                run_id="r06-connected-" + str(prior_lineage),
            )
            commits = WorkerCommits(
                r.core, handle, runtime, rr.journal, ConfinedWorkerGit(rr.journal, container),
                checks=FixedChecks(rr.repo / "verify.py"),
            )
            driver.commits = ResourceWorkerCommits(commits, rr.resources)
            row = store.read(ref, r.scope)
            from ai_task_manager.supervisor.models import RuntimeRefused

            with pytest.raises(RuntimeRefused, match="Current successor consumer authority"):
                with rr.resources.use(inherited):
                    pass
            actual_observations = rr.journal.observations
            with monkeypatch.context() as missing:
                missing.setattr(
                    rr.journal, "observations",
                    lambda attempt, kind: []
                    if (attempt, kind) == (original.attempt_id, "consumer_fence")
                    else actual_observations(attempt, kind),
                )
                with pytest.raises(RuntimeRefused, match="Original resource consumer fence history"):
                    driver.commits.prepare(row, fresh)
            intent = driver.commits.prepare(row, fresh)
            assert intent["worktree"]["branch"] == "managed/" + original.attempt_id
            forged = {**row, "succession": {}}
            with pytest.raises(AccessDenied, match="outside the recorded"):
                driver.commits.prepare(forged, fresh)
            outcome = driver.run(ref)
            assert outcome["state"] == "awaiting_review", outcome
            committed = store.read(ref, r.scope)["steps"]["worker-commit"]["result"]
            assert committed["state"] == "committed"
            candidate = rr.journal.effect(new_attempt.id, "worker-commit-candidate")
            assert candidate and commits.git.committed(pair.worktree, candidate) != base
            assert git(pair.worktree, "show", "HEAD:result.txt") == "retain this partial work"
            with store.transaction() as conn:
                after_lineage = [tuple(row) for row in conn.execute("SELECT * FROM lineage")]
            if prior_lineage:
                assert after_lineage == original_lineage
            else:
                assert len(after_lineage) == 1 and after_lineage[0][2] == original.attempt_id

            from ai_task_manager.adapters.git.candidates import CandidateProfile
            from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
            from ai_task_manager.adapters.git.profile import Executable, GitProfile
            from ai_task_manager.adapters.workflow_reviews.code_candidates import (
                CodeCandidateReviews,
            )
            from ai_task_manager.application.merge_checks.service import MergeChecks
            from ai_task_manager.application.merging.authority import resource_alias
            from ai_task_manager.application.merging.candidates import (
                CandidateService,
                CoreCandidateAuthority,
            )
            from ai_task_manager.application.merging.workflows import (
                CandidateChecks,
                MergeEvidence,
                MergePlan,
                MergeWorkflowService,
            )
            from ai_task_manager.domains.merging.candidates import CandidateGrant
            from ai_task_manager.domains.merging.contracts import Binding
            from ai_task_manager.storage.checks import CheckStore
            from ai_task_manager.storage.merging.journal import Journal
            from tests.checks.fixtures import CHILD, Guard, catalogs, discovery, unit
            from tests.merge.workflows.helpers import OutsideExecutor
            from tests.merge.workflows.test_candidate_review import Captures

            git(rr.repo, "branch", "integration", base)
            home = tmp_path / "merge-home"
            home.mkdir(mode=0o700)
            profile = GitProfile(
                root=tmp_path,
                executable=Executable.capture("/usr/bin/git", GitProfile.environment(home)),
                home=home,
            )
            tree_id = resource_alias("worktree", inherited.worktree_id)
            repo_id = resource_alias("repository", str(rr.repo))
            record = profile.enroll(
                inherited.worktree, worktree_id=tree_id, repository_id=repo_id,
                target_ref="refs/heads/integration",
            )
            directory = tmp_path / "candidates"
            directory.mkdir(mode=0o700)
            current = r.core.get_task(handle, task.id)
            binding = Binding(
                task.id, new_attempt.id, r.scope.principal, r.scope.logical_session,
                r.scope.project, current.prose_digest, sha(CHILD), tree_id, repo_id,
                resource_alias("database", inherited.database_id),
                record["configuration_digest"], "candidate:r06",
            )
            grant = CandidateGrant(
                binding, "refs/heads/managed/" + original.attempt_id,
                git(inherited.worktree, "rev-parse", "HEAD"), base,
                current.version, new_attempt.version, current.decision_revision,
                r.decision(current, "In Dev", actor="model"), "lead:r06-control",
            )
            evidence = MergeEvidence(clock=lambda: r.now)
            r.core.context.evidence = EvidenceRouter([
                *r.core.context.evidence.routes,
                EvidenceRoute(
                    "merge-publication:", frozenset({"merge", "publication_intent"}), evidence
                ),
            ])
            journal = Journal(tmp_path / "merge/journal.sqlite")
            candidates = CandidateService(
                journal, CandidateProfile(profile, tree_id, repo_id, directory),
                CoreCandidateAuthority(r.core, handle, rr.resources, inherited),
                process_factory=CandidateProcesses,
            )
            executor = OutsideExecutor(tmp_path / "check-evidence")
            guard = Guard(source=sha(CHILD))
            checks = MergeChecks(
                CheckStore(tmp_path / "checks/results.sqlite"), guard, executor,
                catalogs=catalogs(tmp_path, project=r.scope.project),
            )

            def discover(candidate):
                guard.root = executor.root = Path(candidate.path)
                return discovery((unit(),))

            captures = Captures(lambda: r.now)
            reviews = CodeCandidateReviews(
                r.core, handle, candidates, ("partial.txt", "result.txt", "src/a.txt"), captures
            )
            merge_plan = MergePlan("fresh-r06-merge", grant, r.now + 100)
            workflow = MergeWorkflowService(
                r.core, handle, merge_plan, candidates, CandidateChecks(checks, discover),
                ProviderFixture(r), evidence, candidate_reviews=reviews, clock=lambda: r.now,
            )
            try:
                delivered = workflow.run(task.id, merge_plan.id, "r06-publish")
                assert delivered["data"]["state"] == "done", delivered
                assert git(rr.repo, "rev-parse", "integration") != base
                assert git(rr.repo, "show", "integration:result.txt") == "retain this partial work"
                assert r.core.get_task(handle, task.id).observed_status == "In Dev"
                before = len(captures.calls)
                assert workflow.resume(delivered["data"]["workflow"])["data"]["state"] == "done"
                assert len(captures.calls) == before
            finally:
                workflow.close()

        assert store.read(predecessor, r.scope) == old_row
    finally:
        rr.close()


def test_R04_own_remaining_unit_progress_binds_current_delivery_version(tmp_path):
    from tests.scheduling.helpers import known_declaration

    rr = RuntimeRig(tmp_path)
    r = rr.rig
    try:
        task = r.queued(code=True, units=("first", "remaining"))
        dispatch = rr.start(task)
        rr.admit(dispatch)
        rr.supervisor.execute(r.handle, dispatch, rr.command(dispatch), operation_key=r.key())
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "provider_status",
                            "decision",
                            "human_decision",
                            "work_unit_complete",
                            "workflow_successor",
                        }
                    ),
                    r.proofs,
                ),
            ]
        )

        def complete(handle, unit):
            current = r.core.get_task(handle, task.id)
            return r.core.execution.complete_unit(
                handle,
                current.id,
                unit,
                current.version,
                current.decision_revision,
                r.proof(
                    "work_unit_complete",
                    current.id,
                    {"unit": unit, "prose_digest": current.prose_digest},
                ),
                r.key(),
            )

        first = complete(r.handle, "first")
        r.now += 200
        decision = r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=r.now + 150,
        )
        a = SuccessorAuthorization(
            r.key(),
            r.scope,
            "workflow:old",
            task.id,
            first.version,
            0,
            "fresh",
            "digest",
            "fresh-key",
            r.now + 120,
            decision,
            ("remaining",),
        )
        r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", r.scope, task.id, asdict(a), a.expires_at
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        r.core.revoke_session(r.handle)
        r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
        current = complete(handle, "remaining")
        assert current.version == a.task_version + 1
        known_declaration(r, current, handle)
        checkpoint = r.core.scheduling.successor_delivery(handle, task.id, dispatch.attempt_id)
        attempt = r.core.get_attempt(handle, dispatch.attempt_id)
        assert checkpoint["task_version"] == current.version
        with r.core.execution.worker_commit_boundary(
            handle, task.id, attempt.id, current.version, attempt.version, current.decision_revision
        ):
            pass
        assert len(rr.runtime.workers) == 1
        # An unrelated version advance has no authorized own-unit proof.
        from ai_task_manager.storage import work

        with r.db.transaction() as conn:
            changed = work.save(conn, replace(current, title="Foreign revision"), current.version)
        from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError

        with pytest.raises(EvidenceRequired, match="Foreign task progress"):
            r.core.scheduling.successor_delivery(handle, changed.id, attempt.id)
        with pytest.raises(WorkError):
            with r.core.execution.worker_commit_boundary(
                handle,
                task.id,
                attempt.id,
                changed.version,
                attempt.version,
                changed.decision_revision,
            ):
                pass
    finally:
        rr.close()
