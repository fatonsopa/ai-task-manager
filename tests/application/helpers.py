"""Synthetic trusted controls; these are not fresh host/model observations."""

import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.decisions.authority import DecisionAuthority, HostGrant
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.adapters.writing.service import WritingService
from ai_task_manager.application.core import Core
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.config.files import write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import ApprovalPolicy
from tests.domains.helpers import Rig

FEATURE_BODY = """Type: Feature

## Description

Separate sessions need their own tracked work and truthful progress.

## Expected outcome

Only the bound session can read and change its assigned task.

## Acceptance criteria

* Keep `check --scope=session` unchanged.
* Refuse cross-session reads and retain failed attempts.
"""


class Observations:
    def __init__(self):
        self.grants, self.instructions, self.host = {}, {}, {}

    def verify_enrollment(self, ref):
        if ref not in self.grants:
            raise EvidenceRequired("No observed enrollment")
        return self.grants[ref]

    def verify_instruction(self, ref):
        if ref not in self.instructions:
            raise EvidenceRequired("No observed instruction")
        return self.instructions[ref]

    def verify_host_observation(self, ref):
        if ref not in self.host:
            raise EvidenceRequired("No observed host trace")
        return self.host[ref]


class EvidenceRoutes:
    def __init__(self, authority, fixture):
        self.authority, self.fixture = authority, fixture

    def verify_evidence(self, ref):
        if ref.startswith("decision-proof:"):
            return self.authority.verify_evidence(ref)
        return self.fixture.verify_evidence(ref)


class ProviderFixture:
    def __init__(self, rig):
        self.rig, self.calls = rig, []

    def confirm_pending(self, handle, task_id):
        task = self.rig.core.get_task(handle, task_id)
        operations = self.rig.core.sync.list_operations(handle)
        assert any(op.task_id == task_id for op in operations), "Intent must precede effect"
        self.calls.append(task_id)
        return self.rig.confirm(task, handle=handle)

    def run_once(self, handle):
        for task in self.rig.core.list_tasks(handle):
            if task.sync_state != "confirmed":
                return self.confirm_pending(handle, task.id)
        return None


class ServiceRig:
    def __init__(self, root, *, writer_root=None, provider=True):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.rig = Rig(self.root / "core.sqlite")
        self.rig.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
        self.authority = AuthorityJournal(
            self.root / "authority.sqlite", self.root / "authority.key"
        )
        self.observations = Observations()
        self.observations.grants["host-grant"] = HostGrant(
            self.rig.scope,
            self.rig.scope.logical_session,
            ("enqueue", "dequeue", "start", "cancel", "duplicate", "hold", "resume"),
            "synthetic-operator-enrollment",
            time.time() + 3600,
        )
        self.decisions = DecisionAuthority(self.authority, self.observations)
        self.writer = None
        if writer_root:
            package = WriterPackage.capture(writer_root, sys.executable)
            self.rig.policy = package.policy()
            self.writer = WritingService(
                package,
                AuthorityJournal(self.root / "writing.sqlite", self.root / "writing.key"),
                self.root / "writing",
                self.rig.renderer,
            )
        self.rig.core = Core(
            self.rig.db,
            self.rig.policy,
            (self.rig.capacity,),
            principals=self.rig.proofs,
            approvals=self.writer.approvals_for_tasks() if self.writer else self.rig.proofs,
            evidence=EvidenceRoutes(self.authority, self.rig.proofs),
            renderer=self.rig.renderer,
            scheduling_sources=self.rig.scheduling_sources,
            clock=lambda: self.rig.now,
        )
        self.provider = ProviderFixture(self.rig) if provider else None
        self.service = ApplicationService(
            self.rig.core,
            self.rig.handle,
            writer=self.writer,
            decisions=self.decisions,
            grant_ref="host-grant",
            provider=self.provider,
        )

    def configuration(self):
        os.chmod(self.root / "core.sqlite", 0o600)
        from ai_task_manager.config.claim_store import enroll_claim_store

        enroll_claim_store(self.rig.db, self.authority)
        SessionFile(self.root / "session.json").save(self.rig.handle, self.rig.scope)
        config = {
            "version": 1,
            "core_database": str(self.root / "core.sqlite"),
            "authority_database": str(self.root / "authority.sqlite"),
            "authority_key": str(self.root / "authority.key"),
            "session_file": str(self.root / "session.json"),
            "approval_policy": asdict(self.rig.policy),
        }
        path = self.root / "config.json"
        write_private(path, config)
        return path


def reviewed_control(packet):
    """Frozen reviewed prose fixture, not independent or fresh AI behavior."""
    style = packet["writing_style"]
    reason = (
        "Synthetic mechanical control for the reviewed fixture: preserve its supplied "
        "facts and exact command, issue structure and scoped acceptance without "
        "inventing results."
    )
    return {
        "objective_sha256": packet["objective_sha256"],
        "output_sha256": packet["output_sha256"],
        "reviewer": {
            "kind": "current_host_self_review",
            "identity": "Current host self-review",
            "model": None,
            "method": "Synthetic reviewed fixture control; not fresh AI performance.",
        },
        "scores": [3] * 7,
        "critical_failures": [],
        "reasons": [reason],
        "requirements": [
            {
                "id": row["id"],
                "accepted": True,
                "rationale": reason,
                "source_ids": ["task-brief", "task-metadata"],
            }
            for row in packet["requirements"]
        ],
        "style_review": {
            "guide_sha256": style["guide_sha256"],
            "output_sha256": packet["output_sha256"],
            "targets_sha256": style["targets_sha256"],
            "reviewed_targets": [row["id"] for row in style["targets"]],
            "passed": True,
            "rationale": reason,
        },
    }
