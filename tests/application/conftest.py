"""Actual local HTTP, actual core and actual Writer; no live provider/model."""

import os
import sys
from dataclasses import asdict

import pytest

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.claim_store import enroll_claim_store
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.config.sessions import SessionFile
from tests.application.helpers import Observations
from tests.linear.helpers import CONNECTION, EXPECTED, LinearRig, ProviderFixture


@pytest.fixture
def composed(tmp_path):
    root = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not root:
        pytest.skip("Actual configured external Technical Writer is required")
    os.chmod(tmp_path, 0o700)
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        os.chmod(tmp_path / "core.sqlite", 0o600)
        credentials = tmp_path / "credentials.env"
        credentials.write_text("LINEAR_TEST_USER_A_TOKEN='synthetic-fixture-only'\n")
        os.chmod(credentials, 0o600)
        package = WriterPackage.capture(root, sys.executable)
        config = {
            "version": 1,
            "core_database": str(tmp_path / "core.sqlite"),
            "authority_database": str(tmp_path / "authority.sqlite"),
            "authority_key": str(tmp_path / "authority.key"),
            "session_file": str(tmp_path / "session.json"),
            "approval_policy": asdict(package.policy()),
            "grant_ref": "enrollment",
            "writer": {
                "root": str(package.root),
                "python": package.python,
                "pins": package.pins,
                "sessions_root": str(tmp_path / "writing"),
                "journal": str(tmp_path / "writing.sqlite"),
                "key": str(tmp_path / "writing.key"),
            },
            "linear": {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(CONNECTION),
                "credential_file": str(credentials),
                "credential_key": "LINEAR_TEST_USER_A_TOKEN",
                "journal": str(tmp_path / "provider.sqlite"),
            },
        }
        SessionFile(config["session_file"]).save(rig.handle, rig.scope)
        observations = Observations()
        import time

        observations.grants["enrollment"] = HostGrant(
            rig.scope,
            rig.scope.logical_session,
            ("enqueue", "dequeue", "start"),
            "synthetic-trusted-enrollment",
            time.time() + 3600,
        )
        path = tmp_path / "service.json"
        enroll_claim_store(
            rig.db, AuthorityJournal(config["authority_database"], config["authority_key"])
        )
        write_private(path, config)
        components = LinearComponents(
            config["linear"],
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(_test_endpoint=provider.endpoint), credentials, expected
            ),
        )
        service = load_service(path, observations=observations, linear_components=components,
                               scheduling_sources=rig.scheduling_sources)
        yield service, rig, provider, components
    finally:
        provider.close()
