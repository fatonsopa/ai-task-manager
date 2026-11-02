import copy
import hashlib
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import UUID, uuid4

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.models import Credentials, ExpectedBinding
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.linear.webhooks import WebhookReceiver
from ai_task_manager.application.linear_sync.inbound import InboundDrain
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.linear_sync.reconcile import Reconciliation

from ai_task_manager.application.core import Core
from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy, IssueStatus, Scope
from ai_task_manager.storage.database import Database
from tests.domains.helpers import ProofFixture, Rig


def uid(number):
    return str(UUID(int=number, version=4))


EXPECTED = ExpectedBinding(uid(1), uid(2), uid(3), "TST", uid(4))
STATES = {status.value: uid(100 + i) for i, status in enumerate(IssueStatus)}
WEBHOOK = uid(10)


class ProviderFixture:
    def __init__(self):
        self.requests, self.issues = [], {}
        self.comments = {}
        self.serial = 0
        self.hook = None
        self.fail_next = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                owner.requests.append((request, dict(self.headers)))
                if owner.fail_next:
                    status, body = owner.fail_next
                    owner.fail_next = None
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                result = owner.graphql(request["query"], request["variables"])
                if owner.hook:
                    owner.hook(request, result)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": result}).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/graphql"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tick(self):
        self.serial += 1
        return (
            datetime.fromtimestamp(1793520000 + self.serial, UTC).isoformat().replace("+00:00", "Z")
        )

    def edit(self, issue_id, **changes):
        self.issues[issue_id].update(changes)
        self.issues[issue_id]["updatedAt"] = self.tick()
        return copy.deepcopy(self.issues[issue_id])

    def graphql(self, query, variables):
        if "query ReadComment" in query:
            comment = copy.deepcopy(self.comments.get(variables["id"]))
            if comment:
                comment["issue"] = copy.deepcopy(self.issues[comment["issue"]["id"]])
            return {"comment": comment}
        if "mutation CreateComment" in query:
            values = variables["input"]
            comment_id = values.get("id", str(uuid4()))
            comment = {
                "id": comment_id,
                "body": values["body"],
                "updatedAt": self.tick(),
                "user": {"id": EXPECTED.viewer_id},
                "issue": copy.deepcopy(self.issues[values["issueId"]]),
            }
            self.comments[comment_id] = comment
            return {"commentCreate": {"success": True, "comment": copy.deepcopy(comment)}}
        if "query VerifyBinding" in query:
            return {
                "viewer": {"id": EXPECTED.viewer_id},
                "organization": {"id": EXPECTED.workspace_id},
                "team": {"id": EXPECTED.team_id, "key": "TST"},
                "project": {"id": EXPECTED.project_id},
            }
        if "query ProjectTeams" in query:
            return {
                "project": {
                    "teams": {
                        "nodes": [{"id": EXPECTED.team_id}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        if "query States" in query:
            return {
                "team": {
                    "states": {
                        "nodes": [{"id": value, "name": key} for key, value in STATES.items()],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        if "query ReadIssue" in query:
            return {"issue": copy.deepcopy(self.issues.get(variables["id"]))}
        if "mutation CreateIssue" in query:
            values = variables["input"]
            issue_id = values.get("id", str(uuid4()))
            data = {
                "id": issue_id,
                "identifier": f"TST-{self.serial + 1}",
                "title": values["title"],
                "description": values["description"],
                "updatedAt": self.tick(),
                "team": {"id": values["teamId"], "key": "TST"},
                "project": {"id": values["projectId"]},
                "assignee": {"id": values["assigneeId"]},
                "state": {
                    "id": values["stateId"],
                    "name": next(
                        key for key, value in STATES.items() if value == values["stateId"]
                    ),
                },
            }
            self.issues[issue_id] = data
            return {"issueCreate": {"success": True, "issue": copy.deepcopy(data)}}
        if "mutation UpdateIssue" in query:
            values = variables["input"]
            data = self.issues[variables["id"]]
            if "stateId" in values:
                data["state"] = {
                    "id": values["stateId"],
                    "name": next(
                        key for key, value in STATES.items() if value == values["stateId"]
                    ),
                }
            for name in ("title", "description"):
                if name in values:
                    data[name] = values[name]
            data["updatedAt"] = self.tick()
            return {"issueUpdate": {"success": True, "issue": copy.deepcopy(data)}}
        raise AssertionError("Unexpected fixture query")

    def event(self, issue_id, *, actor=None, action="update", timestamp=1793520000000):
        issue = self.issues[issue_id]
        return {
            "action": action,
            "type": "Issue",
            "actor": actor or {"type": "user", "id": EXPECTED.viewer_id},
            "organizationId": EXPECTED.workspace_id,
            "webhookId": WEBHOOK,
            "webhookTimestamp": timestamp,
            "createdAt": issue["updatedAt"],
            "data": {
                "id": issue_id,
                "updatedAt": issue["updatedAt"],
                "teamId": issue["team"]["id"],
                "projectId": issue["project"]["id"],
                "assigneeId": issue["assignee"]["id"] if issue["assignee"] else None,
                "stateId": issue["state"]["id"],
            },
        }


class FixtureRouter:
    def __init__(self, provider, fixture):
        self.provider, self.fixture = provider, fixture

    def verify_evidence(self, ref):
        return (
            self.provider.verify_evidence(ref)
            if isinstance(ref, str) and ref.startswith("linear-proof:")
            else self.fixture.verify_evidence(ref)
        )


class LinearRig(Rig):
    def __init__(self, path, provider):
        self.db = Database(path / "core.sqlite")
        self.now, self.serial = 1793520000.0, 0
        self.journal = ProviderJournal(path / "provider.sqlite", clock=lambda: self.now)
        self.proofs = ProofFixture()
        self.policy = ApprovalPolicy("writer-frozen", "issue-reference-frozen", "style-frozen")
        self.renderer = LinearMarkdownRenderer()
        self.capacity = CapacityPolicy("host", 2, 2)
        self.core = self.new_core()
        self.client = LinearClient(
            HttpTransport(_test_endpoint=provider.endpoint),
            lambda: Credentials("fixture-api-secret"),
            EXPECTED,
        )
        binding = self.client.verify_binding()
        self.scope = Scope(
            "linear",
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.project_id,
            "fixture-session",
        )
        self.handle = self.core.register_session(self.journal.bind_principal(binding, self.scope))
        self.outbound = OutboundDrain(self.core, self.client, self.journal)
        self.inbound = InboundDrain(
            self.core,
            self.client,
            self.journal,
            clock=lambda: self.now,
            authorized_actors={EXPECTED.viewer_id},
        )
        self.reconcile = Reconciliation(self.core, self.client, self.journal)
        self.receiver = WebhookReceiver(
            self.core,
            self.handle,
            self.journal,
            binding,
            WEBHOOK,
            lambda: b"fixture-webhook-signing-secret",
            clock=lambda: self.now,
        )
        self.provider = provider

    def new_core(self):
        from tests.scheduling.helpers import SnapshotSource

        if not hasattr(self, "scheduling_sources"):
            self.scheduling_sources = SnapshotSource()
        return Core(
            self.db,
            self.policy,
            (self.capacity,),
            principals=self.journal,
            approvals=self.proofs,
            evidence=FixtureRouter(self.journal, self.proofs),
            renderer=self.renderer,
            clock=lambda: self.now,
            scheduling_sources=self.scheduling_sources,
        )

    def task(self, *, confirmed=True, **kwargs):
        task = super().task(confirmed=False, **kwargs)
        return self.confirm(task) if confirmed else task

    def confirm(self, task, **kwargs):
        from tests.scheduling.helpers import known_declaration

        result = self.outbound.confirm_pending(self.handle, task.id)
        known_declaration(self, result)
        return result

    def signed(self, payload, delivery_id=None, secret=b"fixture-webhook-signing-secret"):
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = [
            ("Content-Type", "application/json"),
            ("Linear-Delivery", delivery_id or str(uuid4())),
            ("Linear-Event", "Issue"),
            ("Linear-Signature", hmac.new(secret, body, hashlib.sha256).hexdigest()),
            ("Linear-Timestamp", str(payload["webhookTimestamp"])),
        ]
        return headers, body
