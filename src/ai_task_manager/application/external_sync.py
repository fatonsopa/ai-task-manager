"""Record remote facts and fence stale effects before legal policy settlement."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, StaleVersion
from ai_task_manager.domains.work.models import DELIVERED, AttemptState, Evidence, SyncState
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.transitions import check_transition, human_hold_required
from ai_task_manager.storage import attempts, issue_claims, sessions, work
from ai_task_manager.storage.database import encode

from .intake import Intake
from .transition_evidence import destination_facts


class ExternalSynchronization:
    def __init__(self, context):
        self.c = context

    def observe(
        self,
        handle,
        task_id,
        expected_version,
        expected_decision_revision,
        observation_ref,
        operation_key,
    ):
        payload = {
            "command": "external_observe",
            "task_id": task_id,
            "version": expected_version,
            "decision_revision": expected_decision_revision,
            "observation_ref": observation_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            proof = self.c.proof(observation_ref, "external_observation", scope, task.id)
            data = proof.payload
            if (
                not task.issue_id
                or data.get("issue_id") != task.issue_id
                or not isinstance(data.get("source_revision"), str)
                or not data["source_revision"]
                or not isinstance(data.get("status"), str)
            ):
                raise EvidenceRequired("Remote observation lacks bound issue, revision or status")
            revision, digest = data["source_revision"], work.fingerprint(data)
            if data["status"] in DELIVERED or data["status"] in {S.CANCELED, S.DUPLICATE}:
                issue_claims.fence(
                    conn,
                    scope,
                    task.issue_id,
                    delivered=data["status"] in DELIVERED,
                    terminal=data["status"] in {S.CANCELED, S.DUPLICATE},
                    source=observation_ref,
                )
            old = conn.execute(
                "SELECT * FROM external_observations WHERE task_id=? "
                "ORDER BY source_revision DESC,rowid DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            state = "pending"
            if old and revision <= old["source_revision"]:
                if revision < old["source_revision"] or digest == old["digest"]:
                    return {
                        "observation_id": old["id"],
                        "state": "duplicate" if revision == old["source_revision"] else "stale",
                    }
                old_data = json.loads(old["payload"])
                facts = (
                    "issue_id",
                    "status",
                    "title",
                    "body",
                    "assigned_to_owner",
                    "source_revision",
                )
                same_facts = all(data.get(key) == old_data.get(key) for key in facts)
                was_human = (
                    old_data.get("actor_kind") == "human"
                    and old_data.get("actor_authorized") is True
                )
                is_human = (
                    data.get("actor_kind") == "human" and data.get("actor_authorized") is True
                )
                if same_facts and was_human:
                    return {"observation_id": old["id"], "state": "stale"}
                if same_facts and is_human and not was_human:
                    conn.execute(
                        "UPDATE external_observations SET state='superseded' WHERE id=?",
                        (old["id"],),
                    )
                else:
                    state = "conflict"
            observation_id = str(uuid4())
            conn.execute(
                "INSERT INTO external_observations VALUES(?,?,?,?,?,?,?)",
                (observation_id, task.id, revision, digest, observation_ref, encode(data), state),
            )
            pending = conn.execute(
                "SELECT id,attempt_count FROM outbox WHERE task_id=? "
                "AND state!='confirmed' AND fenced_by IS NULL",
                (task.id,),
            ).fetchall()
            for row in pending:
                conn.execute(
                    "UPDATE outbox SET fenced_by=? WHERE id=?", (observation_id, row["id"])
                )
                conn.execute(
                    "INSERT INTO outbound_attempts(operation_id,attempt,outcome,detail) "
                    "VALUES(?,?,?,?)",
                    (row["id"], row["attempt_count"], "fenced", observation_id),
                )
            human = (
                state == "pending"
                and data.get("actor_kind") == "human"
                and data.get("actor_authorized") is True
            )
            task = work.save(
                conn,
                replace(
                    task,
                    human_hold=True,
                    delivered_once=task.delivered_once or data["status"] in DELIVERED,
                    sync_state=SyncState.UNCERTAIN,
                    decision_revision=task.decision_revision + int(human),
                    decision_ref=observation_ref,
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "external_change_observed",
                {
                    "observation_id": observation_id,
                    "remote_status": data["status"],
                    "source_revision": revision,
                    "application": state,
                    "observation_ref": observation_ref,
                },
            )
            if data.get("title") != task.title or data.get("body") != task.body:
                self.c.changed(
                    conn,
                    task,
                    "external_prose_change_staged",
                    {
                        "observation_id": observation_id,
                        "source_revision": revision,
                        "proposed_title": data.get("title"),
                        "proposed_body_digest": work.fingerprint({"body": data.get("body")}),
                        "requires_writer_approval": True,
                        "requires_quiescence": attempts.active(conn, task.id) is not None,
                    },
                )
            return {"observation_id": observation_id, "state": state}

        return self.c.mutation(handle, operation_key, payload, action)

    def approve_prose_revision(
        self,
        handle,
        task_id,
        observation_id,
        prose,
        approval_ref,
        expected_version,
        expected_decision_revision,
        readback_ref,
        operation_key,
    ):
        rendered, digest = Intake(self.c).approved(prose, approval_ref)
        payload = {
            "command": "external_approve_prose",
            "task_id": task_id,
            "observation_id": observation_id,
            "prose": asdict(prose),
            "approval_ref": approval_ref,
            "version": expected_version,
            "revision": expected_decision_revision,
            "readback_ref": readback_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            row = conn.execute(
                "SELECT * FROM external_observations WHERE id=? AND task_id=?",
                (observation_id, task.id),
            ).fetchone()
            if (
                row is None
                or row["state"] != "pending"
                or task.decision_ref != row["proof_ref"]
                or issue_claims.known_delivered(conn, scope, task)
                or attempts.active(conn, task.id) is not None
            ):
                raise EvidenceRequired(
                    "Staged prose needs current undelivered scope and actual quiescence"
                )
            original = json.loads(row["payload"])
            if (
                original.get("actor_kind") != "human"
                or original.get("actor_authorized") is not True
            ):
                raise EvidenceRequired("The staged edit has no authorized human scope decision")
            readback = self.c.proof(readback_ref, "external_prose_readback", scope, task.id).payload
            keys = ("issue_id", "source_revision", "status", "title", "body", "assigned_to_owner")
            if (
                any(readback.get(key) != original.get(key) for key in keys)
                or readback.get("title") != rendered.title
                or readback.get("body") != rendered.body
            ):
                raise EvidenceRequired(
                    "Approved prose must match the staged exact provider readback"
                )
            task = work.save(
                conn,
                replace(
                    task,
                    title=rendered.title,
                    body=rendered.body,
                    prose_digest=digest,
                    representation=rendered.representation,
                    approval_ref=approval_ref,
                    code_bearing=prose.code_bearing,
                    required_work=tuple(prose.required_work),
                    completed_work=(),
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "external_prose_revision_approved",
                {
                    "observation_id": observation_id,
                    "approval_ref": approval_ref,
                    "readback_ref": readback_ref,
                    "status_settlement": "pending",
                },
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)

    def settle(
        self,
        handle,
        task_id,
        observation_id,
        expected_version,
        expected_decision_revision,
        cause,
        evidence_refs,
        operation_key,
    ):
        payload = {
            "command": "external_settle",
            "task_id": task_id,
            "observation_id": observation_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "cause": cause,
            "evidence_refs": evidence_refs,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            row = conn.execute(
                "SELECT * FROM external_observations WHERE task_id=? AND id=?",
                (task.id, observation_id),
            ).fetchone()
            if not row or row["state"] != "pending" or task.decision_ref != row["proof_ref"]:
                raise StaleVersion("Observation is missing, settled or superseded")
            proof = self.c.proof(row["proof_ref"], "external_observation", scope, task.id)
            data = proof.payload
            if data != json.loads(row["payload"]):
                raise EvidenceRequired("Observation evidence changed after recording")
            if data.get("actor_kind") != "human" or data.get("actor_authorized") is not True:
                raise EvidenceRequired("External change has no authorized human decision")
            try:
                target = S(data["status"])
            except ValueError as exc:
                raise InvalidTransition("Remote state is outside the configured workflow") from exc
            if data.get("title") != task.title or data.get("body") != task.body:
                raise EvidenceRequired("Remote prose change needs new exact writing approval")
            if target == S.IN_PROGRESS and target != task.observed_status:
                raise InvalidTransition("Remote In Progress cannot establish worker startup")
            if task.delivered_once and target == S.IN_PROGRESS:
                raise InvalidTransition("Further work requires a new linked Backlog issue")
            decision = Evidence(
                "decision",
                scope,
                task.id,
                {
                    "actor": "human",
                    "reason": data.get("reason", "Authorized Linear edit"),
                    "target": target,
                    "revision": task.decision_revision,
                },
            )
            facts, active = destination_facts(
                self.c, conn, scope, task, target, decision, evidence_refs
            )
            if (
                active
                and (data.get("assigned_to_owner") is not True or target != task.observed_status)
                and not facts.quiescent
            ):
                raise EvidenceRequired("Observed human intent does not prove worker quiescence")
            check_transition(task.observed_status, target, cause, facts)
            if active and facts.quiescent:
                attempts.save(
                    conn, replace(active, state=AttemptState.PAUSED, version=active.version + 1)
                )
                conn.execute("DELETE FROM resource_leases WHERE attempt_id=?", (active.id,))
            task = work.save(
                conn,
                replace(
                    task,
                    desired_status=target,
                    observed_status=target,
                    sync_state=SyncState.CONFIRMED,
                    human_hold=(
                        human_hold_required(target) or data.get("assigned_to_owner") is not True
                    ),
                    queued_once=task.queued_once
                    or (target == S.TODO and not issue_claims.imported(conn, task.id)),
                    delivered_once=task.delivered_once or target in DELIVERED,
                    execution_state=AttemptState.PAUSED
                    if active and facts.quiescent
                    else task.execution_state,
                ),
                task.version,
            )
            conn.execute(
                "UPDATE external_observations SET state='applied' WHERE id=?", (observation_id,)
            )
            self.c.changed(
                conn,
                task,
                "external_change_applied",
                {
                    "observation_id": observation_id,
                    "source_revision": row["source_revision"],
                    "cause": cause,
                },
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)

    def observations(self, handle, task_id):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            work.get(conn, sid, task_id)
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,source_revision,state,payload FROM "
                    "external_observations WHERE task_id=? ORDER BY rowid",
                    (task_id,),
                )
            ]
