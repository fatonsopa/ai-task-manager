import json
from dataclasses import FrozenInstanceError, replace

import pytest
from composition_fixture import Chain
from f1_fixtures import FaultOnce

from ai_task_manager.domains.work.errors import WorkError


@pytest.fixture
def chain(tmp_path):
    c = object.__new__(Chain)
    try:
        c.__init__(tmp_path / "chain")
        yield c
    finally:
        if hasattr(c, "runtime"):
            c.close()


def test_actual_core_check_git_and_owned_database_chain(chain):
    result = chain.publish()
    assert result["publication"] == "committed" and result["quiescent"]
    assert chain.target() == chain.new
    facts = chain.consumer.checks.last_verification
    assert len(facts["physical_origins"]) == 1 and facts["carried_origins"] == []
    assert chain.pair.database.is_file()
    with chain.r.core.execution.work_snapshot_boundary(
        chain.r.handle, chain.attempt.id, chain.attempt.version
    ) as snapshot:
        assert snapshot == (chain.attempt, chain.task, chain.r.scope)
        with pytest.raises(FrozenInstanceError):
            snapshot[1].title = "mutated"
    (chain.runtime.path / "composition-result.json").write_text(
        json.dumps(
            {
                "publication": result,
                "checks": facts,
                "authority_timings": chain.consumer.authority.timings,
                "scope": (
                    "Actual local Core/check/resource/Git composition; fixture provider and host"
                ),
            },
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        "hold",
        "revoke",
        "stale_task",
        "stale_attempt",
        "wrong_resource",
        "wrong_scope",
        "source_changed",
        "catalog_changed",
        "missing_checks",
    ],
)
def test_current_authority_and_persisted_inputs_fence_publication(chain, change):
    if change == "hold":
        chain.runtime.hold(chain.dispatch)
    elif change == "revoke":
        chain.r.core.revoke_session(chain.r.handle)
    elif change in {"stale_task", "stale_attempt", "wrong_resource", "wrong_scope"}:
        grant = chain.grant
        if change == "stale_task":
            grant = replace(grant, task_version=grant.task_version - 1)
        elif change == "stale_attempt":
            grant = replace(grant, attempt_version=grant.attempt_version - 1)
        else:
            key = "resource_id" if change == "wrong_resource" else "principal_id"
            grant = replace(
                grant, binding=replace(grant.binding, **{key: "foreign"}, operation_key="different")
            )
        try:
            chain.consumer = chain.compose(grant)
        except WorkError:
            assert chain.target() == chain.old
            return
    elif change == "source_changed":
        (chain.tree / "src/a.txt").write_text("changed after checks")
    elif change == "catalog_changed":
        binding = chain.catalogs._bindings[chain.r.scope.project]
        binding[0].path.write_text("{}")
    elif change == "missing_checks":
        chain.consumer.check_ref = "check-plan:missing"
    with pytest.raises(WorkError):
        chain.publish()
    assert chain.target() == chain.old
    assert not chain.journal.current(chain.consumer.registration["ref"])["facts"].get("local_git")


def test_pending_checks_cannot_authorize_publication(tmp_path):
    chain = Chain(tmp_path / "chain", run=False)
    try:
        with pytest.raises(WorkError):
            chain.publish()
        assert chain.target() == chain.old
    finally:
        chain.close()


def test_later_hold_at_physical_release_is_observed(chain):
    chain.consumer.service.publisher.faults = FaultOnce(
        "before_final_validation", lambda: chain.runtime.hold(chain.dispatch)
    )
    result = chain.publish()
    assert result["publication"] != "committed" and chain.target() == chain.old
    assert not chain.journal.current(chain.consumer.registration["ref"])["facts"]["local_git"].get(
        "children"
    )


def test_historical_committed_replay_after_stop_does_not_reexecute(chain):
    first = chain.publish()
    chain.runtime.supervisor.stop(
        chain.r.handle, chain.dispatch, operation_key=chain.r.key(), reason="completed fixture"
    )
    before = chain.journal.events(chain.consumer.registration["ref"])
    chain.checks.source_guard.source = "0" * 64
    second = chain.publish()
    assert second == first
    assert chain.journal.events(chain.consumer.registration["ref"]) == before


def test_lost_reply_reconciles_same_persisted_effect(chain):
    chain.consumer.service.publisher.faults = FaultOnce("after_effect_observation")
    first = chain.publish()
    before = chain.journal.events(chain.consumer.registration["ref"])
    assert first["publication"] == "committed"
    assert chain.publish() == first
    assert chain.journal.events(chain.consumer.registration["ref"]) == before
