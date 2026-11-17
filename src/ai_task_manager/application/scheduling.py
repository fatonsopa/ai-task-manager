"""Scoped dependency application boundary; external observation happens elsewhere."""

from dataclasses import asdict

from ai_task_manager.domains.scheduling import policy
from ai_task_manager.domains.scheduling.models import (
    Blocker,
    Completion,
    Readiness,
    RejectRenewalSources,
    RejectSchedulingSources,
    SchedulingBlocked,
)
from ai_task_manager.domains.work.errors import EvidenceRequired, StaleVersion
from ai_task_manager.domains.work.models import DELIVERED, SyncState
from ai_task_manager.domains.work.models import AttemptState as A
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import attempts, events, issue_claims, sessions, work
from ai_task_manager.storage import scheduling as store

from .transition_evidence import destination_facts


class _DeliveryContext:
    """Supply no transition decision; reuse only shared delivery consistency checks."""

    payload = {}


class Scheduling:
    def __init__(self, context, sources=None):
        self.c, self.sources = context, sources or RejectSchedulingSources()
        self.renewals = RejectRenewalSources()

    def renew(self, handle, observation_ref, operation_key):
        from .renewals import renew

        return renew(self, handle, observation_ref, operation_key)

    def observe(
        self,
        handle,
        task_id,
        expected_version,
        expected_decision_revision,
        snapshot_ref,
        operation_key,
    ):
        payload = {
            "command": "dependency_snapshot",
            "task_id": task_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "snapshot_ref": snapshot_ref,
        }

        def action(conn, sid, scope):
            # Historical committed replay occurs before receipt resolution. The
            # configured resolver is local/immutable and must never perform HTTP.
            snapshot = self.sources.verify_snapshot(snapshot_ref)
            policy.validate(snapshot, self.c.clock())
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            if (
                snapshot.scope != scope
                or snapshot.task_id != task.id
                or snapshot.prose_digest != task.prose_digest
                or snapshot.canonical_issue_id != task.issue_id
            ):
                raise EvidenceRequired("Dependency snapshot belongs to another current task scope")
            if snapshot.expires_at <= self.c.clock():
                raise SchedulingBlocked("Dependency snapshot expired before consumption")
            prior = store.current(conn, task.id)
            graph_version = prior["graph_version"] if prior else 0
            if snapshot.previous_graph_version != graph_version:
                raise StaleVersion("Dependency graph version changed")
            digest = work.fingerprint(asdict(snapshot))
            if prior:
                if snapshot.source_id != prior["snapshot"].source_id:
                    raise EvidenceRequired("A different source cannot reset dependency ordering")
                if snapshot.source_revision <= prior["source_revision"]:
                    if (
                        snapshot.source_revision == prior["source_revision"]
                        and digest == prior["digest"]
                    ):
                        return self._view(conn, scope, task)
                    raise StaleVersion("Dependency observation is stale or conflicting")
            graph = store.graph(conn, sid)
            graph[task.id] = tuple(edge.predecessor for edge in snapshot.dependencies)
            policy.check_cycles(graph)

            def definition(value):
                return sorted(
                    (edge.predecessor, edge.condition, edge.unit or "")
                    for edge in value.dependencies
                )

            changed = not prior or definition(snapshot) != definition(prior["snapshot"])
            graph_version += int(changed)
            store.save(conn, snapshot, graph_version, digest)
            readiness = self._readiness(conn, scope, task)
            if prior and (
                changed
                or readiness.state != "ready"
                or prior["snapshot"].expires_at <= self.c.clock()
            ):
                conn.execute(
                    "UPDATE scheduling_attempts SET invalidated_by=? WHERE task_id=? "
                    "AND invalidated_by IS NULL",
                    (operation_key, task.id),
                )
            self.c.changed(
                conn,
                task,
                "scheduling_snapshot_recorded",
                {
                    "graph_version": graph_version,
                    "source_revision": snapshot.source_revision,
                    "readiness": readiness.state,
                },
            )
            return self._view(conn, scope, task)

        return self.c.mutation(handle, operation_key, payload, action)

    def _completion(self, conn, scope, session_id, edge):
        def unavailable(reason):
            return Completion(edge.predecessor, edge.condition, reason)

        if edge.predecessor.startswith("external:"):
            return unavailable("foreign")
        try:
            predecessor = work.get(conn, session_id, edge.predecessor)
        except Exception:
            return unavailable("missing")
        if predecessor.observed_status in {S.CANCELED, S.DUPLICATE}:
            return unavailable(
                "canceled" if predecessor.observed_status == S.CANCELED else "duplicate"
            )
        if predecessor.human_hold:
            return unavailable("held")
        if predecessor.execution_state in {A.FAILED, A.CANCELED, A.UNKNOWN}:
            return unavailable("failed" if predecessor.execution_state == A.FAILED else "ambiguous")
        if predecessor.sync_state != SyncState.CONFIRMED:
            return unavailable("unavailable")
        references = dict(edge.evidence_refs)
        try:
            if edge.condition == "unit_verified":
                proof = self.c.proof(
                    references.get("work_unit_complete"),
                    "work_unit_complete",
                    scope,
                    predecessor.id,
                )
                if (
                    proof.payload.get("unit") != edge.unit
                    or proof.payload.get("prose_digest") != predecessor.prose_digest
                ):
                    return unavailable("ambiguous")
                kind, commit, artifact = "work_unit_complete", None, None
            else:
                if edge.condition == "code_merged" and predecessor.observed_status not in DELIVERED:
                    return unavailable("unavailable")
                if edge.condition == "work_done" and predecessor.observed_status != S.DONE:
                    return unavailable("unavailable")
                facts, _ = destination_facts(
                    self.c,
                    conn,
                    scope,
                    predecessor,
                    S.IN_DEV if edge.condition == "code_merged" else S.DONE,
                    _DeliveryContext(),
                    references,
                )
                if not facts.complete or not facts.quiescent:
                    return unavailable("unavailable")
                if edge.condition == "code_merged":
                    if not facts.merged:
                        return unavailable("unavailable")
                    kind = "merge"
                elif predecessor.code_bearing:
                    if not facts.released:
                        return unavailable("unavailable")
                    kind = "production"
                else:
                    if not facts.non_code_verified:
                        return unavailable("unavailable")
                    kind = "non_code_completion"
                proof = self.c.proof(references.get(kind), kind, scope, predecessor.id)
                commit, artifact = proof.payload.get("commit"), proof.payload.get("artifact")
            return Completion(
                edge.predecessor,
                edge.condition,
                "verified",
                kind,
                predecessor.required_work,
                predecessor.completed_work,
                edge.unit,
                commit,
                artifact,
            )
        except Exception:
            return unavailable("unavailable")

    def _readiness(self, conn, scope, task, attempt=None, session_id=None):
        try:
            issue_claims.require_open(conn, scope, task.issue_id)
        except EvidenceRequired:
            return Readiness(
                "blocked",
                (
                    Blocker(
                        "issue_lifecycle_closed",
                        None,
                        "Create a new linked Backlog issue for further work",
                    ),
                ),
            )
        current = store.current(conn, task.id)
        if current is None:
            return policy.evaluate(None, {}, self.c.clock())
        snapshot = current["snapshot"]
        if (
            snapshot.scope != scope
            or snapshot.prose_digest != task.prose_digest
            or snapshot.canonical_issue_id != task.issue_id
            or current["invalidated_by"]
        ):
            return Readiness(
                "unknown",
                (
                    Blocker(
                        "dependency_source_changed",
                        None,
                        "Confirm current dependency and work-scope facts",
                    ),
                ),
            )
        if attempt is not None:
            binding = store.attempt_binding(conn, attempt.id)
            successor = False
            if isinstance(session_id, sessions.TaskSession) and attempt.state == "done":
                self.c.proof(session_id.reference, "workflow_successor", scope, task.id)
                rows = conn.execute(
                    "SELECT payload FROM checkpoints WHERE attempt_id=? "
                    "AND kind='successor_delivery'",
                    (attempt.id,),
                ).fetchall()
                import json

                successor = any(
                    json.loads(row[0])
                    == {
                        "authorization": session_id.reference,
                        "graph_version": current["graph_version"],
                        "task_version": task.version,
                        "decision_revision": task.decision_revision,
                    }
                    for row in rows
                )
            if not successor and (
                not binding
                or binding["invalidated_by"]
                or binding["graph_version"] != current["graph_version"]
            ):
                return Readiness(
                    "blocked",
                    (
                        Blocker(
                            "attempt_dependencies_invalidated",
                            None,
                            "Reconcile the existing attempt through the guarded "
                            "stop/resume workflow",
                        ),
                    ),
                )
        completions = {
            (edge.predecessor, edge.condition, edge.unit): self._completion(
                conn, scope, task.session_id, edge
            )
            for edge in snapshot.dependencies
        }
        return policy.evaluate(snapshot, completions, self.c.clock())

    def successor_delivery(self, handle, task_id, attempt_id):
        """Bind fresh unchanged dependency facts to a settled code successor.

        Original attempt invalidation remains history. This checkpoint permits
        delivery only through the new capability, never old worker execution.
        """
        import json

        from ai_task_manager.domains.work.errors import AccessDenied

        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            if not isinstance(sid, sessions.TaskSession):
                raise AccessDenied("Fresh task successor capability required")
            task, attempt = work.get(conn, sid, task_id), attempts.get(conn, sid, attempt_id)
            proof = self.c.proof(sid.reference, "workflow_successor", scope, task.id)
            self.c.check(task, task.version, proof.payload["decision_revision"])
            base_version = proof.payload["task_version"]
            enrolled = conn.execute(
                "SELECT workflow_ref FROM task_capabilities WHERE reference=?", (sid.reference,)
            ).fetchone()
            operation = conn.execute(
                "SELECT result FROM operations WHERE key=? AND session_id=?",
                (enrolled[0] + ":start:reserve", sid),
            ).fetchone()
            owned = False
            if operation:
                reserved = json.loads(operation[0])
                owned = (
                    reserved.get("id"),
                    reserved.get("task_id"),
                    reserved.get("host"),
                    reserved.get("decision_revision"),
                ) == (attempt.id, task.id, attempt.host, task.decision_revision)
            progress = set()
            lifecycle = {
                "attempt_reserved",
                "worker_started",
                "work_lease_granted",
                "attempt_outcome_recorded",
            }
            for row in conn.execute(
                "SELECT kind,payload FROM events WHERE task_id=? ORDER BY sequence", (task.id,)
            ):
                kind, value = row[0], json.loads(row[1])
                version = value.get("task_version", 0)
                if not base_version < version <= task.version:
                    continue
                accepted = (
                    kind == "work_unit_verified"
                    and value.get("unit") in proof.payload["remaining_work"]
                )
                if owned and kind in lifecycle and value.get("attempt_id") == attempt.id:
                    accepted = kind != "attempt_outcome_recorded" or value.get("outcome") == "done"
                if (
                    owned
                    and kind == "provider_status_confirmed"
                    and value.get("observed_status") == "In Progress"
                ):
                    confirmed = [
                        json.loads(item[0])
                        for item in conn.execute(
                            "SELECT payload FROM outbox WHERE task_id=? "
                            "AND kind='set_status' AND state='confirmed'",
                            (task.id,),
                        )
                    ]
                    accepted = any(
                        payload.get("attempt_id") == attempt.id
                        and payload.get("task_version") == version - 1
                        and payload.get("desired_status") == "In Progress"
                        for payload in confirmed
                    )
                if accepted:
                    progress.add(version)
            if task.version < base_version or progress != set(
                range(base_version + 1, task.version + 1)
            ):
                raise EvidenceRequired("Foreign task progress cannot rebind successor delivery")
            if (
                not task.code_bearing
                or task.human_hold
                or task.delivered_once
                or attempt.task_id != task.id
                or attempt.state != "done"
                or task.execution_state != "done"
                or self._readiness(conn, scope, task).state != "ready"
            ):
                raise AccessDenied("Fresh delivery requires current settled code and dependencies")
            current = store.current(conn, task.id)
            previous = store.attempt_binding(conn, attempt.id)
            if previous is None:
                raise EvidenceRequired("Original dependency binding is missing")
            if previous["graph_version"] != current["graph_version"]:
                raise EvidenceRequired("Changed dependency graph cannot reuse the old code attempt")
            recorded = conn.execute(
                "SELECT data FROM scheduling_history WHERE task_id=? AND graph_version=? "
                "AND source_revision<? ORDER BY id DESC LIMIT 1",
                (task.id, previous["graph_version"], current["source_revision"]),
            ).fetchone()
            if recorded is None:
                raise EvidenceRequired("Original dependency history is missing")
            original = store.snapshot_from(json.loads(recorded[0]))
            snapshot = current["snapshot"]
            fields = (
                "scope",
                "task_id",
                "prose_digest",
                "canonical_issue_id",
                "dependencies",
                "priority",
                "completeness",
            )
            if any(getattr(original, field) != getattr(snapshot, field) for field in fields):
                raise EvidenceRequired("Changed dependency work requires separate reconciliation")
            payload = {
                "authorization": sid.reference,
                "graph_version": current["graph_version"],
                "task_version": task.version,
                "decision_revision": task.decision_revision,
            }
            from ai_task_manager.config.files import digest

            checkpoint = "successor-delivery:" + digest(payload)
            conn.execute(
                "INSERT OR IGNORE INTO checkpoints VALUES(?,?,?,?)",
                (checkpoint, attempt.id, "successor_delivery", json.dumps(payload, sort_keys=True)),
            )
            return payload

    def _view(self, conn, scope, task):
        current = store.current(conn, task.id)
        active = attempts.active(conn, task.id)
        readiness = self._readiness(conn, scope, task, active)
        snapshot = current["snapshot"] if current else None
        return {
            "task_id": task.id,
            "task_version": task.version,
            "state": readiness.state,
            "blockers": [asdict(x) for x in readiness.blockers],
            "graph_version": current["graph_version"] if current else 0,
            "source_revision": current["source_revision"] if current else None,
            "priority": snapshot.priority if snapshot else None,
            "expires_at": snapshot.expires_at if snapshot else None,
            "queue_sequence": store.queue_sequence(conn, task.id),
        }

    def explain(self, handle, task_id):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            return self._view(conn, scope, work.get(conn, sid, task_id))

    def _candidates(self, conn, sid, scope, selected=None):
        result = []
        for row in conn.execute("SELECT id FROM tasks WHERE session_id=?", (sid,)).fetchall():
            if not sessions.permits(sid, row[0]):
                continue
            task = work.get(conn, sid, row[0])
            if (
                not task.queued_once
                or task.human_hold
                or task.delivered_once
                or task.sync_state != SyncState.CONFIRMED
                or attempts.active(conn, task.id)
                or (task.observed_status != S.TODO and task.id != selected)
            ):
                continue
            if self._readiness(conn, scope, task).state != "ready":
                continue
            current = store.current(conn, task.id)
            order = store.queue_sequence(conn, task.id)
            if order is not None:
                result.append((task.id, current["snapshot"].priority, order))
        return policy.ordered(result)

    def require(self, conn, scope, task, phase, attempt=None, *, session_id=None):
        readiness = self._readiness(conn, scope, task, attempt, session_id)
        if readiness.state != "ready":
            raise SchedulingBlocked("; ".join(x.release_condition for x in readiness.blockers))
        if phase == "reserve":
            candidates = self._candidates(conn, task.session_id, scope, task.id)
            if not candidates or candidates[0][0] != task.id:
                raise SchedulingBlocked("An earlier eligible queue item has priority")
        return store.current(conn, task.id)["graph_version"]

    def ordered(self, handle):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            items = [
                self._view(conn, scope, work.get(conn, sid, row[0]))
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE session_id=?", (sid,)
                ).fetchall()
                if sessions.permits(sid, row[0])
            ]
            return sorted(
                items,
                key=lambda x: (
                    policy.PRIORITIES.get(x["priority"], 99),
                    x["queue_sequence"] if x["queue_sequence"] is not None else 2**63,
                    x["task_id"],
                ),
            )

    def source_changed(self, conn, task, event):
        if event.kind.startswith("scheduling_") or event.kind == "dependency_inputs_changed":
            return
        for dependent_id in store.dependents(conn, task.id, task.session_id):
            if store.invalidate(conn, dependent_id, event.id, "predecessor_changed"):
                row = conn.execute(
                    "SELECT session_id FROM tasks WHERE id=?", (dependent_id,)
                ).fetchone()
                if row:
                    events.append(
                        conn,
                        row[0],
                        dependent_id,
                        "dependency_inputs_changed",
                        {"source_event": event.id, "reason": "Refresh authorized dependency facts"},
                    )
