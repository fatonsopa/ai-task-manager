"""Production preparation/reader/receipts with authored GraphQL, never live Linear."""

from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from tests.scheduling.provider_fixture import ProviderFixture


@pytest.mark.parametrize("change", ["own_unit", "hold", "expired", "foreign"])
def test_in_fetch_change_uses_exact_production_preparation_and_bounded_recollection(
    tmp_path, change
):
    p = ProviderFixture(tmp_path / "provider")
    r = p.rig
    task = p.task(code=True)
    p.refresh(task)
    task = r.transition(task, "Todo", "enqueue")
    p.sync(task)
    p.refresh(task)
    task, attempt = r.running(task)
    p.sync(task)
    task, attempt = r.settle(task, attempt)
    p.sync(task)
    p.refresh(task)
    before = r.core.scheduling.explain(r.handle, task.id)
    row = {
        "task_id": task.id,
        "attempt_id": attempt.id,
        "prose_digest": task.prose_digest,
        "decision_revision": task.decision_revision,
    }
    triggered, preparations = [], []
    original = p.source.prepare

    def prepare(*args, **kwargs):
        preparations.append((args[1], kwargs["expected_version"]))
        return original(*args, **kwargs)

    p.source.prepare = prepare

    def during_fetch(query, variables):
        if triggered or "DependencyProse" not in query:
            return
        triggered.append(query)
        current = r.core.get_task(r.handle, task.id)
        if change == "own_unit":
            p.sync(r.complete(current))
        elif change == "hold":
            proof = r.proof(
                "human_decision",
                current.id,
                {"previous_revision": current.decision_revision, "hold": True},
            )
            r.core.decisions.record(r.handle, current.id, current.version, proof, r.key())
        elif change == "expired":
            r.now = before["expires_at"]
        else:
            p.client.expected = replace(p.expected, viewer_id=str(uuid4()))

    p.callback = during_fetch
    controller = DeliveryDependencies(r.core, r.handle, p.source, r.now + 120)
    try:
        if change == "own_unit":
            controller.start(row)
            controller.check()
            assert len(triggered) == 1 and len(preparations) == 2
            assert preparations[0][0] != preparations[1][0]
            assert preparations[1][1] > preparations[0][1]
            after = r.core.scheduling.explain(r.handle, task.id)
            assert after["graph_version"] == before["graph_version"]
            assert after["source_revision"] > before["source_revision"]
        else:
            with pytest.raises(WorkError):
                controller.start(row)
            assert len(preparations) == 1
            assert (
                r.core.scheduling.explain(r.handle, task.id)["source_revision"]
                == before["source_revision"]
            )
    finally:
        controller.close()
        assert controller._thread is None or not controller._thread.is_alive()
