"""Authored subprocess protocol fixture, never an installed product acceptance run."""

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import SessionObservations
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.application.core import Core
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.claim_store import open_claim_store
from ai_task_manager.config.files import FileSeal, read_private, write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.models import ApprovalPolicy
from ai_task_manager.installation.sessions.setup import setup
from tests.linear.helpers import CONNECTION, EXPECTED, WEBHOOK, ProviderFixture

from .installed_endpoint import InstalledReceiverProcess, installed_route

CHILD = """import json, sys, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from uuid import uuid4
config = json.loads(Path(sys.argv[2]).read_text())
selected = json.loads(Path(config['receiver_file']).read_text())
identity = str(uuid4())
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        health = self.path == selected['route'] + '/health'
        body = json.dumps({'instance_id': identity}).encode() if health else b''
        self.send_response(200 if health else 405)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
print(json.dumps({'ok': True, 'event': 'receiver_ready', 'data': {
    'instance_id': identity, 'host': '127.0.0.1', 'port': server.server_port,
    'route': selected['route'], 'health_route': selected['route']+'/health',
    'lifetime_seconds': selected['lifetime_seconds']}}), flush=True)
server.timeout = .1
end = time.monotonic() + 30
while time.monotonic() < end: server.handle_request()
server.server_close()
"""


class InstalledEndpointFixture:
    def __init__(self, root, ledger):
        self.root, self.ledger = Path(root), ledger
        self.root.mkdir(mode=0o700)
        self.provider = ProviderFixture()
        self.child = self.group = None
        try:
            self._configure()
        except BaseException:
            self.close()
            raise

    def _configure(self):
        root, ledger = self.root, self.ledger
        self.config = root / "service.json"
        self.receiver = root / "receiver.json"
        secret = root / "secret.json"
        write_private(secret, {"secret": "authored-secret-not-real-credential"})
        write_private(
            self.receiver,
            {
                "version": 1,
                "route": installed_route(ledger.run_id),
                "webhook_id": WEBHOOK,
                "port": 0,
                "secret_file": str(secret),
                "lifetime_seconds": 30,
            },
        )
        credentials = root / "credential.env"
        credentials.write_text("LINEAR_TEST_USER_A_TOKEN=AUTHORED_NOT_LIVE\n")
        credentials.chmod(0o600)
        policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
        values = {
            "version": 1,
            "core_database": str(root / "core.sqlite"),
            "authority_database": str(root / "authority.sqlite"),
            "authority_key": str(root / "authority.key"),
            "session_file": str(root / "session.json"),
            "approval_policy": asdict(policy),
            "receiver_file": str(self.receiver),
            "linear": {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(replace(CONNECTION, logical_session=ledger.run_id)),
                "credential_file": str(credentials),
                "credential_key": "LINEAR_TEST_USER_A_TOKEN",
                "journal": str(root / "provider.sqlite"),
            },
        }
        write_private(self.config, values)
        components = LinearComponents(
            values["linear"],
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(_test_endpoint=self.provider.endpoint), credentials, expected
            ),
        )
        setup(
            self.config,
            logical_session=ledger.run_id,
            actions=("enqueue",),
            deadline=time.time() + 120,
            operation_key="authored-observer-setup",
            components=components,
        )
        values = read_private(self.config)
        journal = AuthorityJournal(values["authority_database"], values["authority_key"])
        database, claim_store = open_claim_store(values["core_database"], journal)
        core = Core(
            database,
            policy,
            principals=components.journal,
            evidence=components.evidence(journal),
            renderer=components.renderer,
        )
        handle = SessionFile(values["session_file"]).load(core)
        observations = SessionObservations(
            journal, components.client, self.config, values["session_observations"]
        )
        # Trusted authored composition; never import or run the unaccepted listener.
        self.service = ApplicationService(
            core,
            handle,
            decisions=DecisionAuthority(journal, observations),
            grant_ref=values["grant_ref"],
            inbound=components.inbound(core, handle),
            seal=FileSeal(self.config),
            claim_store=claim_store,
        )
        self.entrypoint = root / "authored-aitm"
        self.entrypoint.write_text("#!" + sys.executable + "\n" + CHILD)
        self.entrypoint.chmod(0o700)
        runtime = root / "authored-package"
        paths = [self.entrypoint]
        for name in (
            "application/linear_sync/listener.py",
            "adapters/linear/listener.py",
            "presentation/cli/main.py",
        ):
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Authored package binding, not installed product code\n")
            paths.append(path)
        self.package = {
            "entrypoint": str(self.entrypoint),
            "runtime_root": str(runtime),
            "pins": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        }

    def start(self, *, suffix=()):
        argv = [
            str(self.entrypoint),
            "--config",
            str(self.config),
            "--json",
            "sync",
            "listen",
            *suffix,
        ]
        self.child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"},
        )
        self.group = OwnedProcessGroup(self.child)
        self.group.capture()
        return InstalledReceiverProcess(
            self.group, self.service, self.config, self.ledger, self.package, startup_seconds=3
        )

    def close(self):
        record = {"pid": self.child.pid if self.child else None, "launched": self.child is not None}
        if self.group is not None:
            stopped = self.group.stop()
            record["group"] = self.group.evidence(stopped, orphaned=False)
            if stopped.confirmed:
                self.group.reap_exited_leader()
            record["reaped"] = self.child.returncode is not None
            for stream in (self.child.stdout, self.child.stderr):
                stream.close()
            (self.root / "process-stop.json").write_text(json.dumps(record, indent=2))
            assert stopped.confirmed and record["reaped"]
        self.provider.close()
