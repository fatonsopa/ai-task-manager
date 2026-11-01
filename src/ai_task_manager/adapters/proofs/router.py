"""Route opaque references to one configured issuer with no fallback."""

from dataclasses import dataclass

from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence
from ai_task_manager.domains.work.ports import EvidenceVerifier


@dataclass(frozen=True)
class EvidenceRoute:
    prefix: str
    kinds: frozenset[str]
    verifier: EvidenceVerifier


class EvidenceRouter:
    def __init__(self, routes):
        self.routes = tuple(routes)
        prefixes, kinds = set(), set()
        for route in self.routes:
            if (
                not route.prefix
                or not route.prefix.endswith(":")
                or not route.kinds
                or route.prefix in prefixes
                or any(route.prefix.startswith(p) or p.startswith(route.prefix) for p in prefixes)
                or kinds.intersection(route.kinds)
            ):
                raise EvidenceRequired("Proof issuer prefixes and kinds must be distinct")
            prefixes.add(route.prefix)
            kinds.update(route.kinds)

    def verify_evidence(self, proof_ref):
        if not isinstance(proof_ref, str):
            raise EvidenceRequired("Opaque evidence reference is required")
        route = next((r for r in self.routes if proof_ref.startswith(r.prefix)), None)
        if route is None:
            raise EvidenceRequired("Unknown proof issuer")
        result = route.verifier.verify_evidence(proof_ref)
        if not isinstance(result, Evidence) or result.kind not in route.kinds:
            raise EvidenceRequired("Proof kind is not owned by this issuer")
        return result
