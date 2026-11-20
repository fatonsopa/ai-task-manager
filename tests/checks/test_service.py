from dataclasses import replace

import pytest

from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.checks import CheckStore
from tests.checks.fixtures import (
    SCOPE,
    SOURCE,
    FixtureExecutor,
    Guard,
    RecordingExecutor,
    catalogs,
    change,
    discovery,
    execution,
    private_dir,
    sha,
    unit,
    write_inputs,
)


def rig(tmp_path, *, executor=None, guard=None, baselines=None):
    root = private_dir(tmp_path)
    store = CheckStore(root / "checks.sqlite")
    executor = executor or RecordingExecutor()
    return MergeChecks(
        store, guard or Guard(), executor, catalogs=catalogs(root), baselines=baselines
    ), executor


def prepare(
    service, units=None, *, key="operation", changes=None, attempt="attempt", prerequisites=()
):
    if prerequisites:
        service.catalogs = catalogs(service.store.path.parent, prerequisites=prerequisites)
    return service.prepare(
        SCOPE,
        attempt,
        SOURCE,
        changes or change(),
        discovery(units),
        key,
    )


def test_plan_precedes_dispatch(tmp_path):
    def observe(p, group, names):
        state = service.store.read(ref, SCOPE)
        assert state["groups"][group]["state"] == "intent"
        assert [e["kind"] for e in state["events"]] == ["plan_created", "dispatch_intent"]
        return execution(p, group, names)

    service, executor = rig(tmp_path, executor=RecordingExecutor(observe))
    ref = prepare(service)
    assert executor.calls == []
    result = service.run(ref, SCOPE)
    assert result["outcome"] == "passed"
    assert [e["kind"] for e in result["events"]] == [
        "plan_created",
        "dispatch_intent",
        "group_result",
    ]


def test_reused_results_are_not_physical(tmp_path):
    service, executor = rig(tmp_path)
    first = prepare(service)
    service.run(first, SCOPE)
    second = prepare(service, key="retry", attempt="retry")
    result = service.run(second, SCOPE)["groups"]["checks a"]["result"]
    assert len(executor.calls) == 1
    assert result["physical_count"] == 0 and result["carried_count"] == 1
    assert result["ran"] == [] and set(result["carried"]) == {"a"}
    assert service.run(second, SCOPE)["groups"]["checks a"]["result"] == result


def test_partial_reuse_dispatches_only_missing(tmp_path):
    service, executor = rig(tmp_path)
    service.run(prepare(service), SCOPE)
    second = prepare(
        service, (unit("a"), unit("b")), key="partial", changes=change(("src/a.txt", "src/b.txt"))
    )
    result = service.run(second, SCOPE)["groups"]["checks a"]["result"]
    assert executor.calls[-1] == ("checks a", ("b",))
    assert result["selected"] == ["a", "b"]
    assert result["ran"] == ["b"] and result["physical_count"] == 1
    assert result["carried_count"] == 1


def test_results_survive_restart_and_later_failure(tmp_path):
    units = (unit("a"), unit("b", "checks b"))
    root = private_dir(tmp_path)
    write_inputs(root, units)
    (root / "src/b.txt").write_text("wrong")
    broken = replace(
        units[1], inputs=(replace(units[1].inputs[0], value=sha("wrong")), *units[1].inputs[1:])
    )
    executor = FixtureExecutor(root)
    service, _ = rig(root, executor=executor, guard=Guard(root=root))
    first = prepare(service, (units[0], broken), changes=change(("src/a.txt", "src/b.txt")))
    failed = service.run(first, SCOPE)
    assert failed["outcome"] == "failed"
    assert failed["groups"]["checks a"]["result"]["outcome"] == "passed"
    assert failed["groups"]["checks b"]["result"]["outcome"] == "assertion_failed"
    old_events = failed["events"]
    assert len(executor.records) == 2
    (root / "src/b.txt").write_text("4")
    reopened = MergeChecks(
        CheckStore(root / "checks.sqlite"), Guard(root=root), executor, catalogs=catalogs(root)
    )
    retry = prepare(
        reopened, units, key="fixed", attempt="retry", changes=change(("src/a.txt", "src/b.txt"))
    )
    fixed = reopened.run(retry, SCOPE)
    assert fixed["outcome"] == "passed"
    assert fixed["groups"]["checks a"]["result"]["carried_count"] == 1
    assert fixed["groups"]["checks b"]["result"]["physical_count"] == 1
    assert len(executor.records) == 3
    assert reopened.store.read(first, SCOPE)["events"] == old_events
    for record in executor.records:
        observed = record["completion"]["group"]
        assert observed["confirmed_stop"] and observed["observation"]["known"]
        assert observed["observation"]["live_pids"] == []
        assert record["bytes"] > 0


def test_independent_groups_continue(tmp_path):
    units = (unit("a"), unit("b", "checks b"), unit("c", "checks c"))

    def observe(p, group, names):
        return execution(p, group, names, cause="source_error", exit_code=1, passed=())

    service, executor = rig(tmp_path, executor=RecordingExecutor(observe))
    ref = prepare(
        service,
        units,
        changes=change(("src/a.txt", "src/b.txt", "src/c.txt")),
        prerequisites=(("checks c", ("checks a",)),),
    )
    result = service.run(ref, SCOPE)
    assert [g for g, _ in executor.calls] == ["checks a", "checks b"]
    assert result["groups"]["checks c"]["result"]["reason"] == "Prerequisite not passed"
    assert result["outcome"] == "failed"


