"""Consume opaque existing evidence; this module never issues acceptance proofs."""

from ai_task_manager.domains.work.errors import EvidenceRequired


class ReviewedEvidence:
    def __init__(self, core, lookup):
        self.core, self.lookup = core, lookup

    def read(self, scope, task, observation):
        references = self.lookup(scope, task, observation["digest"])
        if references is None:
            return None
        if type(references) is not dict or set(references) != {"units", "delivery"}:
            raise EvidenceRequired("Opaque reviewed references are required, not a passed flag")
        if set(references["units"]) != set(task.required_work):
            raise EvidenceRequired("Review does not cover every declared work unit")
        for unit, ref in references["units"].items():
            proof = self.core.context.proof(ref, "work_unit_complete", scope, task.id)
            if proof.payload.get("unit") != unit:
                raise EvidenceRequired("Review work unit differs")
            self._artifact(proof, task, observation)
        proof = self.core.context.proof(
            references["delivery"], "non_code_completion", scope, task.id
        )
        self._artifact(proof, task, observation)
        return references

    @staticmethod
    def _artifact(proof, task, observation):
        if (
            proof.payload.get("prose_digest") != task.prose_digest
            or proof.payload.get("artifact_digest") != observation["digest"]
        ):
            raise EvidenceRequired("Semantic acceptance belongs to another artifact or scope")
