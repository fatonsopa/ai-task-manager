"""Disposable real policy-revision-1 accounting for authored host fixtures only."""

from datetime import UTC, datetime

from ai_task_manager.adapters.hosts.bridge.invocations import (
    InitializationInvocation,
    InvocationBinding,
    InvocationPermits,
)
from tests.test_build_control_policy_amendment import Rig
from tools.build_control import BuildControl
from tools.build_support.authorization import PinnedRecords


class FixtureAccounting:
    def __init__(self, root, *, maximum_seconds=60):
        root.mkdir(mode=0o700)
        self.rig = Rig(root)
        self.rig.now = datetime.now(UTC)
        self.rig.amend()
        self.rig.batch(count=100, seconds=maximum_seconds, budget="0")
        self.maximum_seconds = maximum_seconds
        self.control = BuildControl(
            self.rig.base.ledger,
            expected_policy_revision=1,
            records=PinnedRecords(self.rig.bindings),
        )
        self.number = 0
        self.owners, self.auth, self.hosts = {}, {}, {}

    def claim(self, policy, owner, purpose, *, worker=False, parent=None, maximum=60):
        self.number += 1
        call_id = f"fixture-{purpose}-{self.number}"
        # Claim establishes the persisted relative bound slightly after this
        # observation. Leave a finite preparation margin, then verify the
        # actual result rather than extending the immutable owner deadline.
        seconds = min(maximum, owner.deadline - datetime.now(UTC).timestamp()) - 0.05
        self.control.reserve(
            call_id,
            label="authored credential-free host control",
            paid=False,
            max_cost_usd=None,
            uses_worker=worker,
            batch_id="batch",
            timeout_seconds=seconds,
            metadata={
                "parent_call_id": parent,
                "invocation_binding": {
                    "attempt_id": owner.attempt_id,
                    "host": owner.host,
                    "configuration_digest": owner.configuration_digest,
                    "model": policy.model,
                    "effort": policy.effort,
                    "purpose": purpose,
                },
            },
        )
        call = self.control.claim_launch(call_id, worker_identity=owner.worker_id)
        binding = InvocationBinding.observed(
            {"run_id": "policy-fixture", "policy_revision": 1, "call": call}
        )
        assert binding.deadline <= owner.deadline
        return InvocationPermits(self.control, {call_id: binding}).prepare_launch(call_id)

    def initialize(self, policy, owner):
        self.owners[owner.attempt_id] = (policy, owner)
        host = self.claim(policy, owner, "host", worker=True, maximum=self.maximum_seconds)
        relay = self.claim(
            policy,
            owner,
            "relay",
            parent=host.binding.call_id,
            maximum=host.binding.deadline - datetime.now(UTC).timestamp() - 0.02,
        )
        self.hosts[owner.attempt_id] = (host, relay)
        return InitializationInvocation(host, relay)

    def authorize_auth(self, policy, owner):
        permit = self.claim(policy, owner, "auth", maximum=15)
        self.auth[owner.attempt_id] = permit
        return permit

    def record_auth(self, owner, completion):
        permit = self.auth[owner.attempt_id]
        self.control.record_outcome(
            permit.binding.call_id,
            outcome=completion["outcome"],
            process_stopped=completion["group"]["confirmed_stop"],
            actual_cost_usd="0" if completion["group"]["confirmed_stop"] else None,
            evidence_ref="authored actual status subprocess completion",
        )

    def assignment(self, attempt):
        policy, owner = self.owners[attempt.id]
        return self.claim(
            policy, owner, "assignment", parent=self.hosts[attempt.id][0].binding.call_id
        )

    def complete(self, permit, outcome):
        self.control.record_outcome(
            permit.binding.call_id,
            outcome="succeeded" if outcome == "observed_turn" else "unknown",
            process_stopped=outcome == "observed_turn",
            actual_cost_usd="0" if outcome == "observed_turn" else None,
            evidence_ref="authored protocol turn result, not a real model",
        )

    def stop(self, owner, stopped):
        for permit in self.hosts[owner.attempt_id]:
            self.control.record_outcome(
                permit.binding.call_id,
                outcome="succeeded" if stopped.confirmed else "unknown",
                process_stopped=stopped.confirmed,
                actual_cost_usd="0" if stopped.confirmed else None,
                evidence_ref="authored complete owned host group stop",
            )