def test_consumer_applies_attribution(tmp_path):
    signature = sha("assertion")

    class Baseline:
        def comparable_failures(self, p, group):
            assert p.head == "2" * 40 and group == "checks a"
            return (("a", signature),)

    def observe(p, group, names):
        return execution(p, group, names, passed=(), failures=(("a", signature),), exit_code=1)

    service, _ = rig(tmp_path, executor=RecordingExecutor(observe), baselines=Baseline())
    result = service.run(prepare(service), SCOPE)
    assert result["outcome"] == "dev_red"
    assert result["groups"]["checks a"]["result"]["failures"] == [["a", signature]]


def test_executor_exception_is_uncertain(tmp_path):
    effects = []

    def effect_then_error(p, group, names):
        effects.append(names)
        raise AccessDenied("Reply validation failed after an effect")

    service, _ = rig(tmp_path, executor=RecordingExecutor(effect_then_error))
    ref = prepare(service)
    result = service.run(ref, SCOPE)
    assert result["outcome"] == "blocked"
    row = result["groups"]["checks a"]["result"]
    assert row["outcome"] == "unknown" and row["physical_count"] is None
    assert row["failure"] and "Reply validation failed after an effect" not in str(row["failure"])
    assert "target_change" not in row
    assert effects == [("a",)]
    service.run(ref, SCOPE)
    assert effects == [("a",)]


def test_retry_retains_failure(tmp_path):
    service, executor = rig(
        tmp_path,
        executor=RecordingExecutor(
            lambda p, g, n: execution(
                p, g, n, passed=(), failures=(("a", sha("intermittent assertion")),), exit_code=1
            )
        ),
    )
    failed_ref = prepare(service)
    first = service.run(failed_ref, SCOPE)
    service.executor = RecordingExecutor()
    passing = service.run(prepare(service, key="retry", attempt="retry"), SCOPE)
    assert passing["outcome"] == "passed"
    assert service.store.read(failed_ref, SCOPE)["events"] == first["events"]
    assert first["groups"]["checks a"]["result"]["outcome"] == "assertion_failed"
    assert len(executor.calls) == 1


def test_phase_durations(tmp_path):
    service, _ = rig(tmp_path)
    result = service.run(prepare(service), SCOPE)["groups"]["checks a"]["result"]
    assert result["active_seconds"] == result["ended"] - result["started"] == 1
    assert "queue_seconds" not in result and "end_to_end_seconds" not in result
    assert result["physical_count"] == 1


def test_source_binding_changed(tmp_path):
    service, executor = rig(tmp_path)
    ref = prepare(service)
    service.source_guard.source = sha("changed producer")
    with pytest.raises(AccessDenied):
        service.run(ref, SCOPE)
    assert executor.calls == []
    assert service.store.read(ref, SCOPE)["groups"]["checks a"]["state"] == "pending"


def test_postexecution_source_change_cannot_mint_proof(tmp_path):
    guard = Guard()

    def mutate(p, group, names):
        guard.source = sha("changed")
        return execution(p, group, names)

    service, _ = rig(tmp_path, executor=RecordingExecutor(mutate), guard=guard)
    ref = prepare(service)
    result = service.run(ref, SCOPE)
    assert result["outcome"] == "blocked"
    assert result["groups"]["checks a"]["result"]["physical_count"] == 1
    assert "target_change" not in result["groups"]["checks a"]["result"]
    assert service.store.proofs(result["plan"], result["plan"].units[0]) == []


def test_empty_plan_has_no_dispatch(tmp_path):
    service, executor = rig(tmp_path)
    ref = prepare(service, (), changes=change(()))
    result = service.run(ref, SCOPE)
    assert result["outcome"] == "passed" and executor.calls == []
    assert [e["kind"] for e in result["events"]] == ["plan_created"]


def test_empty_plan_still_checks_current_source(tmp_path):
    service, executor = rig(tmp_path)
    ref = prepare(service, (), changes=change(()))
    service.source_guard.source = sha("changed")
    with pytest.raises(AccessDenied):
        service.run(ref, SCOPE)
    assert executor.calls == []


def test_baseline_failure_does_not_discard_execution(tmp_path):
    class Baseline:
        def comparable_failures(self, p, group):
            raise RuntimeError("Authored unavailable comparison")

    signature = (("a", sha("assertion")),)
    service, _ = rig(
        tmp_path,
        baselines=Baseline(),
        executor=RecordingExecutor(
            lambda p, g, n: execution(p, g, n, passed=(), failures=signature, exit_code=1)
        ),
    )
    result = service.run(prepare(service), SCOPE)["groups"]["checks a"]["result"]
    assert result["outcome"] == "assertion_failed"
    assert result["physical_count"] == 1
    assert result["baseline_available"] is False
    assert result["baseline_failures"] is None
