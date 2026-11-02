"""Explicit test-only recording/fault ports; never installed in product composition."""

from contextlib import contextmanager
from dataclasses import replace
from itertools import count

import pytest

from ai_task_manager.adapters.projects.ai_index.callbacks import Callbacks
from ai_task_manager.adapters.projects.ai_index.contracts import (
    Binding,
    Callback,
    Observation,
    Receipt,
    encode,
)
from ai_task_manager.adapters.projects.ai_index.journal import Journal
from ai_task_manager.adapters.projects.ai_index.operations import Operations
from ai_task_manager.adapters.projects.ai_index.registrations import Registrations
from ai_task_manager.domains.work.errors import AccessDenied

OLD, NEW = "a" * 40, "b" * 40


class AuthorityDouble:
    def __init__(self, binding):
        self.allowed, self.active, self.ready = binding, True, True
        self.log = []

    def registration(self, binding):
        if binding != self.allowed:
            raise AccessDenied("Unregistered test binding")

    def before_start(self, binding):
        self.registration(binding)
        self.log.append("before_start")
        if not self.ready:
            raise AccessDenied("Test resource is not ready")

    def check_current(self, binding):
        self.registration(binding)
        self.log.append("check_current")
        if not self.active:
            raise AccessDenied("Test work authority revoked")

    @contextmanager
    def authority_boundary(self, binding):
        self.check_current(binding)
        self.log.append("authority_enter")
        try:
            yield
        finally:
            self.log.append("authority_exit")


class ObservationDouble:
    def __init__(self):
        self.facts, self.receipts = {}, {}
        self.process_fact = None

    def read(self, ref, binding):
        return self.facts[ref]

    def process(self, binding):
        return self.process_fact

    def receipt(self, ref, binding):
        if ref not in self.receipts:
            raise AccessDenied("No test receipt")
        return self.receipts[ref]


class FaultDouble:
    def __init__(self):
        self.stage = None

    def hit(self, stage):
        if stage == self.stage:
            raise RuntimeError("Injected journal interruption")


class StopDouble:
    def __init__(self):
        self.calls = []

    def verify(self, binding, decision_ref):
        if decision_ref != "authorized-stop":
            raise AccessDenied("No scoped stop decision")

    def request(self, binding, decision_ref):
        self.calls.append((binding.attempt_id, decision_ref))


class DownstreamDouble:
    def __init__(self, harness):
        self.harness, self.calls, self.effects = harness, [], []
        self.mode = "accept"
        # This independently declared work set belongs to the downstream double,
        # not to an adapter status/completion algorithm.
        self.required = {"part-a", "part-b"}
        self.completed = {"part-a"}

    def accept(self, callback, operation_key):
        self.calls.append((callback, operation_key))
        if (
            self.mode == "deny"
            or callback.approval_ref != "approved"
            or (callback.part_id == "partial" and self.completed != self.required)
            or callback.result_ref in {"selected", "carried", "not_run"}
        ):
            return self.harness.receipt("denied", callback.sequence)
        self.effects.append(callback.kind)
        ref = self.harness.receipt("accepted_intent", callback.sequence)
        if self.mode == "foreign_receipt":
            self.harness.observations.receipts[ref] = replace(
                self.harness.observations.receipts[ref], binding_digest="f" * 64
            )
        if self.mode == "effect_then_access_denied":
            raise AccessDenied("Injected guard exception after possible effect")
        if self.mode == "uncertain":
            raise RuntimeError("Injected lost downstream reply")
        return ref


class Harness:
    def __init__(self, path):
        self.binding = Binding(
            "task",
            "attempt",
            "principal",
            "session",
            "team",
            "project",
            "issue",
            "c" * 64,
            "d" * 64,
            "tree",
            "common",
            "stack",
            "merge-command",
            "operation",
        )
        self.fault = FaultDouble()
        self.journal = Journal(path, faults=self.fault)
        self.authority = AuthorityDouble(self.binding)
        self.observations = ObservationDouble()
        self.stop = StopDouble()
        self.registrations = Registrations(self.journal, self.authority)
        self.registration = self.registrations.register(self.binding)
        self.ref = self.registration["ref"]
        self.operations = Operations(self.journal, self.authority, self.observations, self.stop)
        self.downstream = DownstreamDouble(self)
        self.callbacks = Callbacks(self.journal, self.downstream, self.observations)
        self.serial = count(1)

    @property
    def current(self):
        return self.journal.current(self.ref)

    def version(self):
        return self.current["version"]

    def fact(self, kind, payload, ref=None):
        ref = ref or "observation:" + str(next(self.serial))
        self.observations.facts[ref] = Observation(
            self.ref, self.binding.fingerprint, ref, kind, encode(payload)
        )
        return ref

    def observe(self, kind, payload, ref=None):
        evidence = self.fact(kind, payload, ref)
        return self.operations.observe(self.ref, self.version(), "record:" + evidence, evidence)

    def launch(self, outcome="pass"):
        self.operations.intend_launch(self.ref, self.version(), "launch")
        self.observe("launched", {"process_ref": "process", "children": ["child"]})
        self.observe("checks", {"head": NEW, "outcome": outcome})

    def prepare(self):
        return self.operations.prepare_publication(
            self.ref, self.version(), "prepare", old=OLD, new=NEW, checked=NEW, range_ref="range"
        )

    def publish(self, outcome="pass"):
        self.launch(outcome)
        self.prepare()
        with self.operations.authority_boundary(self.ref):
            pass  # A recording contract only: no real Git effect or bound.
        self.observe(
            "publication",
            {"old": OLD, "new": NEW, "checked": NEW, "range_ref": "range", "confirmed": True},
        )

    def callback(self, **changes):
        cb = Callback(
            self.ref,
            self.binding.fingerprint,
            self.binding.source_digest,
            self.binding.issue_id,
            self.binding.team_id,
            self.binding.principal_id,
            1,
            "landed",
            "range",
            NEW,
            "e" * 64,
            "approved",
            "whole",
        )
        return replace(cb, **changes)

    def queue(self, **changes):
        cb = self.callback(**changes)
        receipt = self.callbacks.submit(cb, self.version())
        return cb, receipt

    def receipt(self, stage, sequence=1, ref=None):
        ref = ref or "receipt:" + str(next(self.serial))
        self.observations.receipts[ref] = Receipt(
            self.ref, self.binding.fingerprint, sequence, stage, ref
        )
        return ref

    def process(self, state="quiescent", **changes):
        payload = {
            "process_ref": "process",
            "children": {"child": state},
            "state": state,
            "complete": True,
        }
        payload.update(changes)
        self.observations.process_fact = Observation(
            self.ref, self.binding.fingerprint, "process-proof", "process", encode(payload)
        )


@pytest.fixture
def harness(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return Harness(private / "operations.sqlite")
