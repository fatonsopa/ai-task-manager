"""Real successor/Core/workflow and Git; persisted completion is authored input.

The existing private Git fixture uses commit-tree, not hooks or a container. This
control qualifies receipt consumption, never container closure or genuine review.
"""

import json
import secrets
from contextlib import nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.application.commits import WorkerCommits
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlans
from ai_task_manager.application.workflows.recovery import SuccessorAuthorization, admit_successor
from ai_task_manager.domains.checks.models import fingerprint
from ai_task_manager.domains.work.models import Evidence, SessionHandle
from ai_task_manager.installation.workflows.code_delivery import DeliveryResources
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from ai_task_manager.installation.workflows.worker_resources import ResourceWorkerCommits
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.worker_commits.helpers import WorkerFixture, git
from tests.worker_commits.test_private_commit_controls import private_commit


@pytest.mark.parametrize("case", ["current", "missing_receipt", "changed_source", "dependencies"])
def test_successor_reconciles_pending_completed_commit_without_effects(tmp_path, monkeypatch, case):
    fixture = WorkerFixture((tmp_path / "owned").resolve(), resource_locks=())
    freshness = None
    try:
        control = private_commit(fixture)
        r, journal = fixture.rig, fixture.rr.journal
        attempt_id = control.pair.attempt_id
        intent = {k: v for k, v in control.candidate.items() if k not in {"tree", "message"}}
        journal.intend(attempt_id, "worker-commit-intent", intent)
        journal.intend(attempt_id, "worker-commit-candidate", control.candidate)
        control.publish()
        receipt = {
            "task_id": control.row["task_id"],
            "attempt_id": attempt_id,
            "commit": control.head,
            "tree": control.candidate["tree"],
            "branch": control.row["steps"]["start"]["result"].get(
                "branch", "managed/" + attempt_id
            ),
            "intent_digest": fingerprint(intent),
            "state": "committed",
        }
        if case != "missing_receipt":
            journal.observe(attempt_id, "worker-commit-complete", receipt)
        saved_observations = journal.observations(attempt_id, "worker-commit-complete")
        fixture.store.claim(fixture.ref, r.scope, "original-owner")
        fixture.store.intend(fixture.ref, "original-owner", "worker-commit", intent)
        fixture.store.release(fixture.ref, "original-owner")
        original = fixture.store.read(fixture.ref, r.scope)
        assert journal.observations(attempt_id, "runtime_outcome")[-1]["quiescent"]
        r.now += 31
        assert r.core.scheduling.explain(r.handle, original["task_id"])["state"] == "unknown"
        task = r.core.get_task(r.handle, original["task_id"])
        plan = replace(fixture.plan, id="fresh", deadline=r.now + 100)
        decision = r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=plan.deadline,
        )
        authority = SuccessorAuthorization(
            r.key(),
            r.scope,
            fixture.ref,
            task.id,
            task.version,
            task.decision_revision,
            plan.id,
            plan.digest,
            "fresh-key",
            plan.deadline,
            decision,
            task.required_work,
        )
        r.proofs.evidence[authority.reference] = Evidence(
            "workflow_successor", r.scope, task.id, asdict(authority), authority.expires_at
        )
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, fixture.rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"decision", "human_decision", "provider_status", "workflow_successor"}
                    ),
                    r.proofs,
                ),
            ]
        )
        fresh_handle = SessionHandle(secrets.token_urlsafe(32))
        r.core.revoke_session(r.handle)
        settled = SimpleNamespace(
            check=lambda *_: None, check_locked=lambda *_: None, boundary=lambda *_: nullcontext()
        )
        ref = admit_successor(
            r.core,
            r.handle,
            fresh_handle,
            fixture.store,
            authority,
            plan,
            fixture.runtime.binding(task, plan),
            settled,
        )
        source = AuthoredDependencyCollection(r.core, fresh_handle)
        source.fail = case == "dependencies"
        if case == "changed_source":
            (control.pair.worktree / "result.py").write_text("Changed after the original commit\n")
        freshness = DeliveryDependencies(r.core, fresh_handle, source, plan.deadline)
        resources = DeliveryResources(
            fixture.rr.resources, r.core.context.clock, plan.deadline, freshness
        )
        commits = ResourceWorkerCommits(
            WorkerCommits(
                r.core,
                fresh_handle,
                fixture.runtime,
                journal,
                control.observer,
                checks=fixture.checks,
            ),
            resources,
            freshness,
        )
        fixture.rr.resources.completed_successors = frozenset({attempt_id})

        def forbidden(*args, **kwargs):
            raise AssertionError("Retained receipt must not replay an effect")

        for name in ("prepare", "run"):
            monkeypatch.setattr(commits.commits, name, forbidden)
        monkeypatch.setattr(control.observer, "reconcile_publication", forbidden)
        monkeypatch.setattr(control.observer, "require_quiescent_commit", forbidden)
        driver = WorkflowDriver(
            r.core,
            fresh_handle,
            fixture.store,
            WorkflowPlans((plan,)),
            runtime=fixture.runtime,
            repository=fixture.driver.repository,
            commits=commits,
        )
        result = driver.run(ref)
        current = fixture.store.read(ref, r.scope)
        facts = {
            "result": result,
            "pending_retained": original["steps"]["worker-commit"],
            "current_commit": current["steps"].get("worker-commit"),
            "author_processes": len(fixture.rr.runtime.workers),
            "checks": fixture.checks.calls,
            "dependency_collections": source.calls,
        }
        (tmp_path / "retained-commit-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        if case != "current":
            assert result["state"] in {"blocked", "uncertain"}, facts
            assert current["steps"]["worker-commit"]["result"] is None
            assert len(fixture.rr.runtime.workers) == fixture.checks.calls == 1
            assert journal.observations(attempt_id, "worker-commit-complete") == saved_observations
            return
        assert result["state"] == "awaiting_review" and result["commit"] == control.head, facts
        assert current["steps"]["worker-commit"]["result"] == receipt
        assert fixture.store.read(fixture.ref, r.scope) == original
        assert len(fixture.rr.runtime.workers) == fixture.checks.calls == 1
        assert source.calls >= 1
        assert git(control.pair.worktree, "rev-parse", "HEAD") == control.head
        assert journal.observations(attempt_id, "worker-commit-complete") == saved_observations
    finally:
        if freshness is not None:
            freshness.close()
        fixture.close()
