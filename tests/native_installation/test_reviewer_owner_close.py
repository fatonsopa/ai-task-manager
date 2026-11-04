"""Owner teardown retains a separate reviewer after its author is terminal."""

from pathlib import Path

import pytest

import ai_task_manager.installation.native as native
from ai_task_manager.installation.workflows import InstalledWorkflow
from tests.native_installation import test_reviewer_composition

installed = test_reviewer_composition.installed


@pytest.mark.parametrize(
    "fault", [None, "missing_stop", "wrong_worker", "wrong_state", "foreign_scope"]
)
def test_RO01_failed_reviewer_is_in_owner_shutdown(installed, monkeypatch, fault):
    s = installed
    owner = InstalledWorkflow(
        s["config"],
        s["primary"].installation,
        native.resolve(s["config"], s["primary"].installation),
    )
    s["owners"].append(owner)
    factory = owner.registry.reviewer_factory
    producer = factory(s["inspection"])

    def failed(*args, **kwargs):
        raise RuntimeError("declared reviewer invocation failure")

    monkeypatch.setattr(producer.workers, "execute_reviewed", failed)
    with pytest.raises(RuntimeError, match="declared reviewer"):
        owner._review_source(Path(owner.spec.data["state_root"])).resolve(s["inspection"])
    # ReviewRig's author is already terminal; no author workflow is active here.
    assert (
        producer.workers.core.get_attempt(
            producer.handle, s["inspection"].context["author"]["attempt_id"]
        ).state
        == "done"
    )
    if fault in {"missing_stop", "wrong_worker", "wrong_state"}:
        original = producer.workers.journal.observations

        def observations(attempt_id, kind):
            rows = original(attempt_id, kind)
            if kind == "runtime_outcome":
                if fault == "missing_stop":
                    return []
                rows = [dict(row) for row in rows]
                rows[-1]["worker_id" if fault == "wrong_worker" else "state"] = "foreign"
            return rows

        monkeypatch.setattr(producer.workers.journal, "observations", observations)
    if fault == "foreign_scope":
        original_attempt = producer.workers.journal.attempt
        monkeypatch.setattr(
            producer.workers.journal,
            "attempt",
            lambda attempt_id: {**original_attempt(attempt_id), "scope": {"foreign": True}},
        )

    def forbidden_stop(*args, **kwargs):
        pytest.fail("owner close must not repeat native stop")

    monkeypatch.setattr(producer.workers, "stop", forbidden_stop)
    outcomes = owner.service.workflows.close()
    assert len(outcomes) == 1
    assert outcomes[0]["role"] == "reviewer"
    assert outcomes[0]["task_id"] == s["task"].id
    assert outcomes[0]["quiescent"] is (fault is None)
    if fault is not None:
        assert outcomes[0]["state"] == "uncertain"
    assert owner.service.workflows.close() == outcomes


def test_RO02_unknown_reviewer_retains_obligation_without_repeated_stop(installed, monkeypatch):
    from dataclasses import replace

    s = installed
    owner = InstalledWorkflow(
        s["config"],
        s["primary"].installation,
        native.resolve(s["config"], s["primary"].installation),
    )
    s["owners"].append(owner)
    factory = owner.registry.reviewer_factory
    producer = factory(s["inspection"])
    original_start = producer.workers.start
    stops = []

    def start(*args, **kwargs):
        attempt = original_start(*args, **kwargs)
        host = producer.workers.hosts.owned[attempt.id]
        original_stop = host.stop

        def unknown_stop():
            stops.append(attempt.id)
            return replace(original_stop(), confirmed=False, reason="declared helper unknown")

        monkeypatch.setattr(host, "stop", unknown_stop)
        return attempt

    monkeypatch.setattr(producer.workers, "start", start)

    def failed(*args, **kwargs):
        raise RuntimeError("declared reviewer invocation failure")

    monkeypatch.setattr(producer.workers, "execute_reviewed", failed)
    with pytest.raises(RuntimeError, match="declared reviewer"):
        owner._review_source(Path(owner.spec.data["state_root"])).resolve(s["inspection"])
    assert len(stops) == 1
    outcomes = owner.service.workflows.close()
    assert outcomes[0]["state"] == "uncertain" and outcomes[0]["quiescent"] is False
    attempt = producer.workers.core.get_attempt(producer.handle, stops[0])
    assert attempt.state == "unknown"
    assert owner.service.workflows.close() == outcomes
    assert len(stops) == 1
    with pytest.raises(Exception, match="owner is closed"):
        factory(s["inspection"])


@pytest.mark.parametrize("outcome", ["unknown", "uncertain"])
def test_RO03_launch_propagates_unresolved_shutdown(monkeypatch, outcome):
    from types import SimpleNamespace

    import ai_task_manager.installation.workflows as workflows

    row = {"role": "reviewer", "state": outcome, "quiescent": False}
    monkeypatch.setattr(
        workflows,
        "InstalledWorkflow",
        lambda *a, **kw: SimpleNamespace(
            serve=lambda *a: None, owner=SimpleNamespace(shutdown_result=[row])
        ),
    )
    result = workflows.launch("declared-config", "declared-installation", registry=object())
    assert result["outcome"] == "uncertain" and result["exit_code"] == 6
    assert result["data"]["shutdown"] == [row]


def test_RO04_no_reviewer_leaves_unrelated_workflow_close_unchanged():
    from types import SimpleNamespace

    from ai_task_manager.application.workflows.service import WorkflowService

    service = WorkflowService(SimpleNamespace())
    assert service.close() == []


def test_RO05_reviewer_failure_is_not_an_empty_success():
    from types import SimpleNamespace

    from ai_task_manager.application.workflows.service import WorkflowService

    def failed():
        raise RuntimeError("declared journal unavailable")

    service = WorkflowService(SimpleNamespace(), reviewer_owner=SimpleNamespace(close=failed))
    assert service.close() == [
        {"role": "reviewer", "state": "uncertain", "quiescent": False, "reason": "RuntimeError"}
    ]
