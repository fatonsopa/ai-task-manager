"""Actual Writer/service composition with explicitly authored external observations."""

import os
import sys
import time
from dataclasses import asdict

import pytest

from ai_task_manager.adapters.decisions.authority import DecisionAuthority, HostGrant
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.adapters.writing.service import WritingService
from ai_task_manager.application.core import Core
from ai_task_manager.application.existing_intake import ExistingIntake
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.config.files import write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.models import CapacityPolicy
from tests.application.helpers import Observations, reviewed_control


def compose(rig):
    root = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not root:
        pytest.skip("Actual configured current Technical Writer is required")
    package = WriterPackage.capture(root, sys.executable)
    writer = WritingService(
        package,
        AuthorityJournal(rig.root / "writing.sqlite", rig.root / "writing.key"),
        rig.root / "writing",
        rig.renderer,
    )
    rig.policy = package.policy()
    rig.core = Core(
        rig.db,
        rig.policy,
        (CapacityPolicy("fixture", 2, 2),),
        principals=rig.journal,
        approvals=writer.approvals_for_tasks(),
        evidence=rig.router,
        renderer=rig.renderer,
    )
    rig.intake = ExistingIntake(rig.core, rig.handle, rig.reader)
    observations = Observations()
    observations.grants["lead-grant"] = HostGrant(
        rig.scope,
        rig.scope.logical_session,
        ("enqueue", "start", "dequeue"),
        "synthetic-authorized-lead",
        time.time() + 3600,
    )
    decisions = DecisionAuthority(rig.authority, observations)
    service = ApplicationService(
        rig.core,
        rig.handle,
        writer=writer,
        decisions=decisions,
        grant_ref="lead-grant",
        provider=OutboundDrain(rig.core, rig.client, rig.journal),
        existing=rig.intake,
        claim_store=rig.claim,
    )
    return service


def approve(service, title, body, *, scope=None, purpose="task", task=None):
    if scope is None:
        prepared = service.call(
            "writing_prepare",
            {
                "kind": "issue_feature",
                "purpose": purpose,
                "title": title,
                "body": body,
                "code_bearing": True,
                "required_work": ["complete"],
                **({"task_id": task.id} if task else {}),
            },
        )
        assert prepared["ok"], prepared
        prepared = prepared["data"]
    else:
        from ai_task_manager.domains.work.models import TaskProse

        prepared = service.writer.prepare(
            scope, "issue_feature", TaskProse(title, body), purpose=purpose, task=task
        )
    scope = scope or service.scope
    review = service.writer.review(
        scope, prepared["writing_id"], reviewed_control(prepared["packet"])
    )
    assert review["accepted"], review
    result = service.writer.finalize(scope, prepared["writing_id"])
    return result


def configuration(rig, service, path):
    package = service.writer.package
    credentials = rig.root / "fixture-credentials.env"
    if not credentials.exists():
        credentials.write_text("LINEAR_TEST_USER_A_TOKEN=synthetic-fixture-only\n")
        os.chmod(credentials, 0o600)
    SessionFile(rig.root / "session.json").save(rig.handle, rig.scope)
    result = {
        "version": 1,
        "core_database": rig.db.path,
        "authority_database": str(rig.authority.path),
        "authority_key": str(rig.authority.key_path),
        "session_file": str(rig.root / "session.json"),
        "approval_policy": asdict(package.policy()),
        "writer": {
            "root": str(package.root),
            "python": package.python,
            "pins": package.pins,
            "sessions_root": str(rig.root / "writing"),
            "journal": str(rig.root / "writing.sqlite"),
            "key": str(rig.root / "writing.key"),
        },
        "linear": {
            "expected": asdict(rig.client.expected),
            "credential_file": str(credentials),
            "credential_key": "LINEAR_TEST_USER_A_TOKEN",
            "journal": rig.journal.path,
        },
    }
    write_private(path, result)
    return result
