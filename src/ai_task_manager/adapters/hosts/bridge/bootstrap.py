"""Explicit diagnostic startup, with accounting but no Core worker authority."""

from dataclasses import asdict

from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.config.evaluation_records import EvaluationRecords
from ai_task_manager.domains.work.errors import AccessDenied

from .controller import ManagedNativeHost
from .invocations import InitializationInvocation, PreparedInvocation, require_invocation


class BootstrapReview:
    """Consume a lead-installed diagnostic review; hashes do not prove user origin."""

    def __init__(self, records, review_ref, initialization, template):
        if not isinstance(initialization, InitializationInvocation) or not isinstance(
            records, EvaluationRecords
        ):
            raise AccessDenied("Diagnostic review needs actual registered root capabilities")
        self.records, self.review_ref = records, review_ref
        self.initialization, self.template = initialization, template
        self._bound = (records, review_ref, initialization, template)

    def require_bootstrap(self, policy, binding):
        if any(
            a is not b
            for a, b in zip(
                (self.records, self.review_ref, self.initialization, self.template),
                self._bound,
            )
        ):
            raise AccessDenied("The lead-installed diagnostic review changed")
        permit = self.initialization.native
        controller = permit.issuer.controller
        snapshot = controller.snapshot()
        reviewed = self.records.resolve(
            self.review_ref, "native_bootstrap_review", snapshot["run_id"]
        )
        data = reviewed.data
        expected = {
            "version",
            "purpose",
            "binding",
            "template",
            "batch_id",
            "expires_at",
            "billing_basis_ref",
            "case_id",
        }
        if (
            set(data) != expected
            or data["version"] != 1
            or data["purpose"] != "isolated-native-startup-diagnostic"
            or data["binding"] != asdict(binding)
            or data["template"] != self.template.fingerprint
            or not data["billing_basis_ref"]
            or not data["case_id"]
            or isinstance(data["expires_at"], bool)
            or data["expires_at"] > min(binding.deadline, permit.binding.deadline)
            or data["expires_at"] <= controller.clock().timestamp()
            or permit.binding.run_id != snapshot["run_id"]
            or permit.binding.policy_revision != snapshot["policy"]["revision"]
        ):
            raise AccessDenied("The exact bounded native diagnostic was not authorized")
        self.template.check(policy)
        call = next((row for row in snapshot["calls"] if row["call_id"] == permit.reference), None)
        if (
            call is None
            or call["status"] not in {"launching", "running"}
            or call["invocation_bound"]["batch_id"] != data["batch_id"]
        ):
            raise AccessDenied("The reviewed diagnostic is outside its actual current batch")
        current = (
            permit.issuer.prepare_launch(permit.reference)
            if call["status"] == "launching"
            else permit.running()
        )
        if current.binding != permit.binding:
            raise AccessDenied("The original diagnostic invocation binding changed")


