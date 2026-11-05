"""Existing Core observation API retains current stopped-worker dependency facts."""

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.worker_commits.helpers import WorkerFixture


@pytest.mark.parametrize("case", ["current", "expired", "foreign", "collection_expired"])
def test_delivery_dependency_collection_never_revives_expired_or_foreign_authority(tmp_path, case):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        before = r.core.scheduling.explain(r.handle, row["task_id"])
        if case == "expired":
            r.now = before["expires_at"]
        elif case == "foreign":
            row = dict(row, prose_digest="0" * 64)
        elif case == "collection_expired":
            original = source.prepare

            def delayed(*args, **kwargs):
                value = original(*args, **kwargs)
                r.now = before["expires_at"]
                return value

            source.prepare = delayed
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        if case == "current":
            task_before = r.core.get_task(r.handle, row["task_id"])
            controller.start(row)
            controller.check()
            after = r.core.scheduling.explain(r.handle, row["task_id"])
            assert after["graph_version"] == before["graph_version"]
            assert after["source_revision"] > before["source_revision"]
            assert r.core.get_task(r.handle, row["task_id"]) == task_before
        else:
            with pytest.raises(AccessDenied):
                controller.start(row)
            after = r.core.scheduling.explain(r.handle, row["task_id"])
            assert after["source_revision"] == before["source_revision"]
    finally:
        if controller is not None:
            controller.close()
            assert controller._thread is None or not controller._thread.is_alive()
        fixture.close()


def test_delivery_close_during_collection_publishes_no_new_observation(tmp_path):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        original = source.prepare
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        before = r.core.scheduling.explain(r.handle, row["task_id"])

        def close_during_read(*args, **kwargs):
            value = original(*args, **kwargs)
            controller.close()
            return value

        source.prepare = close_during_read
        controller.start(row)
        assert controller._thread is None
        assert (
            r.core.scheduling.explain(r.handle, row["task_id"])["source_revision"]
            == before["source_revision"]
        )
    finally:
        if controller is not None:
            controller.close()
        fixture.close()


def test_own_unit_progress_during_collection_keeps_exact_scope_and_graph(tmp_path):
    fixture = WorkerFixture((tmp_path / "worker").resolve())
    controller = None
    try:
        row = fixture.execute_only()
        r = fixture.rig
        source = AuthoredDependencyCollection(r.core, r.handle)
        original = source.prepare
        before = r.core.scheduling.explain(r.handle, row["task_id"])
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter

        # Authored unit fact exercises version progress only, not semantic acceptance.
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute(
                    route.prefix,
                    route.kinds | {"work_unit_complete"}
                    if route.prefix == "fixture:"
                    else route.kinds,
                    route.verifier,
                )
                for route in r.core.context.evidence.routes
            ]
        )

        progressed = []

        def complete_during_read(*args, **kwargs):
            value = original(*args, **kwargs)
            if not progressed:
                progressed.append(True)
                r.complete(r.core.get_task(r.handle, row["task_id"]))
            return value

        source.prepare = complete_during_read
        controller = DeliveryDependencies(r.core, r.handle, source, fixture.plan.deadline)
        controller.start(row)
        controller.check()
        current = r.core.get_task(r.handle, row["task_id"])
        assert current.completed_work == current.required_work
        assert (
            r.core.scheduling.explain(r.handle, current.id)["graph_version"]
            == before["graph_version"]
        )
    finally:
        if controller is not None:
            controller.close()
        fixture.close()
