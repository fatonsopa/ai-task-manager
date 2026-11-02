"""Durable callback delegation; no task status, prose or provider decisions."""

from dataclasses import asdict

from ai_task_manager.domains.work.errors import (
    AccessDenied,
    InvalidTransition,
    ValidationError,
)

from .contracts import Binding, Callback, Receipt, token


class Callbacks:
    def __init__(self, journal, downstream=None, observations=None):
        self.journal, self.downstream, self.observations = journal, downstream, observations

    def submit(self, callback, expected_version):
        if type(callback) is not Callback:
            raise ValidationError("A typed callback envelope is required")
        operation = self.journal.current(callback.operation_ref)
        callback.validate(operation)
        request = ["callback", asdict(callback)]
        key = "callback:" + str(callback.sequence)
        replay = self.journal.replay(callback.operation_ref, key, request)
        if replay is not None:
            return replay
        facts = dict(operation["facts"])
        if callback.sequence != facts.get("callback_sequence", 0) + 1:
            raise InvalidTransition("Callback sequence is out of order")
        facts["callback_sequence"] = callback.sequence
        if callback.kind == "rolled_back":
            facts["followup_obligation"] = {
                "sequence": callback.sequence,
                "state": "pending_review",
            }
        return self.journal.commit(
            callback.operation_ref,
            expected_version,
            key,
            "callback_queued",
            facts,
            {"sequence": callback.sequence},
            (asdict(callback),),
            request=request,
        )

    def dispatch(self, ref, sequence):
        if self.downstream is None or self.observations is None:
            raise AccessDenied("Reviewed downstream transport is unavailable")
        callback = self.journal.callback(ref, sequence)
        # Exact replay returns the original intent, never a second transport send.
        intent, claimed = self.journal.callback_transition(
            ref, sequence, "queued", "sending", "dispatch:" + str(sequence), claim=True
        )
        if not claimed:
            return intent
        try:
            receipt_ref = self.downstream.accept(
                Callback(**callback["envelope"]), "dispatch:" + ref + ":" + str(sequence)
            )
        except Exception:
            # No exception text or callback content is retained as a diagnostic.
            self.journal.callback_transition(
                ref, sequence, "sending", "uncertain", "dispatch-uncertain:" + str(sequence)
            )
        else:
            try:
                self.settle(ref, sequence, receipt_ref)
            except Exception:
                # The downstream may already have accepted an effect. A bad or
                # absent receipt does not turn that uncertainty into a denial.
                self.journal.callback_transition(
                    ref, sequence, "sending", "uncertain", "dispatch-unreadable:" + str(sequence)
                )
        return intent

    def settle(self, ref, sequence, evidence_ref):
        if self.observations is None:
            raise AccessDenied("Trusted receipt transport is unavailable")
        operation = self.journal.current(ref)
        receipt = self.observations.receipt(token(evidence_ref), Binding(**operation["binding"]))
        if (
            type(receipt) is not Receipt
            or receipt.operation_ref != ref
            or receipt.binding_digest != operation["fingerprint"]
            or type(receipt.sequence) is not int
            or receipt.sequence != sequence
            or receipt.evidence_ref != evidence_ref
        ):
            raise AccessDenied("Receipt does not match the callback")
        before = {
            "denied": "sending",
            "accepted_intent": "sending",
            "applied": "accepted_intent",
            "provider_confirmed": "applied",
            "lead_delivered": "provider_confirmed",
            "ai_ack": "lead_delivered",
            "user_reported": "ai_ack",
        }
        if receipt.stage not in before:
            raise ValidationError("Unknown downstream receipt stage")
        # Old-stage receipts replay their original receipt after later advancement.
        # An uncertain send can only recover through an authenticated receipt.
        state = self.journal.callback(ref, sequence)["state"]
        expected = (
            "uncertain"
            if state == "uncertain" and receipt.stage in {"accepted_intent", "denied"}
            else before[receipt.stage]
        )
        return self.journal.callback_transition(
            ref, sequence, expected, receipt.stage, "receipt:" + evidence_ref, evidence_ref
        )
