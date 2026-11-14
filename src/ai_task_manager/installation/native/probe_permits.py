"""Accounted canary stages consume real invocation capabilities without a Core lease."""

from dataclasses import asdict
from datetime import UTC, datetime
from threading import Event

from ai_task_manager.adapters.hosts.bridge.conformance import ProbeThreadGate
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.adapters.hosts.bridge.invocations import (
    InitializationInvocation,
    InvocationBinding,
    InvocationPermits,
)
from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.domains.work.errors import AccessDenied

from .admissions import claude_auth_child


class ProbePermits:
    def __init__(self, authority, policy, binding, gateway, journal):
        self.authority, self.policy, self.binding, self.journal = (
            authority,
            policy,
            binding,
            journal,
        )
        self.controller = authority.controller
        self.context = authority.context
        self.probe_thread = ProbeThreadGate(self.context, authority, gateway)
        self.issued = {}
        self.initialization = None
        self._auth_pending = None
        self._identity_next = 0
        self._identity_observer = None
        self._identity_cancelled = Event()

    def check_admission(self, host):
        raise AccessDenied("Canary permission cannot admit a normal Core worker")

    def _claim(self, purpose, name=None, *, not_after=None):
        a, b = self.authority, self.binding
        a.require_current(
            self.context,
            policy=self.policy,
            binding=b,
            phase=(
                "canary"
                if purpose == "assignment"
                else purpose
                if purpose in {"auth", "neutral"}
                else "start"
            ),
        )
        name = name or purpose
        if name in self.issued:
            raise AccessDenied("A canary invocation cannot be replayed")
        parent = self.initialization.native.reference if self.initialization else None
        if purpose == "relay":
            parent = self.issued["host"].reference
        cap = min(b.deadline, not_after if not_after is not None else b.deadline)
        seconds = (
            min(a.permission["limits"][purpose], cap - self.controller.clock().timestamp()) - 0.05
        )
        if seconds <= 0:
            raise AccessDenied("Original probe stage deadline expired")
        call_id = "probe:" + b.worker_id + ":" + name
        self.controller.reserve(
            call_id,
            label="Native capability probe " + name,
            paid=False,
            max_cost_usd=None,
            uses_worker=purpose == "host",
            batch_id=a.reference,
            timeout_seconds=seconds,
            metadata={
                "parent_call_id": parent,
                "billing_basis_ref": "subscription-only:" + a.reference,
                "invocation_binding": {
                    "attempt_id": b.attempt_id,
                    "host": b.host,
                    "configuration_digest": b.configuration_digest,
                    "model": self.policy.model,
                    "effort": self.policy.effort,
                    "purpose": purpose,
                },
            },
        )
        call = self.controller.claim_launch(
            call_id, worker_identity=b.worker_id, not_after=datetime.fromtimestamp(cap, UTC)
        )
        observed = InvocationBinding.observed(
            {"run_id": self.controller.snapshot()["run_id"], "policy_revision": 1, "call": call}
        )
        permit = InvocationPermits(self.controller, {call_id: observed}).prepare_launch(call_id)
        self.issued[name] = permit
        return permit

    def authorize_initialization(self, policy, binding):
        if (
            policy is not self.policy
            or binding is not self.binding
            or self.initialization is not None
        ):
            raise AccessDenied("Probe initialization owner changed or was already used")
        host = self._claim("host")
        relay = self._claim("relay", not_after=host.binding.deadline)
        self.initialization = InitializationInvocation(host, relay)
        return self.initialization

    def authorize_auth(self, policy, binding):
        if (
            policy is not self.policy
            or binding is not self.binding
            or self._auth_pending is not None
        ):
            raise AccessDenied("Probe authentication owner changed")
        name, cap = "auth", None
        if self._identity_observer is not None:
            refs = self.authority.permission["identity_refreshes"]
            if self._identity_next >= len(refs):
                raise AccessDenied("Finite probe identity-auth plan exhausted")
            name = refs[self._identity_next]
            self._identity_next += 1
            cap = self._identity_observer.prepare().observation.expires_at
        self._auth_pending = self._claim("auth", name, not_after=cap)
        return self._auth_pending

    def record_auth(self, binding, completion):
        if binding is not self.binding or self._auth_pending is None:
            raise AccessDenied("Actual probe auth completion has no pending owner")
        permit = self._auth_pending
        self._record(
            permit,
            completion["outcome"],
            completion["group"]["confirmed_stop"],
            effect="accounting_auth",
            completion=completion,
            cancelled=self._identity_cancelled,
        )
        self._auth_pending = None

    def authorize_neutral_start(self, policy, binding):
        if (
            policy is not self.policy
            or binding is not self.binding
            or not claude_auth_child(
                self.controller, self.authority.manifest, binding, self.journal
            )
        ):
            raise AccessDenied("Probe neutral input requires actual matching subscription auth")
        return self._claim("neutral")

    def _record(self, permit, outcome, stopped, *, effect, cancelled=None, **data):
        if permit not in self.issued.values():
            raise AccessDenied("Probe outcome belongs to another invocation")
        permit.issuer._validate(permit)
        ref = self.journal._append("trace", {"effect": effect, "call_id": permit.reference, **data})
        if permit.issuer.controller is not self.controller:
            raise AccessDenied("Probe outcome controller changed")
        if effect == "accounting_auth" and stopped:
            permit.issuer.record_outcome(
                permit, outcome=outcome, process_stopped=stopped, evidence_ref=ref,
                cancelled=cancelled,
            )
        else:
            self.controller.record_outcome(
                permit.reference, outcome=outcome, process_stopped=stopped, evidence_ref=ref
            )

    def record_initialization(self, permit, *, outcome, observation_ref, detail):
        if permit is not self.initialization:
            raise AccessDenied("Probe initialization differs")
        reference = self.journal._append(
            "trace",
            {
                "effect": "native_probe_initialization",
                "outcome": outcome,
                "observation_ref": observation_ref,
                "detail": detail,
            },
        )
        if outcome == "failed_or_uncertain" and observation_ref is None:
            identities = {permit.native.reference, permit.relay.reference}
            rows = [
                row for row in self.controller.snapshot()["calls"] if row["call_id"] in identities
            ]
            if len(rows) == 2 and all(
                row["status"] == "launching"
                and row["started_at"] is None
                and row["launch_token"] is None
                and row["process_pid"] is None
                and row["process_start_identity"] is None
                and not row["process_stopped"]
                for row in rows
            ):
                self.controller._abort_unreturned_claims(
                    rows, worker_identity=self.binding.worker_id, evidence_ref=reference
                )

    def record_neutral_start(self, permit, *, outcome, result, observation_ref):
        self._record(
            permit,
            "succeeded" if outcome == "observed_turn" else "unknown",
            outcome == "observed_turn",
            effect="native_probe_neutral",
            observation_ref=observation_ref,
        )

    def record_host_stop(self, binding, stop):
        if (
            binding is not self.binding
            or not isinstance(stop, GroupStop)
            or (stop.confirmed and (not stop.observation.known or stop.observation.live_members))
        ):
            raise AccessDenied("Actual owned probe stop observation required")
        calls = {row["call_id"]: row for row in self.controller.snapshot()["calls"]}
        for permit in self.issued.values():
            row = calls[permit.reference]
            if not row["process_stopped"]:
                outcome = row["outcome"] or (
                    "succeeded" if row["status"] == "running" else "failed"
                )
                self._record(
                    permit,
                    outcome if stop.confirmed else "unknown",
                    stop.confirmed,
                    effect="native_probe_stop",
                    stop=asdict(stop),
                )

    def identity_observations(self, native):
        refs = self.authority.permission["identity_refreshes"]
        if not refs:
            return None
        observer = NativeIdentityObservations(native, self, not_after=self.context.deadline)
        self._identity_observer = observer
        return IdentityRefresh(
            observer,
            native.directory / "probe-identity",
            len(refs),
            on_failure=native.identity_failed,
            cancelled=self._identity_cancelled,
        )
