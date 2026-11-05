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
