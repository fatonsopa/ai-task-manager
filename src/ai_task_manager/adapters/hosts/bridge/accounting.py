"""Compose real invocation accounting with Core and runtime ownership.

This adapter issues accounting capabilities only. Native restrictions, subscription
authentication, provider status and decision authority remain independent ports.
"""

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Lock, get_ident

from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.domains.work.errors import AccessDenied

from .invocations import InitializationInvocation, InvocationBinding, InvocationPermits


@dataclass(frozen=True)
class AccountingPlan:
    batch_id: str
    call_prefix: str
    task_id: str
    host: str
    model: str
    effort: str
    billing_basis_ref: str
    deadline: float
    host_seconds: float
    auth_seconds: float
    neutral_seconds: float
    assignment_seconds: float
    assignments: tuple[str, ...] = ("assignment",)

    def __post_init__(self):
        strings = (
            self.batch_id,
            self.call_prefix,
            self.task_id,
            self.model,
            self.effort,
            self.billing_basis_ref,
            *self.assignments,
        )
        bounds = (
            self.deadline,
            self.host_seconds,
            self.auth_seconds,
            self.neutral_seconds,
            self.assignment_seconds,
        )
        if (
            self.host not in {"claude", "codex"}
            or any(not isinstance(x, str) or not x or "\x00" in x for x in strings)
            or not isinstance(self.assignments, tuple)
            or not self.assignments
            or len(set(self.assignments)) != len(self.assignments)
            or any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                or x <= 0
                for x in bounds
            )
        ):
            raise AccessDenied("Accounting needs an exact finite trusted plan")


