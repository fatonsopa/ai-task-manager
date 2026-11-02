from dataclasses import replace

import pytest

from ai_task_manager.domains.scheduling.models import Dependency
from ai_task_manager.domains.work.errors import StaleVersion
from ai_task_manager.domains.work.models import IssueStatus as S

from .helpers import declare, snapshot


@pytest.mark.parametrize("variant", ["predecessor_change", "graph_refresh", "hold", "assignment_away", "duplicate", "reordered", "same_session_delivery_event", "distinct_session_event_isolation", "distinct_principal_event_isolation"])
def test_event_driven_wake_and_fencing(variant, rig):
    if variant in {"same_session_delivery_event", "distinct_session_event_isolation", "distinct_principal_event_isolation"}:
        session_event_control(rig, variant)
        return
    predecessor, dependent = rig.task(), rig.task()
    declare(rig.core, rig.handle, dependent, rig.scheduling_sources, rig.now,
            (Dependency(predecessor.id, "work_done"),))
    with rig.db.transaction() as conn:
        before = conn.execute("SELECT count(*) FROM scheduling_wakes WHERE task_id=?", (dependent.id,)).fetchone()[0]
    if variant == "graph_refresh":
        prior = rig.core.scheduling.explain(rig.handle, dependent.id)
        declare(rig.core, rig.handle, dependent, rig.scheduling_sources, rig.now,
                (Dependency(predecessor.id, "work_done"),))
        assert rig.core.scheduling.explain(rig.handle, dependent.id)["graph_version"] == prior["graph_version"]
        return
    if variant in {"hold", "assignment_away"}:
        proof = rig.proof("human_decision", predecessor.id, {
            "previous_revision": predecessor.decision_revision,
            "hold": True, "assignment": "other" if variant == "assignment_away" else None,
        })
        rig.core.decisions.record(rig.handle, predecessor.id, predecessor.version, proof, rig.key())
    else:
        with rig.db.transaction() as conn:
            event = rig.core.context.changed(conn, predecessor, "authored_current_source_change")
            if variant == "duplicate":
                # The exact original event identity must deduplicate, not a new event.
                from ai_task_manager.storage import events
                original = next(item for item in reversed(events.list_events(conn, predecessor.session_id, 0))
                                if item.task_id == predecessor.id and item.kind == "authored_current_source_change")
                rig.core.scheduling.source_changed(conn, predecessor, original)
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM scheduling_wakes WHERE task_id=?", (dependent.id,)).fetchone()[0] == before + 1
        assert conn.execute("SELECT invalidated_by FROM scheduling_snapshots WHERE task_id=?", (dependent.id,)).fetchone()[0]
    if variant == "reordered":
        old = snapshot(rig.core, rig.handle, dependent, rig.now,
                       source_revision=1)
        with pytest.raises(StaleVersion):
            rig.core.scheduling.observe(rig.handle, dependent.id, dependent.version, 0,
                rig.scheduling_sources.issue(old), rig.key())
    assert rig.core.scheduling.explain(rig.handle, dependent.id)["state"] == "unknown"


@pytest.mark.parametrize("variant", ["restart", "expired_snapshot", "missed_source_event", "reconnect"])
def test_missed_event_and_expiry_repair(variant, rig):
    task = rig.task()
    if variant in {"restart", "reconnect"}:
        rig.core = rig.new_core()
        assert rig.core.scheduling.explain(rig.handle, task.id)["state"] == "ready"
    else:
        rig.now += 31
        assert rig.core.scheduling.explain(rig.handle, task.id)["state"] == "unknown"
    declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now)
    assert rig.core.scheduling.explain(rig.handle, task.id)["state"] == "ready"
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def session_event_control(rig, variant):
    from ai_task_manager.storage import events

    predecessor = rig.task()
    if variant == "same_session_delivery_event":
        scope, handle = rig.scope, rig.handle
        dependent = rig.task()
    else:
        scope = replace(rig.scope, logical_session="separate-session",
                        principal="separate-principal" if variant == "distinct_principal_event_isolation" else rig.scope.principal)
        handle = rig.session(scope)
        dependent = rig.task(handle=handle, confirmed=False)
        dependent = rig.confirm(dependent, handle=handle, scope=scope)
    declare(rig.core, handle, dependent, rig.scheduling_sources, rig.now,
            (Dependency(predecessor.id, "work_done"),))
    def retained():
        with rig.db.transaction() as conn:
            return {
                "events": [tuple(row) for row in conn.execute("SELECT * FROM events WHERE task_id=?", (dependent.id,))],
                "snapshot": [tuple(row) for row in conn.execute("SELECT * FROM scheduling_snapshots WHERE task_id=?", (dependent.id,))],
                "wakes": [tuple(row) for row in conn.execute("SELECT * FROM scheduling_wakes WHERE task_id=?", (dependent.id,))],
            }
    before = retained()
    with rig.db.transaction() as conn:
        rig.core.context.changed(conn, predecessor, "authored_delivery_fact_changed")
        source = next(e for e in reversed(events.list_events(conn, predecessor.session_id, 0))
                      if e.kind == "authored_delivery_fact_changed")
        rig.core.scheduling.source_changed(conn, predecessor, source)
    after = retained()
    if variant == "same_session_delivery_event":
        assert len(after["events"]) == len(before["events"]) + 1
        assert len(after["wakes"]) == len(before["wakes"]) + 1
        assert rig.core.scheduling.explain(handle, dependent.id)["state"] == "unknown"
    else:
        assert after == before
        assert rig.core.scheduling.explain(handle, dependent.id)["state"] == "blocked"
        assert source.id not in str(after)
