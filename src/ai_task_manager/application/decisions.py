from dataclasses import asdict, replace

from ai_task_manager.domains.work.errors import StaleDecision, ValidationError
from ai_task_manager.storage import work


class Decisions:
    def __init__(self, context):
        self.c = context

    def record(self, handle, task_id, expected_version, proof_ref, operation_key):
        payload = {
            "command": "human_decision",
            "task_id": task_id,
            "version": expected_version,
            "proof_ref": proof_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, task.decision_revision)
            proof = self.c.proof(proof_ref, "human_decision", scope, task_id)
            if (
                type(proof.payload.get("previous_revision")) is not int
                or proof.payload["previous_revision"] != task.decision_revision
            ):
                raise StaleDecision("Human decision is older than the current revision")
            if type(proof.payload.get("hold")) is not bool:
                raise ValidationError("Human decision must explicitly retain or release the hold")
            task = work.save(
                conn,
                replace(
                    task,
                    decision_revision=task.decision_revision + 1,
                    decision_ref=proof_ref,
                    human_hold=proof.payload["hold"],
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "human_decision_recorded",
                {
                    "proof_ref": proof_ref,
                    "hold": task.human_hold,
                    "assignment": proof.payload.get("assignment"),
                },
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)
