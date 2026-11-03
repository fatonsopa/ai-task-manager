import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import pytest
from composition_fixture import Chain
from f1_fixtures import FaultOnce

from ai_task_manager.domains.work.errors import WorkError


@contextmanager
def owned_chain(root, **options):
    value = object.__new__(Chain)
    try:
        value.__init__(root, **options)
        yield value
    finally:
        if hasattr(value, "runtime"):
            value.close()


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
    with chain.r.core.execution.merge_snapshot_boundary(
        chain.r.handle,
        chain.task.id,
        chain.attempt.id,
        chain.task.version,
        chain.attempt.version,
        chain.task.decision_revision,
        chain.grant.decision_ref,
        publication_context=chain.grant.publication_context,
    ) as snapshot:
        assert snapshot == (chain.task, chain.attempt, chain.r.scope)
        with pytest.raises(FrozenInstanceError):
            snapshot[0].title = "mutated"
    with pytest.raises(WorkError):
        chain.r.core.execution.validate_work_lease(
            chain.r.handle, chain.attempt.id, chain.attempt.version
        )
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
    with owned_chain(tmp_path / "chain", run=False) as chain:
        with pytest.raises(WorkError):
            chain.publish()
        assert chain.target() == chain.old


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
    assert chain.attempt.state == "done" and chain.dispatch.worker.process.returncode is not None
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


def test_actual_failed_physical_check_denies_publication(tmp_path):
    with owned_chain(tmp_path / "chain", value="5") as chain:
        snapshot = chain.store.read(chain.plan_ref, chain.scope)
        assert snapshot["outcome"] == "failed"
        result = snapshot["groups"]["checks a"]["result"]
        assert result["exit_code"] == 1 and result["ran"] == ["a"]
        with pytest.raises(WorkError):
            chain.publish()
        assert chain.target() == chain.old


@pytest.mark.parametrize("outcome", ["unresolved_intent", "unknown_result"])
def test_persisted_uncertainty_is_not_check_permission(tmp_path, outcome):
    with owned_chain(tmp_path / "chain", run=False) as chain:
        version = chain.store.begin(chain.plan_ref, chain.scope, "checks a", 0, ("a",), {})
        if outcome == "unknown_result":
            chain.store.finish(
                chain.plan_ref, chain.scope, "checks a", version, error="FixtureObserverUnavailable"
            )
        with pytest.raises(WorkError):
            chain.publish()
        assert chain.target() == chain.old
        assert not chain.journal.current(chain.consumer.registration["ref"])["facts"].get(
            "local_git"
        )


@pytest.mark.parametrize("mismatch", ["principal", "attempt", "head"])
def test_actual_persisted_foreign_plan_cannot_authorize_publication(chain, mismatch):
    from ai_task_manager.domains.checks.models import Availability, Change
    from tests.checks.fixtures import discovery

    scope = replace(chain.scope, principal="other") if mismatch == "principal" else chain.scope
    ref = chain.checks.prepare(
        scope,
        "foreign-attempt" if mismatch == "attempt" else chain.attempt.id,
        chain.source,
        Change(
            Availability.KNOWN,
            ("src/a.txt",),
            chain.old,
            chain.old if mismatch == "head" else chain.new,
        ),
        discovery(chain.units),
        "other-plan",
    )
    # The same-store physical proof is reusable only within its exact scope.
    # A foreign principal remains pending; attempt/head probes use real carried
    # proof facts rather than fabricated success dictionaries.
    if mismatch != "principal":
        chain.checks.run(ref, scope)
        assert chain.store.read(ref, scope)["outcome"] == "passed"
    chain.consumer.check_ref = ref
    with pytest.raises(WorkError):
        chain.publish()
    assert chain.target() == chain.old


def test_same_input_reuse_preserves_actual_physical_origin(chain):
    from ai_task_manager.domains.checks.models import Availability, Change
    from tests.checks.fixtures import discovery

    before = len(chain.executor.records)
    ref = chain.checks.prepare(
        chain.scope,
        chain.attempt.id,
        chain.source,
        Change(Availability.KNOWN, ("src/a.txt",), chain.old, chain.new),
        discovery(chain.units),
        "reused-plan",
    )
    snapshot = chain.checks.run(ref, chain.scope)
    assert snapshot["outcome"] == "passed"
    assert snapshot["groups"]["checks a"]["result"]["carried_count"] == 1
    assert len(chain.executor.records) == before
    chain.consumer.check_ref = ref
    assert chain.publish()["publication"] == "committed"
    facts = chain.consumer.checks.last_verification
    assert facts["physical_origins"] == [] and len(facts["carried_origins"]) == 1


@pytest.mark.parametrize("change", ["hold", "stale_attempt"])
def test_atomic_snapshot_port_refuses_before_body(chain, change):
    expected = chain.attempt.version
    if change == "hold":
        chain.runtime.hold(chain.dispatch)
    else:
        expected -= 1
    entered = False
    with pytest.raises(WorkError):
        with chain.r.core.execution.merge_snapshot_boundary(
            chain.r.handle,
            chain.task.id,
            chain.attempt.id,
            chain.task.version,
            expected,
            chain.task.decision_revision,
            chain.grant.decision_ref,
        ):
            entered = True
    assert not entered and chain.target() == chain.old


def test_observed_reassignment_blocks_effect_without_releasing_owned_resources(chain):
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
    from tests.domains.test_external_supersession import observation

    # This is an authored provider observation passed through the real Core
    # boundary. No live assignment or provider response is claimed.
    chain.r.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, chain.runtime.proofs),
            EvidenceRoute(
                "fixture:",
                frozenset(
                    {"provider_status", "decision", "human_decision", "external_observation"}
                ),
                chain.r.proofs,
            ),
        ]
    )
    task = chain.r.core.get_task(chain.r.handle, chain.task.id)
    proof = observation(chain.r, task, status="In Progress", assigned_to_owner=False)
    chain.r.core.external.observe(
        chain.r.handle, task.id, task.version, task.decision_revision, proof, chain.r.key()
    )
    with pytest.raises(WorkError):
        chain.publish()
    current = chain.r.core.get_task(chain.r.handle, task.id)
    retained = chain.r.core.get_attempt(chain.r.handle, chain.attempt.id)
    assert current.human_hold and current.sync_state == "uncertain"
    assert retained == chain.attempt and retained.state == "done"
    assert chain.pair.database.is_file() and chain.pair.worktree.is_dir()
    assert chain.target() == chain.old


def test_missing_pending_fence_refuses_physical_publication_boundary(chain):
    entered = False
    with pytest.raises(WorkError):
        with chain.consumer.authority.authority_boundary(chain.binding):
            entered = True
    assert not entered and chain.target() == chain.old
    assert not chain.journal.current(chain.consumer.registration["ref"])["facts"].get("local_git")
