"""One real fixture subprocess crosses the public application boundary."""

from ai_task_manager.adapters.decisions.authority import DecisionAuthority, HumanInstruction
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.application.service.linear import DECISION_KINDS
from tests.application.helpers import Observations
from tests.runtime.helpers import RuntimeRig


def test_a17_review_can_start_a_real_isolated_fixture_with_observed_acceptance(tmp_path):
    fixture = RuntimeRig(tmp_path)
    rig = fixture.rig
    first = second = None
    try:
        prior_routes = fixture.rig.core.context.evidence.routes
        fixture.rig.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute(
                    route.prefix,
                    route.kinds | {"work_unit_complete"}
                    if route.prefix == "fixture:"
                    else route.kinds,
                    route.verifier,
                )
                for route in prior_routes
            ]
        )
        first = fixture.start()
        fixture.admit(first)
        fixture.supervisor.execute(
            rig.handle, first, fixture.command(first), operation_key=rig.key()
        )
        task = rig.core.get_task(
            rig.handle, rig.core.get_attempt(rig.handle, first.attempt_id).task_id
        )
        task = rig.complete(task)
        task = rig.transition(task, "In Review", "request_review")
        authority = AuthorityJournal(
            tmp_path / "authority.sqlite", tmp_path / "authority.key", clock=lambda: rig.now
        )
        observations = Observations()
        observations.instructions["review-acceptance"] = HumanInstruction(
            rig.scope,
            task.id,
            "In Progress",
            task.decision_revision,
            "Continue the accepted scoped fixture",
            "synthetic-observed-user-message",
            task.version,
            task.prose_digest,
        )
        fixture.rig.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute(
                    route.prefix,
                    route.kinds - {"decision", "human_decision"}
                    if route.prefix == "fixture:"
                    else route.kinds,
                    route.verifier,
                )
                for route in fixture.rig.core.context.evidence.routes
            ]
            + [EvidenceRoute("decision-proof:", DECISION_KINDS, authority)]
        )

        class Gateway:
            def start(self, handle, task_id, host, **arguments):
                nonlocal second
                second = fixture.supervisor.start(
                    handle, task_id, host, **arguments, deadline=rig.now + 120
                )
                fixture.dispatches.append(second)
                return fixture.supervisor.admit(
                    handle, second, operation_key="admit-public-fixture"
                )

        service = ApplicationService(
            rig.core,
            rig.handle,
            decisions=DecisionAuthority(authority, observations),
            workers=Gateway(),
        )
        start_arguments = {
            "task_id": task.id,
            "host": "fixture",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "human_ref": "review-acceptance",
            "operation_key": "public-review-start",
        }
        response = service.call("worker_start", start_arguments)
        assert response["ok"] and response["data"]["state"] == "running", response
        assert response["data"]["host"] == "fixture"
        assert rig.core.get_task(rig.handle, task.id).observed_status == "In Progress"
        assert second.resources.worktree != first.resources.worktree
        assert second.resources.database != first.resources.database
        result = fixture.supervisor.execute(
            rig.handle, second, fixture.command(second), operation_key=rig.key()
        )
        assert result.quiescent
        replay = service.call("worker_start", start_arguments)
        assert replay["ok"] and replay["data"]["state"] == "done"
        assert fixture.resources.cleanup(rig.core, rig.handle, second.attempt_id) == {
            "worktree": "removed",
            "database": "removed",
        }
        assert fixture.resources.cleanup(rig.core, rig.handle, first.attempt_id) == {
            "worktree": "removed",
            "database": "removed",
        }
    finally:
        fixture.close()