class HostInvocationAccounting:
    """One attempt per instance, using an already registered finite controller batch.

    The trusted admission port must verify both native restrictions and the
    included-subscription billing basis. It must independently verify fresh auth
    before neutral model input. No default admission or public receipt issuer exists.
    """

    def __init__(self, controller, core, handle, runtime, journal, plan, *, admissions):
        if (
            not isinstance(plan, AccountingPlan)
            or not callable(getattr(admissions, "check_admission", None))
            or not callable(getattr(admissions, "verify_authenticated", None))
            or not callable(getattr(admissions, "verify_policy", None))
        ):
            raise AccessDenied("A trusted native admission and authentication verifier is required")
        state = controller.snapshot()
        if state["policy"]["revision"] != 1:
            raise AccessDenied("This adapter requires the reviewed accounting policy revision")
        self.control, self.core, self.handle = controller, core, handle
        self.runtime, self.journal, self.plan = runtime, journal, plan
        self.admissions = admissions
        self._bound = (controller, core, handle, runtime, journal, plan, admissions)
        self._owner_binding = None
        self._calls, self._permits = {}, {}
        self._initialization = None
        self._initialization_lock = Lock()
        self._initialization_attempted = False
        self._initialization_token = None
        self._initialization_thread = None

    def _current(self):
        if any(
            a is not b
            for a, b in zip(
                (
                    self.control,
                    self.core,
                    self.handle,
                    self.runtime,
                    self.journal,
                    self.plan,
                    self.admissions,
                ),
                self._bound,
            )
        ):
            raise AccessDenied("The accounting composition changed")

    def check_admission(self, host):
        self._current()
        if host != self.plan.host or self.control.clock().timestamp() >= self.plan.deadline:
            raise AccessDenied("The registered host or immutable accounting deadline differs")
        self.admissions.check_admission(self.plan)

    def _owner(self, policy, binding):
        self.check_admission(binding.host)
        session, scope = self.core.session_binding(self.handle)
        attempt = self.core.get_attempt(self.handle, binding.attempt_id)
        owner = self.runtime.attempt(binding.attempt_id)
        if (
            (scope, session, attempt.task_id, attempt.session_id, attempt.host)
            != (
                binding.scope,
                binding.core_session,
                self.plan.task_id,
                binding.core_session,
                binding.host,
            )
            or owner["scope"] != asdict(scope)
            or (owner["task_id"], owner["worker_id"], owner["host"], owner["deadline"])
            != (self.plan.task_id, binding.worker_id, binding.host, binding.deadline)
            or (policy.host, policy.model, policy.effort, policy.configuration_digest)
            != (self.plan.host, self.plan.model, self.plan.effort, binding.configuration_digest)
            or binding.deadline > self.plan.deadline
            or (self._owner_binding is not None and self._owner_binding != binding)
        ):
            raise AccessDenied("Accounting differs from the actual Core/runtime owner")
        self._owner_binding = binding
        self.admissions.verify_policy(policy, binding, self.plan, self.control)
        return attempt

    def _claim(
        self,
        policy,
        binding,
        purpose,
        seconds,
        *,
        reference=None,
        worker=False,
        parent=None,
    ):
        self._owner(policy, binding)
        key = reference or purpose
        if key in self._calls:
            raise AccessDenied("This invocation was already claimed; reconcile rather than retry")
        remaining = min(binding.deadline, self.plan.deadline) - self.control.clock().timestamp()
        # The persisted launch deadline is established by claim_launch, slightly
        # after this observation. Reserve a bounded preparation margin, never extend.
        maximum = min(seconds, remaining) - 0.05
        if maximum <= 0:
            raise AccessDenied("The immutable accounting window expired")
        call_id = self.plan.call_prefix + ":" + binding.attempt_id + ":" + key
        self.control.reserve(
            call_id,
            label="Managed " + purpose,
            paid=False,
            max_cost_usd=None,
            uses_worker=worker,
            batch_id=self.plan.batch_id,
            timeout_seconds=maximum,
            metadata={
                "parent_call_id": parent,
                "billing_basis_ref": self.plan.billing_basis_ref,
                "invocation_binding": {
                    "attempt_id": binding.attempt_id,
                    "host": binding.host,
                    "configuration_digest": binding.configuration_digest,
                    "model": policy.model,
                    "effort": policy.effort,
                    "purpose": purpose,
                },
            },
        )
        # Retain ownership even if claiming or decoding fails after reservation.
        self._calls[key] = call_id
        call = self.control.claim_launch(
            call_id,
            worker_identity=binding.worker_id,
            not_after=datetime.fromtimestamp(min(binding.deadline, self.plan.deadline), UTC),
        )
        state = self.control.snapshot()
        observed = InvocationBinding.observed(
            {
                "run_id": state["run_id"],
                "policy_revision": state["policy"]["revision"],
                "call": call,
            }
        )
        if observed.deadline > binding.deadline:
            raise AccessDenied("Persisted invocation exceeds its original host deadline")
        permit = InvocationPermits(self.control, {call_id: observed}).prepare_launch(call_id)
        self._permits[call_id] = permit
        return permit

    def authorize_initialization(self, policy, binding):
        if not self._initialization_lock.acquire(blocking=False):
            raise AccessDenied("Native initialization authorization is already in progress")
        try:
            if self._initialization_attempted:
                raise AccessDenied("Native initialization authorization was already attempted")
            attempt = self._owner(policy, binding)
            if attempt.state != "reserved" or self._initialization is not None:
                raise AccessDenied("Native initialization requires the reserved Core attempt")
            self._policy = policy
            self._initialization_attempted = True
            token = self._initialization_token = object()
            self._initialization_thread = get_ident()
            try:
                host = self._claim(policy, binding, "host", self.plan.host_seconds, worker=True)
                relay = self._claim(
                    policy,
                    binding,
                    "relay",
                    host.binding.deadline - self.control.clock().timestamp(),
                    parent=host.binding.call_id,
                )
                self._initialization = InitializationInvocation(host, relay)
                return self._initialization
            except BaseException as error:
                try:
                    self._abort_unreturned_initialization(binding, token, type(error).__name__)
                except BaseException as abort_error:
                    error.initialization_abort_error = type(abort_error).__name__
                raise
            finally:
                self._initialization_token = None
                self._initialization_thread = None
        finally:
            self._initialization_lock.release()

    def _abort_unreturned_initialization(self, binding, token, reason):
        """Only this producer's pre-return catch can attest that it handed out no launch."""
        self._current()
        if (
            token is None
            or token is not self._initialization_token
            or self._initialization_thread != get_ident()
            or not self._initialization_lock.locked()
            or self._initialization is not None
            or self._owner_binding != binding
        ):
            raise AccessDenied("Initialization abort is outside its original pre-return owner")
        snapshot = {row["call_id"]: row for row in self.control.snapshot()["calls"]}
        owned = []
        for purpose in ("host", "relay"):
            call_id = self._calls.get(purpose)
            if call_id is None:
                continue
            row = snapshot[call_id]
            expected = {
                "attempt_id": binding.attempt_id,
                "host": binding.host,
                "configuration_digest": binding.configuration_digest,
                "model": self._policy.model,
                "effort": self._policy.effort,
                "purpose": purpose,
            }
            if (
                row["metadata"].get("invocation_binding") != expected
                or row["metadata"].get("billing_basis_ref") != self.plan.billing_basis_ref
                or row["metadata"].get("parent_call_id")
                != (self._calls.get("host") if purpose == "relay" else None)
                or row["paid"]
                or row["uses_worker"] is not (purpose == "host")
                or row["status"] not in {"reserved", "launching"}
                or row["worker_identity"]
                != (None if row["status"] == "reserved" else binding.worker_id)
                or row["worker_state"] != ("reserved" if purpose == "host" else "released")
                or row["process_stopped"]
                or any(
                    row.get(key) is not None
                    for key in ("started_at", "process_pid", "process_start_identity")
                )
            ):
                raise AccessDenied("Started, changed or uncertain initialization remains held")
            owned.append(row)
        if not owned:
            return
        evidence = self.journal._append(
            "trace",
            {
                "effect": "initialization_authorization_aborted_before_return",
                "binding": asdict(binding),
                "reason": reason,
                "calls": owned,
                "native_launch_capability_returned": False,
                "group_stop": "not_issued; no native construction was reached",
            },
        )
        self.control._abort_unreturned_claims(
            owned, worker_identity=binding.worker_id, evidence_ref=evidence
        )

    def _evidence(self, effect, **data):
        self._current()
        return self.journal._append("trace", {"effect": effect, **data})

    def record_initialization(self, permit, *, outcome, observation_ref, detail):
        if permit is not self._initialization:
            raise AccessDenied("Initialization belongs to another accounting owner")
        self._evidence(
            "accounting_initialization",
            outcome=outcome,
            observation_ref=observation_ref,
            detail=detail,
        )
        # Startup observation is not host/group stop and releases no worker slot.

    def authorize_auth(self, policy, binding):
        if self._initialization is None:
            raise AccessDenied("Authentication needs a registered initialization owner")
        return self._claim(
            policy,
            binding,
            "auth",
            self.plan.auth_seconds,
            parent=self._initialization.native.reference,
        )

    def record_auth(self, binding, completion):
        if binding != self._owner_binding:
            raise AccessDenied("Authentication belongs to another runtime owner")
        call_id = self._calls["auth"]
        confirmed = completion["group"]["confirmed_stop"]
        if type(confirmed) is not bool:
            raise AccessDenied("Authentication stop must be explicitly observed")
        ref = self._evidence("accounting_auth", call_id=call_id, completion=completion)
        self.control.record_outcome(
            call_id,
            outcome=completion["outcome"],
            process_stopped=confirmed,
            evidence_ref=ref,
        )

    def authorize_neutral_start(self, policy, binding):
        self._owner(policy, binding)
        self.admissions.verify_authenticated(policy, binding, self.journal)
        return self._claim(
            policy,
            binding,
            "neutral",
            self.plan.neutral_seconds,
            parent=self._calls["host"],
        )

    def require_native_conformance(self, policy, binding):
        self._owner(policy, binding)
        verify = getattr(self.admissions, "require_native_conformance", None)
        if not callable(verify):
            raise AccessDenied("Actual pinned native-tool conformance is unavailable")
        return verify(policy, binding, self.journal)

    def record_neutral_start(self, permit, *, outcome, result, observation_ref):
        self._record_turn(permit, outcome, detail=None, observation_ref=observation_ref)

    def authorize(self, reference, attempt):
        self._current()
        if reference not in self.plan.assignments or self._owner_binding is None:
            raise AccessDenied("The assigned model reference is not registered")
        binding = self._owner_binding
        if attempt.id != binding.attempt_id:
            raise AccessDenied("The assigned model belongs to another attempt")
        self.core.execution.validate_work_lease(self.handle, attempt.id, attempt.version)
        # Reuse the exact policy observed during host initialization, not model data.
        policy = self._policy
        return self._claim(
            policy,
            binding,
            "assignment",
            self.plan.assignment_seconds,
            reference="assignment:" + reference,
            parent=self._calls["host"],
        )

    def record(self, permit, *, outcome, detail):
        self._record_turn(permit, outcome, detail=detail)

    def _record_turn(self, permit, outcome, *, detail, observation_ref=None):
        self._current()
        if self._permits.get(permit.binding.call_id) is not permit:
            raise AccessDenied("Model completion belongs to another issued invocation")
        if permit.binding.purpose not in {"neutral", "assignment"}:
            raise AccessDenied("This invocation is not a model turn")
        if not self.journal.intend(
            "accounting-outcome:" + permit.reference,
            {"call_id": permit.reference, "outcome": outcome},
        ):
            raise AccessDenied("Existing completion needs reconciliation")
        ref = self._evidence(
            "accounting_turn",
            call_id=permit.reference,
            outcome=outcome,
            detail=detail,
            observation_ref=observation_ref,
        )
        observed = outcome == "observed_turn"
        self.control.record_outcome(
            permit.reference,
            outcome="succeeded" if observed else "unknown",
            process_stopped=observed,
            evidence_ref=ref,
        )

    def record_host_stop(self, binding, stop):
        self._current()
        if (
            binding != self._owner_binding
            or not isinstance(stop, GroupStop)
            or type(stop.confirmed) is not bool
            or (stop.confirmed and (not stop.observation.known or stop.observation.live_members))
        ):
            raise AccessDenied("Host stop belongs to another observed owner")
        ref = self._evidence(
            "accounting_host_stop", attempt_id=binding.attempt_id, stop=asdict(stop)
        )
        calls = {row["call_id"]: row for row in self.control.snapshot()["calls"]}
        for call_id in self._calls.values():
            call = calls[call_id]
            if call["process_stopped"]:
                continue
            terminal = "succeeded" if call["status"] == "running" else "failed"
            outcome = call["outcome"] or (terminal if stop.confirmed else "unknown")
            self.control.record_outcome(
                call_id,
                outcome=outcome,
                process_stopped=stop.confirmed,
                evidence_ref=ref,
            )
