"""Synthetic provider composition; never a production credential or endpoint."""

import http.client
import time
from dataclasses import asdict, replace
from pathlib import Path

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.claim_store import enroll_claim_store
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.models import ApprovalPolicy
from ai_task_manager.installation.sessions.setup import setup
from ai_task_manager.storage.database import Database
from tests.linear.helpers import CONNECTION, EXPECTED, WEBHOOK, LinearRig, ProviderFixture

SECRET = "fixture-webhook-signing-secret"
ROUTE = "/hooks/receiver_fixture_154"


class Fixture:
    def __init__(self, root, *, enrollment_seconds=120, database_directory=None, port=0):
        self.provider = None
        try:
            self._initialize(root, enrollment_seconds, database_directory, port)
        except BaseException:
            if self.provider is not None:
                self.provider.close()
            raise

    def _initialize(self, root, enrollment_seconds, database_directory, port):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.provider = ProviderFixture()
        self.rig = LinearRig(self.root, self.provider)
        if database_directory is not None:
            database_directory = Path(database_directory)
            database_directory.mkdir(mode=0o700)
            target = database_directory / "core.sqlite"
            Path(self.rig.db.path).rename(target)
            self.rig.db = Database(target)
        self.rig.now = time.time()
        self.rig.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
        self.rig.core = self.rig.new_core()
        self.rig.db_path = Path(self.rig.db.path)
        self.rig.db_path.chmod(0o600)
        self.rig.scope = replace(self.rig.scope, logical_session="receiver-installed-fixture")
        self.connection = replace(CONNECTION, logical_session=self.rig.scope.logical_session)
        secret = self.root / "webhook-secret.json"
        write_private(secret, {"secret": SECRET})
        credentials = self.root / "credentials.env"
        credentials.write_text("LINEAR_TEST_USER_A_TOKEN=fixture-api-secret\n")
        credentials.chmod(0o600)
        self.receiver = self.root / "receiver.json"
        self.values = {
            "version": 1,
            "route": ROUTE,
            "webhook_id": WEBHOOK,
            "port": port,
            "secret_file": str(secret),
            "lifetime_seconds": 60,
        }
        write_private(self.receiver, self.values)
        self.config = self.root / "service.json"
        self.config_data = {
            "version": 1,
            "core_database": str(self.rig.db_path),
            "authority_database": str(self.root / "authority.sqlite"),
            "authority_key": str(self.root / "authority.key"),
            "session_file": str(self.root / "session.json"),
            "approval_policy": asdict(self.rig.policy),
            "receiver_file": str(self.receiver),
            "linear": {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(self.connection),
                "credential_file": str(credentials),
                "credential_key": "LINEAR_TEST_USER_A_TOKEN",
                "journal": str(self.root / "provider.sqlite"),
            },
        }
        authority = AuthorityJournal(
            self.config_data["authority_database"], self.config_data["authority_key"]
        )
        enroll_claim_store(self.rig.db, authority)
        write_private(self.config, self.config_data)
        components = self.components()
        setup(
            self.config,
            logical_session=self.rig.scope.logical_session,
            actions=("enqueue",),
            deadline=time.time() + enrollment_seconds,
            operation_key="receiver-fixture-enrollment",
            components=components,
        )
        self.config_data = read_private(self.config)
        self.rig.handle = SessionFile(self.config_data["session_file"]).load(self.rig.core)
        self.rig.client = components.client
        self.rig.outbound = OutboundDrain(self.rig.core, self.rig.client, self.rig.journal)
        self.task = self.rig.task()

    def components(self):
        return LinearComponents(
            self.config_data["linear"],
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(_test_endpoint=self.provider.endpoint), credentials, expected
            ),
        )

    def load(self):
        return load_service(self.config, linear_components=self.components())

    def event(self, **changes):
        payload = self.provider.event(self.task.issue_id, timestamp=int(time.time() * 1000))
        payload.update(changes)
        return self.rig.signed(payload)

    def close(self):
        self.provider.close()


def request(address, route, headers=(), body=b"", method="POST"):
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.putrequest(method, route, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Content-Length", str(len(body)))
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_for(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Bounded receiver condition did not settle")
