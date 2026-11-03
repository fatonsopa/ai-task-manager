"""Authored provider/selection controls, not live user or provider acceptance."""

import copy
import os
import time
from dataclasses import replace
from uuid import uuid4

from ai_task_manager.adapters.decisions.existing import IssueSelection, SelectionAuthority
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.existing import ExistingReader
from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.adapters.linear.receipts import KINDS, ProviderJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.application.core import Core
from ai_task_manager.application.existing_intake import ExistingIntake
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.application.service.linear import DECISION_KINDS
from ai_task_manager.config.claim_store import enroll_claim_store, verify_claim_store
from ai_task_manager.domains.work.errors import ApprovalRequired
from ai_task_manager.domains.work.existing import readback_context
from ai_task_manager.domains.work.models import (
    Approval,
    ApprovalPolicy,
    CapacityPolicy,
    Scope,
    TaskProse,
)
from ai_task_manager.storage.database import Database
from tests.domains.helpers import ProofFixture
from tests.linear.helpers import CONNECTION, EXPECTED, STATES, ProviderFixture
from tests.scheduling.helpers import SnapshotSource, seed_known_queue

CREATED = "2026-11-01T00:00:00Z"
SECOND = "2026-11-01T00:01:00Z"
UPDATED = "2026-11-01T00:02:00Z"
CATALOG = "2020-01-01T00:00:00Z"


def span(status, start=CREATED, end=None, number=0):
    kind = {
        "Backlog": "backlog",
        "Todo": "unstarted",
        "Done": "completed",
        "Canceled": "canceled",
        "Duplicate": "duplicate",
    }.get(status, "started")
    return {
        "id": f"state-span-{number}",
        "stateId": STATES[status],
        "startedAt": start,
        "endedAt": end,
        "state": {
            "id": STATES[status],
            "name": status,
            "type": kind,
            "createdAt": CATALOG,
            "updatedAt": CATALOG,
            "team": {"id": EXPECTED.team_id},
        },
    }


class ExistingProvider(ProviderFixture):
    def __init__(self):
        self.histories = {}
        self.page_size = 50
        self.return_foreign = False
        self.history_override = None
        super().__init__()

    def seed(self, status="Backlog", *, issue_id=None, title="Verify task ownership"):
        issue_id = issue_id or str(uuid4())
        issue = {
            "id": issue_id,
            "identifier": f"TST-{len(self.issues) + 1}",
            "title": title,
            "description": "Verify the selected issue and retain evidence.",
            "createdAt": CREATED,
            "updatedAt": UPDATED,
            "archivedAt": None,
            "trashed": False,
            "assignee": {"id": EXPECTED.viewer_id},
            "team": {"id": EXPECTED.team_id, "key": "TST"},
            "project": {"id": EXPECTED.project_id},
            "state": {"id": STATES[status], "name": status},
        }
        self.issues[issue_id] = issue
        self.histories[issue_id] = [span("Backlog")]
        if status != "Backlog":
            self.histories[issue_id] = [span("Backlog", end=SECOND), span(status, SECOND, number=1)]
        return issue_id

    @staticmethod
    def metadata(raw):
        return {key: value for key, value in raw.items() if key not in {"title", "description"}}

    def graphql(self, query, variables):
        if "query ExistingSearch" in query:
            candidates = list(self.issues.values())
            query_text = variables["filter"]["and"][0]["title"]["containsIgnoreCase"]
            candidates = [
                item for item in candidates if query_text.casefold() in item["title"].casefold()
            ]
            offset = int(variables["after"] or 0)
            nodes = candidates[offset : offset + variables["first"]]
            end = offset + len(nodes)
            return {
                "issues": {
                    "nodes": [self.metadata(copy.deepcopy(item)) for item in nodes],
                    "pageInfo": {"hasNextPage": end < len(candidates), "endCursor": str(end)},
                }
            }
        if any(
            name in query
            for name in (
                "query ExistingMetadata",
                "query ExistingProse",
                "query ExistingTitle",
                "query ExistingStateHistory",
            )
        ):
            issue = self.issues.get(variables["id"])
            if issue is None:
                issue = next(
                    (
                        value
                        for value in self.issues.values()
                        if value["identifier"] == variables["id"]
                    ),
                    None,
                )
            if issue is None:
                return {"issue": None}
            if "query ExistingStateHistory" in query:
                nodes = self.histories[issue["id"]]
                offset = int(variables["after"] or 0)
                selected = copy.deepcopy(nodes[offset : offset + self.page_size])
                end = offset + len(selected)
                result = {
                    "issue": {
                        "id": issue["id"],
                        "stateHistory": {
                            "nodes": selected,
                            "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
                        },
                    }
                }
                return self.history_override(result) if self.history_override else result
            result = copy.deepcopy(issue)
            if "query ExistingMetadata" in query:
                result = self.metadata(result)
            if "query ExistingTitle" in query:
                result.pop("description", None)
            return {"issue": result}
        previous = copy.deepcopy(self.issues.get(variables.get("id")))
        result = super().graphql(query, variables)
        if "mutation UpdateIssue" in query and previous:
            issue = self.issues[previous["id"]]
            if issue["state"]["id"] != previous["state"]["id"]:
                history = self.histories[issue["id"]]
                history[-1]["endedAt"] = issue["updatedAt"]
                history.append(
                    span(issue["state"]["name"], issue["updatedAt"], number=len(history))
                )
        return result


