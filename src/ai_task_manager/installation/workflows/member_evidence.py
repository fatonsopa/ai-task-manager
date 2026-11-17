"""Read milestone proofs only from their sealed installed task's current issuer."""

from ai_task_manager.adapters.proofs.router import EvidenceRouter
from ai_task_manager.adapters.workflow_reviews.code_worker_units import CodeWorkerUnitIssuer
from ai_task_manager.adapters.workflow_reviews.issuer import WorkflowReviews
from ai_task_manager.application.merging.workflows.evidence import MergeEvidence
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence, Scope


class MemberMilestones(EvidenceRouter):
    def __init__(self, routes, members):
        super().__init__(routes)
        self.members = tuple(members)

    def with_routes(self, routes):
        return MemberMilestones(routes, self.members)

    def verify_evidence(self, reference):
        families = {
            "workflow-review:": (WorkflowReviews, frozenset({"work_unit_complete", "non_code_completion"})),
            "code-worker-review:": (CodeWorkerUnitIssuer, frozenset({"work_unit_complete"})),
            "merge-publication:": (MergeEvidence, frozenset({"merge", "publication_intent"})),
        }
        family = next((p for p in families if isinstance(reference, str) and reference.startswith(p)), None)
        if family is None:
            return super().verify_evidence(reference)
        self.members[0].spec.check()
        issuer_type, kinds = families[family]
        matches = []
        for member in self.members:
            driver = member.service.workflows.driver
            delivery = driver.code_delivery
            issuer = (member.review_issuer if issuer_type is WorkflowReviews else
                      delivery.issuer if issuer_type is CodeWorkerUnitIssuer and delivery else
                      delivery.evidence if issuer_type is MergeEvidence and delivery else None)
            if type(issuer) is issuer_type and issuer.owns_reference(reference):
                matches.append((member, issuer))
        if len(matches) != 1:
            raise EvidenceRequired("One original installed task must own the milestone reference")
        member, issuer = matches[0]
        member.spec.check()
        proof = issuer.verify_evidence(reference)
        if (not isinstance(proof, Evidence) or proof.kind not in kinds
                or proof.scope != Scope(**member.spec.data["scope"])
                or proof.subject != member.spec.data["task_id"]):
            raise EvidenceRequired("Milestone differs from its original installed task and scope")
        return proof
