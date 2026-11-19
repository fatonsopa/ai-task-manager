"""Owned HTTP identity controls; these never claim a real provider account."""

import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.models import ExpectedBinding
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.models import IssueStatus


class OwnedProvider:
    def __init__(self, root):
        root.mkdir(mode=0o700)
        self.root = root
        self.expected = ExpectedBinding(*(str(uuid4()) for _ in range(3)), "TST", str(uuid4()))
        from tests.linear.helpers import observed_quota
        self.quota = observed_quota(root, self.expected.viewer_id)
        self.actual = self.expected
        self.agent_id = str(uuid4())
        self.requests = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                query = request["query"]
                provider.requests += 1
                e = provider.actual
                page = {"hasNextPage": False, "endCursor": None}
                if "VerifyBinding" in query:
                    data = {
                        "viewer": {"id": e.viewer_id},
                        "organization": {"id": e.workspace_id},
                        "team": {"id": e.team_id, "key": e.team_key},
                        "project": {"id": e.project_id},
                    }
                elif "ProjectTeams" in query:
                    data = {"project": {"teams": {"nodes": [{"id": e.team_id}], "pageInfo": page}}}
                elif "query AgentUsers" in query:
                    data = {
                        "organization": {"id": e.workspace_id},
                        "users": {
                            "nodes": [{"id": provider.agent_id, "app": True}],
                            "pageInfo": page,
                        },
                    }
                elif "query States" in query:
                    data = {
                        "team": {
                            "states": {
                                "nodes": [
                                    {"id": str(uuid4()), "name": s.value} for s in IssueStatus
                                ],
                                "pageInfo": page,
                            }
                        }
                    }
                else:
                    raise AssertionError("No provider mutation or task query is admitted")
                raw = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        credential = root / "credential.env"
        credential.write_text("LINEAR_API_KEY=authored-local-credential\n")
        credential.chmod(0o600)
        self.config = root / "service.json"
        self.linear = {
            "expected": asdict(self.expected),
            "agent_connection": {
                "connection_id": str(uuid4()),
                "workspace_id": self.expected.workspace_id,
                "principal_id": self.expected.viewer_id,
                "logical_session": "lead-session",
                "agent_id": self.agent_id,
            },
            "credential_file": str(credential),
            "credential_key": "LINEAR_API_KEY",
            "journal": str(root / "provider.sqlite"),
        }
        write_private(
            self.config,
            {
                "version": 1,
                "core_database": str(root / "core.sqlite"),
                "authority_database": str(root / "authority.sqlite"),
                "authority_key": str(root / "authority.key"),
                "session_file": str(root / "session.json"),
                "approval_policy": {
                    "writer_hash": "a" * 64,
                    "reference_hash": "b" * 64,
                    "style_hash": "c" * 64,
                },
                "linear": self.linear,
            },
        )
        self.deadline = time.time() + 120

    def components(self):
        return LinearComponents(
            self.linear,
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(timeout=2, _test_endpoint=self.endpoint, _quota_store=self.quota),
                credentials, expected
            ),
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()