class ScopedApprovals:
    def __init__(self, renderer, policy):
        self.renderer, self.policy, self.rows = renderer, policy, {}

    def add(self, scope, prose):
        ref = "synthetic-task-writing:" + str(uuid4())
        rendered = self.renderer.render(prose)
        self.rows[ref] = (
            scope,
            prose,
            Approval(prose_digest(prose, rendered), self.policy, rendered.representation),
        )
        return ref

    def verify_approval(self, ref):
        if ref not in self.rows:
            raise ApprovalRequired("Synthetic scoped approval is unavailable")
        return self.rows[ref][2]

    def verify_scoped_approval(self, ref, scope):
        self.verify_approval(ref)
        if self.rows[ref][0] != scope:
            raise ApprovalRequired("Synthetic approval belongs to another scope")
        return self.rows[ref][2]


class SelectionObservations:
    def __init__(self):
        self.rows = {}

    def verify_issue_selection(self, ref):
        return self.rows[ref]


class ExistingRig:
    def __init__(self, root, provider, *, session="session-a", db=None, authority=None):
        self.root, self.provider = root, provider
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self.db = db or Database(root / "core.sqlite")
        os.chmod(self.db.path, 0o600)
        self.authority = authority or AuthorityJournal(
            root / "authority.sqlite", root / "authority.key"
        )
        enroll_claim_store(self.db, self.authority)
        self.claim = verify_claim_store(self.db, self.authority)
        self.journal = ProviderJournal(root / "provider.sqlite")
        self.connection = replace(CONNECTION, logical_session=session)
        self.client = LinearClient(
            HttpTransport(_test_endpoint=provider.endpoint),
            lambda: Credentials("synthetic-fixture-only"),
            EXPECTED,
            agent_connection=lambda: self.connection,
        )
        self.binding = self.client.verify_binding()
        self.scope = Scope(
            "linear", EXPECTED.workspace_id, EXPECTED.viewer_id, EXPECTED.project_id, session
        )
        self.renderer = LinearMarkdownRenderer()
        self.policy = ApprovalPolicy("writer-fixture", "reference-fixture", "style-fixture")
        self.approvals = ScopedApprovals(self.renderer, self.policy)
        self.router = EvidenceRouter(
            [
                EvidenceRoute("linear-proof:", KINDS, self.journal),
                EvidenceRoute("decision-proof:", DECISION_KINDS, self.authority),
            ]
        )
        self.scheduling_sources = SnapshotSource()
        self.core = Core(
            self.db,
            self.policy,
            (CapacityPolicy("fixture", 2, 2),),
            principals=self.journal,
            evidence=self.router,
            approvals=self.approvals,
            renderer=self.renderer,
            scheduling_sources=self.scheduling_sources,
        )
        self.handle = self.core.register_session(
            self.journal.bind_principal(self.binding, self.scope)
        )
        self.reader = ExistingReader(self.client, self.journal, self.scope, self.claim.context)
        self.intake = ExistingIntake(self.core, self.handle, self.reader)
        self.observations = SelectionObservations()
        self.selection_authority = SelectionAuthority(self.authority, self.observations)

    def packet(self, issue_id):
        snapshot, ref = self.reader.inspect(issue_id)
        prose = TaskProse(snapshot["title"], snapshot["body"])
        approval = self.approvals.add(self.scope, prose)
        return ref, prose, approval, self.select(ref)

    def select(self, ref):
        proof = self.reader.receipt(ref)
        snapshot = proof.payload["snapshot"]
        observed = "synthetic-selection:" + str(uuid4())
        self.observations.rows[observed] = IssueSelection(
            self.scope,
            snapshot["issue_id"],
            proof.payload["discovery_digest"],
            self.claim.context,
            "a" * 64,
            time.time() + 300,
        )
        selection = self.selection_authority.issue(
            self.scope,
            snapshot["issue_id"],
            proof.payload["discovery_digest"],
            self.claim.context,
            observed,
        )
        return selection

    def admit(self, issue_id, key=None):
        ref, prose, approval, selected = self.packet(issue_id)
        task = self.intake.admit(ref, prose, approval, selected, key or str(uuid4()))
        # Record fixture knowledge without changing admission or replay history.
        if not hasattr(self, "known_dependencies"):
            self.known_dependencies = {}
        self.known_dependencies[task.id] = ((), "normal")
        return task

    def admission_readback(self, discovery_ref, operation_key):
        original = self.reader.receipt(discovery_ref)
        _, ref = self.reader.readback(
            original.subject,
            readback_context("admission", operation_key, discovery_ref=discovery_ref),
        )
        return ref

    def queue(self, task, key=None):
        key = key or str(uuid4())
        if not hasattr(self, "_queue_decisions"):
            self._queue_decisions = {}
        if key not in self._queue_decisions:
            self._queue_decisions[key] = self.decision(task)
        with self.db.transaction() as conn:
            replay = conn.execute("SELECT 1 FROM operations WHERE key=?", (key,)).fetchone()
        source = None if replay else self.intake.queue_readback(
            task.id, task.version, task.decision_revision, key)
        result = self.core.transitions.transition(
            self.handle, task.id, "Todo", "enqueue", task.version,
            task.decision_revision, self._queue_decisions[key], {"existing_source": source}, key,
        )
        if not replay:
            seed_known_queue(self, self.core.get_task(self.handle, task.id))
        return result

    def startup_source(self, task, key, *, phase="reserve", attempt_id=None):
        return self.intake.guard(
            task,
            context=readback_context(
                "start",
                key,
                task_id=task.id,
                phase=phase,
                attempt_id=attempt_id,
                expected_version=task.version,
                decision_revision=task.decision_revision,
            ),
        )

    def reserve(self, task, key=None):
        key = key or str(uuid4())
        source = self.startup_source(task, key)
        return self.core.execution.reserve(
            self.handle,
            task.id,
            "fixture",
            task.version,
            task.decision_revision,
            self.decision(task, "In Progress"),
            key,
            readback_ref=source,
        )

    def runtime_proof(self, kind, subject, payload):
        if not hasattr(self, "runtime_facts"):
            self.runtime_facts = ProofFixture()
            self.core.context.evidence = EvidenceRouter(
                [
                    *self.router.routes,
                    EvidenceRoute(
                        "fixture-runtime:",
                        frozenset({"worker_start", "resources_ready"}),
                        self.runtime_facts,
                    ),
                ]
            )
        from ai_task_manager.domains.work.models import Evidence

        ref = "fixture-runtime:" + str(uuid4())
        self.runtime_facts.evidence[ref] = Evidence(kind, self.scope, subject, payload)
        return ref

    def running(self, task):
        """Core lifecycle from authored runtime facts; no actual model/host observation."""
        from ai_task_manager.application.linear_sync.outbound import OutboundDrain

        task = self.queue(task)
        attempt = self.reserve(task)
        task = self.core.get_task(self.handle, task.id)
        key = str(uuid4())
        source = self.startup_source(task, key, phase="start", attempt_id=attempt.id)
        startup = self.runtime_proof(
            "worker_start",
            attempt.id,
            {"task_id": task.id, "host": "fixture", "worker_id": "authored-fixture-worker"},
        )
        attempt = self.core.execution.start(
            self.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            startup,
            self.decision(task, "In Progress"),
            key,
            readback_ref=source,
        )
        task = OutboundDrain(self.core, self.client, self.journal).confirm_pending(
            self.handle, task.id
        )
        ready = self.runtime_proof(
            "resources_ready",
            attempt.id,
            {
                "task_id": task.id,
                "host": "fixture",
                "worker_id": attempt.worker_id,
                "ready": True,
                "worktree_id": "authored-tree:" + attempt.id,
                "database_id": "authored-db:" + attempt.id,
            },
        )
        attempt = self.core.execution.running(
            self.handle, attempt.id, task.version, task.decision_revision, ready, str(uuid4())
        )
        return self.core.get_task(self.handle, task.id), attempt

    def decision(self, task, target="Todo"):
        return self.authority._evidence(
            "decision",
            self.scope,
            task.id,
            {
                "target": target,
                "revision": task.decision_revision,
                "actor": "agent",
            },
            "synthetic-trusted-lead-intent",
        )

    def effects(self):
        with self.db.transaction() as conn:
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
                for table in (
                    "tasks",
                    "issue_bindings",
                    "events",
                    "outbox",
                    "attempts",
                    "resource_leases",
                    "operations",
                )
            }

    def assert_no_provider_mutations(self):
        assert not [query for query, _ in self.provider.requests if "mutation " in query["query"]]
