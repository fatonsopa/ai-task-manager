"""Candidate facts over the existing coherent merge journal, without another schema."""

from dataclasses import asdict, replace
from uuid import uuid4

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict


class CandidateStore:
    def __init__(self, journal):
        self.journal = journal

    def create(self, grant, profile, *, predecessor=None):
        profile.validate()
        binding = grant.binding
        row = self.journal.register(asdict(binding), binding.fingerprint)
        ref = row["ref"]
        operation = self.journal.current(ref)
        request = {"grant": asdict(grant), "profile": profile.digest}
        old = operation["facts"].get("candidate")
        if old:
            if old["request"] != request:
                raise IdempotencyConflict("Candidate request differs from its durable source")
            return ref, False
        # Configured labels and private Git homes can differ for the same
        # physical repository. They remain in the operation above, but cannot
        # split the scoped task's canonical effect owner.
        physical_repository = {
            "device": profile.source["common_identity"][0],
            "inode": profile.source["common_identity"][1],
        }
        claim_context = {
            "version": 1,
            "principal": binding.principal_id,
            "project": binding.project_id,
            "session": binding.session_id,
            "task": binding.task_id,
            "repository": physical_repository,
        }
        claim_key = "candidate-claim:" + digest(claim_context)
        claim_binding = replace(
            binding,
            operation_key=claim_key,
            repository_id="physical-repository:" + digest(physical_repository),
            configuration_digest=digest(claim_context),
        )
        claim = self.journal.register(asdict(claim_binding), claim_binding.fingerprint)["ref"]
        state = self.journal.current(claim)
        owner = state["facts"].get("candidate_owner")
        if owner and owner["operation"] != ref:
            if predecessor is None or owner["operation"] != predecessor:
                raise AccessDenied("A different or unresolved candidate owns this task")
            prior = self.get(predecessor)
            if prior["claim"] != claim or prior["generation"] != owner["generation"]:
                raise AccessDenied("Candidate successor lost its exact predecessor claim")
            generation = str(uuid4())
            self.journal.commit(
                claim,
                state["version"],
                "candidate-successor:" + ref,
                "candidate_successor",
                dict(
                    state["facts"],
                    candidate_owner={
                        "operation": ref,
                        "owner_ref": grant.owner_ref,
                        "generation": generation,
                    },
                    candidate_owner_history=[
                        *state["facts"].get("candidate_owner_history", []),
                        owner,
                    ],
                ),
            )
            owner = {"operation": ref, "owner_ref": grant.owner_ref, "generation": generation}
        generation = str(uuid4()) if not owner else owner["generation"]
        if not owner:
            self.journal.commit(
                claim,
                state["version"],
                "claim:" + generation,
                "candidate_claim",
                {
                    "candidate_owner": {
                        "operation": ref,
                        "owner_ref": grant.owner_ref,
                        "generation": generation,
                    }
                },
            )
        else:
            generation = owner["generation"]
        candidate = {
            "request": request,
            "claim": claim,
            "generation": generation,
            "path": str(profile.root / generation),
            "branch": "refs/heads/candidates/" + generation,
            "stage": "intent",
            "effects": {},
        }
        self.journal.commit(
            ref,
            operation["version"],
            "candidate-create",
            "candidate_intent",
            dict(operation["facts"], candidate=candidate),
            request=request,
        )
        return ref, True

    def get(self, ref):
        return self.journal.current(ref)["facts"]["candidate"]

    def write(self, ref, data, stage):
        row = self.journal.current(ref)
        return self.journal.commit(
            ref,
            row["version"],
            "candidate:" + str(uuid4()),
            "candidate_" + stage,
            dict(row["facts"], candidate=data),
            data,
        )

    def owner(self, ref):
        data = self.get(ref)
        current = self.journal.current(data["claim"])["facts"].get("candidate_owner")
        expected = {
            "operation": ref,
            "generation": data["generation"],
            "owner_ref": data["request"]["grant"]["owner_ref"],
        }
        if current != expected:
            raise AccessDenied("Canonical candidate owner changed")
        return current

    def effect(self, ref, name, payload):
        self.owner(ref)
        data = self.get(ref)
        if name in data["effects"]:
            raise AccessDenied("Existing Git effect must reconcile instead of repeating")
        effects = dict(data["effects"], **{name: {"intent": payload}})
        self.write(ref, dict(data, effects=effects, stage=name), name + "_intent")

    def observe(self, ref, name, key, value):
        data = self.get(ref)
        effects = dict(data["effects"])
        effects[name] = dict(effects[name], **{key: value})
        self.write(ref, dict(data, effects=effects), name + "_" + key)