class BootstrapPermits:
    """Consume installer-registered capabilities; never reserve or mint authority.

    The installer supplies a source-bound diagnostic authorization port. This
    object cannot be used as a normal ManagedHostFactory admission verifier.
    """

    def __init__(self, initialization, auth, neutral, authorization, journal):
        if (
            not isinstance(initialization, InitializationInvocation)
            or not (isinstance(auth, PreparedInvocation) or callable(auth))
            or not (isinstance(neutral, PreparedInvocation) or callable(neutral))
            or not isinstance(authorization, BootstrapReview)
        ):
            raise AccessDenied("Diagnostic startup needs registered typed invocations")
        self.initialization, self.auth, self.neutral = initialization, auth, neutral
        self.authorization, self.journal = authorization, journal
        self._original = (initialization, auth, neutral, authorization, journal)
        self._binding = None
        self._claimed = set()
        self._issued = {}

    def _current(self):
        if any(
            a is not b
            for a, b in zip(
                (self.initialization, self.auth, self.neutral, self.authorization, self.journal),
                self._original,
            )
        ):
            raise AccessDenied("Diagnostic accounting composition changed")

    def check_admission(self, host):
        raise AccessDenied("Diagnostic authority cannot admit a normal Core worker")

    def _take(self, permit, policy, binding, purpose):
        self._current()
        self.authorization.require_bootstrap(policy, binding)
        if self._binding is not None and binding != self._binding:
            raise AccessDenied("Diagnostic invocation belongs to another owner")
        self._binding = binding
        # A trusted installer may defer reservation until this actual stage is
        # needed. Its return must still be a genuine scoped root capability.
        permit = permit() if callable(permit) else permit
        require_invocation(permit, policy, binding, purpose)
        host = self.initialization.native
        if permit.issuer.controller is not host.issuer.controller:
            raise AccessDenied("Diagnostic calls require the same current controller")
        if permit.reference in self._claimed:
            raise AccessDenied("Diagnostic invocation was already consumed")
        self._claimed.add(permit.reference)
        self._issued[purpose] = permit
        return permit

    def authorize_initialization(self, policy, binding):
        self._take(self.initialization.native, policy, binding, "host")
        self._take(self.initialization.relay, policy, binding, "relay")
        return self.initialization

    def authorize_auth(self, policy, binding):
        return self._take(self.auth, policy, binding, "auth")

    def authorize_neutral_start(self, policy, binding):
        # The native initializer sends only its fixed tool-free READY request.
        return self._take(self.neutral, policy, binding, "neutral")

    def _record(self, permit, outcome, stopped, **observations):
        self._current()
        if permit.reference not in self._claimed:
            raise AccessDenied("Unconsumed diagnostic call has no observed outcome")
        ref = self.journal._append(
            "trace",
            {
                "effect": "diagnostic_invocation_outcome",
                "call_id": permit.reference,
                "outcome": outcome,
                "process_stopped": stopped,
                **observations,
            },
        )
        permit.issuer.controller.record_outcome(
            permit.reference,
            outcome=outcome,
            process_stopped=stopped,
            evidence_ref=ref,
        )

    def record_auth(self, binding, completion):
        if binding != self._binding:
            raise AccessDenied("Authentication belongs to another diagnostic owner")
        self._record(
            self._issued["auth"],
            completion["outcome"],
            completion["group"]["confirmed_stop"],
            completion=completion,
        )

    def record_initialization(self, permit, *, outcome, observation_ref, detail):
        self._current()
        if permit is not self.initialization:
            raise AccessDenied("Initialization belongs to another diagnostic owner")
        self.journal._append(
            "trace",
            {
                "effect": "diagnostic_initialization",
                "outcome": outcome,
                "observation_ref": observation_ref,
                "detail": detail,
                "core_worker_authority": "none",
                "full_conformance": "not_established",
            },
        )

    def record_neutral_start(self, permit, *, outcome, result, observation_ref):
        if permit is not self._issued.get("neutral"):
            raise AccessDenied("Neutral turn belongs to another diagnostic owner")
        done = outcome == "observed_turn"
        self._record(
            permit, "succeeded" if done else "unknown", done, observation_ref=observation_ref
        )

    def record_host_stop(self, binding, stop):
        self._current()
        if binding != self._binding or not isinstance(stop, GroupStop):
            raise AccessDenied("Diagnostic stop needs its actual owned-group observation")
        if stop.confirmed and (not stop.observation.known or stop.observation.live_members):
            raise AccessDenied("Contradictory diagnostic stop cannot release capacity")
        controller = self.initialization.native.issuer.controller
        calls = {row["call_id"]: row for row in controller.snapshot()["calls"]}
        for permit in self._issued.values():
            if permit.reference not in self._claimed:
                continue
            call = calls[permit.reference]
            if call["process_stopped"]:
                continue
            terminal = "succeeded" if call["status"] == "running" else "failed"
            outcome = call["outcome"] or (terminal if stop.confirmed else "unknown")
            self._record(permit, outcome, stop.confirmed, owned_group_stop=asdict(stop))


class DiagnosticBootstrap:
    """Initialize a real owned host without reserving/starting a Core worker.

    The complete installer-reviewed source is the trust boundary. Arbitrary
    same-user Python access is not confined by these object identity checks.
    """

    def __init__(self, native, observations):
        if (
            not isinstance(native, ManagedNativeHost)
            or not isinstance(native.permits, BootstrapPermits)
            or native.core is not None
            or native.resources is not None
            or native.gateway.delegate is not None
            or native.binding.host != "claude"
            or not native.binding.core_session.startswith("diagnostic:")
            or not native.binding.attempt_id.startswith("diagnostic:")
        ):
            raise AccessDenied("Only the supported isolated Claude diagnostic is configured")
        self.native, self.observations = native, observations
        self._started = False

    def start(self):
        if self._started:
            raise AccessDenied("A diagnostic cannot be restarted or extend its deadline")
        self._started = True
        ready = self.native.initialize()
        if self.native.gateway.delegate is not None or self.native.startup.admitted:
            raise AccessDenied("Diagnostic startup acquired unexpected work authority")
        return self.observations.capture(self.native, ready)

    def stop(self):
        return self.native.stop()
