"""Application operation facts only; real foreground execution remains unavailable."""

from contextlib import contextmanager

from ai_task_manager.domains.work.errors import AccessDenied, InvalidTransition, ValidationError

from .contracts import Answer, Binding, Observation, sha, token


class Operations:
    def __init__(self, journal, authority, observations=None, stop_port=None):
        self.journal, self.authority = journal, authority
        self.observations, self.stop_port = observations, stop_port

    def _load(self, ref):
        operation = self.journal.current(ref)
        return operation, Binding(**operation["binding"])

    def check_authority(self, ref):
        _, binding = self._load(ref)
        if self.authority is None:
            raise AccessDenied("Trusted authority is unavailable")
        return self.authority.check_current(binding)

    def intend_launch(self, ref, expected_version, key):
        request = ["launch_intent"]
        replay = self.journal.replay(ref, key, request)
        if replay is not None:
            return replay
        operation, binding = self._load(ref)
        if self.authority is None:
            raise AccessDenied("Trusted authority is unavailable")
        if operation["facts"].get("launch_intent"):
            raise InvalidTransition("An existing launch intent cannot be repeated")
        self.authority.before_start(binding)
        self.authority.check_current(binding)
        facts = dict(operation["facts"], launch_intent=True)
        return self.journal.commit(
            ref, expected_version, key, "launch_intent", facts, request=request
        )

    def prepare_publication(self, ref, expected_version, key, *, old, new, checked, range_ref):
        payload = {
            "old": sha(old, 40),
            "new": sha(new, 40),
            "checked": sha(checked, 40),
            "range_ref": token(range_ref),
        }
        request = ["publication_intent", payload]
        replay = self.journal.replay(ref, key, request)
        if replay is not None:
            return replay
        operation, binding = self._load(ref)
        facts = dict(operation["facts"])
        checks = facts.get("checks", {})
        if (
            not facts.get("launched")
            or checks.get("outcome") not in {"pass", "dev_red", "source_skip"}
            or checks.get("head") != new
            or checked != new
            or "publication_intent" in facts
        ):
            raise InvalidTransition("Exact launch and checked-head facts are required")
        self.authority.check_current(binding)
        facts["publication_intent"] = payload
        return self.journal.commit(
            ref, expected_version, key, "publication_intent", facts, payload, request=request
        )

    @contextmanager
    def authority_boundary(self, ref, *, scope="final_local_git"):
        operation, binding = self._load(ref)
        if scope != "final_local_git" or "publication_intent" not in operation["facts"]:
            raise AccessDenied("Only the registered final-local section is described")
        if any(
            name in operation["facts"]
            for name in ("publication", "publication_uncertain", "result")
        ):
            raise AccessDenied(
                "Observed or uncertain publication requires reconciliation, not replay"
            )
        boundary = getattr(self.authority, "authority_boundary", None)
        if boundary is None:
            raise AccessDenied("The authority-only host port is unavailable")
        self.authority.check_current(binding)
        # F0 exercises this protocol with recording ports only. There is no Git
        # runner, mutation callback or evidence issuer in this component.
        with boundary(binding):
            yield

    def observe(self, ref, expected_version, key, evidence_ref):
        request = ["observation", token(evidence_ref)]
        replay = self.journal.replay(ref, key, request)
        if replay is not None:
            return replay
        operation, binding = self._load(ref)
        if self.observations is None:
            raise AccessDenied("Trusted observation transport is unavailable")
        fact = self.observations.read(evidence_ref, binding)
        if type(fact) is not Observation or fact.evidence_ref != evidence_ref:
            raise AccessDenied("Invalid observation transport result")
        payload = fact.payload(operation, fact.kind)
        shapes = {
            "launched": ({"process_ref", "children"}, set()),
            "checks": ({"head", "outcome"}, set()),
            "publication": ({"old", "new", "checked", "range_ref", "confirmed"}, {"current_dev"}),
            "result": ({"answer"}, set()),
            "content_equal": ({"confirmed"}, set()),
            "database": ({"state"}, set()),
            "test_evidence": ({"coverage", "head"}, {"carried_from", "selected_ref"}),
        }
        if fact.kind not in shapes:
            raise ValidationError("Unknown application observation kind")
        required, optional = shapes[fact.kind]
        if not required <= payload.keys() or payload.keys() - required - optional:
            raise ValidationError("Unexpected observation fields")
        if fact.kind in {"publication", "content_equal"} and type(payload["confirmed"]) is not bool:
            raise ValidationError("Confirmation must be a boolean")
        if "current_dev" in payload:
            sha(payload["current_dev"], 40)
        if fact.kind == "test_evidence":
            sha(payload["head"], 40)
            if "carried_from" in payload:
                sha(payload["carried_from"], 40)
            if "selected_ref" in payload:
                token(payload["selected_ref"])
        facts = dict(operation["facts"])
        if fact.kind == "launched":
            if not facts.get("launch_intent") or facts.get("launched"):
                raise InvalidTransition("Launch observation requires one prior intent")
            token(payload.get("process_ref"))
            if type(payload.get("children")) is not list:
                raise ValidationError("Complete child identities are required")
            for child in payload["children"]:
                token(child)
            if len(set(payload["children"])) != len(payload["children"]):
                raise ValidationError("Child identities must be unique")
            facts["launched"] = payload
        elif fact.kind == "checks":
            if not facts.get("launched"):
                raise InvalidTransition("Checks require a launched operation")
            sha(payload.get("head"), 40)
            if payload.get("outcome") not in {"pass", "fail", "dev_red", "source_skip", "unknown"}:
                raise ValidationError("Unknown check observation")
            facts["checks"] = payload
        elif fact.kind == "publication":
            intent = facts.get("publication_intent")
            if not intent or any(payload.get(k) != v for k, v in intent.items()):
                raise AccessDenied("Publication differs from the final intent")
            if payload.get("confirmed") is True:
                facts["publication"] = dict(intent, evidence_ref=evidence_ref)
                facts["heavy_obligation"] = "pending"
            else:
                facts["publication_uncertain"] = True
        elif fact.kind == "result":
            try:
                answer = Answer(payload.get("answer"))
            except (TypeError, ValueError):
                raise ValidationError("Unknown merge result") from None
            if not facts.get("launched"):
                raise InvalidTransition("Result requires a launch observation")
            if answer == Answer.MERGED and not facts.get("publication"):
                raise InvalidTransition("MERGED requires independently observed publication")
            if answer == Answer.NOTHING_TO_MERGE and not facts.get("content_equal"):
                raise InvalidTransition("Content reconciliation is required")
            facts["result"] = answer.value
            facts["result_evidence"] = evidence_ref
        elif fact.kind == "content_equal":
            if not facts.get("launched") or payload.get("confirmed") is not True:
                raise AccessDenied("Content equality is unconfirmed")
            facts["content_equal"] = evidence_ref
        elif fact.kind == "database":
            if payload.get("state") not in {"running", "stopped", "removed", "warning", "unknown"}:
                raise ValidationError("Unknown resource observation")
            facts["database"] = payload["state"]
        elif fact.kind == "test_evidence":
            if payload.get("coverage") not in {"whole", "selected", "carried", "not_run"}:
                raise ValidationError("Unknown test coverage observation")
            facts["test_evidence"] = dict(payload, evidence_ref=evidence_ref)
        else:
            raise ValidationError("Unknown application observation kind")
        return self.journal.commit(
            ref, expected_version, key, "observed_" + fact.kind, facts, payload, request=request
        )

    def model_ended(self, ref, expected_version, key, reason):
        if reason not in {"model_return", "eof", "window", "disconnect"}:
            raise ValidationError("Unknown model observation")
        operation, _ = self._load(ref)
        facts = dict(operation["facts"], model_observation=reason)
        return self.journal.commit(
            ref,
            expected_version,
            key,
            "model_ended",
            facts,
            {"reason": reason},
            request=["model_ended", reason],
        )

    def reconcile_stopped(self, attempt_id):
        operations = self.journal.for_attempt(token(attempt_id))
        if not operations or self.observations is None:
            return False
        for operation in operations:
            launched = operation["facts"].get("launched")
            if not launched:
                return False
            binding = Binding(**operation["binding"])
            try:
                fact = self.observations.process(binding)
                if type(fact) is not Observation:
                    return False
                payload = fact.payload(operation, "process")
                children = payload.get("children")
                if (
                    payload.get("process_ref") != launched["process_ref"]
                    or payload.get("complete") is not True
                    or payload.get("state") != "quiescent"
                    or type(children) is not dict
                    or set(children) != set(launched["children"])
                    or any(v != "quiescent" for v in children.values())
                ):
                    return False
                facts = dict(operation["facts"], quiescence_evidence=fact.evidence_ref)
                self.journal.commit(
                    operation["ref"],
                    operation["version"],
                    "quiescent:" + fact.evidence_ref,
                    "quiescent_observed",
                    facts,
                    request=["quiescent", fact.evidence_ref],
                )
            except (AccessDenied, ValidationError, KeyError):
                return False
        return True

    def request_stop(self, ref, expected_version, key, decision_ref):
        if self.stop_port is None:
            raise AccessDenied("Authorized stop transport is unavailable")
        operation, binding = self._load(ref)
        request = ["stop_request", token(decision_ref)]
        replay = self.journal.replay(ref, key, request)
        if replay is not None:
            return replay
        # The trusted stop port verifies this opaque decision reference. A model
        # end or current quiescence observation never calls this method.
        self.stop_port.verify(binding, decision_ref)
        facts = dict(operation["facts"], stop_intent=decision_ref)
        receipt = self.journal.commit(
            ref, expected_version, key, "stop_intent", facts, request=request
        )
        self.stop_port.request(binding, decision_ref)
        return receipt

    def execute_registered(self, merge_ref, context):
        raise AccessDenied("Real protected foreground execution is not configured in F0")
