"""Authored GraphQL observations through real client/Core; no live provider claim."""

import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.connections import AgentConnection
from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
from ai_task_manager.adapters.linear.dependencies.source import DependencySource
from ai_task_manager.adapters.linear.models import Credentials, ExpectedBinding, WireResponse
from ai_task_manager.domains.work.models import IssueStatus, Scope, TaskProse
from tests.domains.helpers import Rig

SCHEMA_CAPTURE_SHA256 = "25a2885200f941513b0878c6521e7955ab9ccec9b61d88efca563bcb063d6b0a"
STAMP = "2026-11-02T00:00:00.000000Z"


class ProviderFixture:
    def __init__(self, root):
        root.mkdir(mode=0o700)
        self.rig = Rig(root / "core.sqlite")
        ids = [str(uuid4()) for _ in range(4)]
        self.expected = ExpectedBinding(ids[0], ids[1], ids[2], "TST", ids[3])
        self.scope = Scope("linear", ids[1], ids[0], ids[3], "session")
        self.rig.scope, self.rig.handle = self.scope, self.rig.session(self.scope)
        self.connection = AgentConnection(
            str(uuid4()), ids[1], ids[0], self.scope.logical_session, str(uuid4())
        )
        self.states = {s.value: str(uuid4()) for s in IssueStatus}
        self.nodes, self.edges, self.calls, self.callback = {}, [], [], None
        self.page_size, self.disabled, self.depth = 50, False, 0
        self.schema = json.loads(
            (Path(__file__).parent / "fixtures/dependency_schema.json").read_text()
        )
        self.client = LinearClient(
            self, lambda: Credentials("authored-token"), self.expected, max_pages=5
        )
        self.client.verify_binding()
        self.receipts = DependencyReceipts(
            root / "dependencies.sqlite", root / "dependencies.key", clock=lambda: self.rig.now
        )
        self.rig.core.scheduling.sources = self.receipts
        self.source = DependencySource(self.rig.core, self.rig.handle, self.client, self.receipts)
        transaction = self.rig.db.transaction

        @contextmanager
        def observed_transaction():
            with transaction() as conn:
                self.depth += 1
                try:
                    yield conn
                finally:
                    self.depth -= 1

        self.rig.db.transaction = observed_transaction

    def task(self, *, handle=None, code=False, title="Verify dependency scope"):
        r = self.rig
        prose = TaskProse(title, "Retain the exact scoped result.", code)
        handle = handle or r.handle
        task = r.core.intake.submit(handle, prose, r.approval(prose), r.key())
        operation = r.core.sync.list_operations(handle)[-1]
        issue = str(uuid4())
        reference = r.proof(
            "provider_status",
            task.id,
            {
                "operation_id": operation.id,
                "status": "Backlog",
                "prose_digest": task.prose_digest,
                "title": task.title,
                "body": task.body,
                "representation": task.representation,
                "issue_id": issue,
            },
            scope=r.core.session_binding(handle)[1],
        )
        task = r.core.intake.confirm_status(
            handle, task.id, task.version, task.decision_revision, reference, r.key()
        )
        self.sync(task)
        return task

    def sync(self, task):
        self.nodes[task.issue_id] = {
            "id": task.issue_id,
            "identifier": "TST-123",
            "createdAt": STAMP,
            "updatedAt": STAMP,
            "archivedAt": None,
            "trashed": False,
            "priority": 3,
            "assignee": {"id": self.expected.viewer_id},
            "team": {"id": self.expected.team_id, "key": "TST"},
            "project": {"id": self.expected.project_id},
            "state": {"id": self.states[task.observed_status], "name": task.observed_status},
            "title": task.title,
            "description": task.body,
        }

    def edge(self, predecessor, successor, kind="blocks", **changes):
        row = {
            "id": str(uuid4()),
            "type": kind,
            "updatedAt": STAMP,
            "archivedAt": None,
            "issue": {"id": predecessor.issue_id},
            "relatedIssue": {"id": successor.issue_id},
        }
        row.update(changes)
        self.edges.append(row)
        return row

    def send(self, query, variables, credentials):
        assert self.depth == 0, "Provider I/O occurred inside Core"
        if self.disabled:
            raise AssertionError("No provider read was admitted")
        self.calls.append((query, copy.deepcopy(variables)))
        if self.callback:
            self.callback(query, variables)
        e = self.expected
        if "VerifyBinding" in query:
            data = {
                "viewer": {"id": e.viewer_id},
                "organization": {"id": e.workspace_id},
                "team": {"id": e.team_id, "key": e.team_key},
                "project": {"id": e.project_id},
            }
        elif "ProjectTeams" in query:
            data = {
                "project": {
                    "teams": {
                        "nodes": [{"id": e.team_id}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        elif "query AgentUsers" in query:
            data = {
                "organization": {"id": e.workspace_id},
                "users": {
                    "nodes": [{"id": self.connection.agent_id, "app": True}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        elif "query States" in query:
            data = {
                "team": {
                    "states": {
                        "nodes": [{"id": v, "name": k} for k, v in self.states.items()],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        elif "DependencySchema" in query:
            data = self.schema
        else:
            issue = variables["id"]
            node = copy.deepcopy(self.nodes[issue])
            if "DependencyProse" not in query:
                node.pop("title")
                node.pop("description")
            # Query shape v2 can request both first pages after metadata ownership.
            # A v1 directional request still returns only its requested connection.
            directions = [
                d
                for d in ("relations", "inverseRelations")
                if re.search(r"\b" + re.escape(d) + r"\s*\(", query)
            ]
            for direction in directions:
                endpoint = "relatedIssue" if direction == "inverseRelations" else "issue"
                rows = [copy.deepcopy(x) for x in self.edges if x[endpoint]["id"] == issue]
                start = int(variables.get("after") or 0)
                end = start + self.page_size
                node[direction] = {
                    "nodes": rows[start:end],
                    "pageInfo": {
                        "hasNextPage": end < len(rows),
                        "endCursor": str(end) if end < len(rows) else None,
                    },
                }
            data = {"issue": node}
        return WireResponse(200, {}, json.dumps({"data": data}).encode())

    def refresh(self, task, key=None):
        return self.source.refresh(task.id, key or self.rig.key())
