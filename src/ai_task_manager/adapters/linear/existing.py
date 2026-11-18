"""Metadata-first discovery; only verified own prose becomes a durable receipt."""

import re

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.existing import digest, readback_context
from ai_task_manager.domains.work.models import IssueStatus, valid_display_identifier

from .errors import LinearError
from .models import canonical_id, revision

META = """id identifier createdAt updatedAt archivedAt trashed assignee { id }
 team { id key } project { id } state { id name }"""
READ_META = "query ExistingMetadata($id: String!) { issue(id: $id) { " + META + " } }"
READ_PROSE = (
    "query ExistingProse($id: String!) { issue(id: $id) { " + META + " title description } }"
)
READ_TITLE = "query ExistingTitle($id: String!) { issue(id: $id) { " + META + " title } }"
FIND = (
    """query ExistingSearch($filter: IssueFilter!, $first: Int!, $after: String) {
 issues(filter: $filter, first: $first, after: $after) { nodes { """
    + META
    + """ }
 pageInfo { hasNextPage endCursor } } }"""
)
HISTORY = """query ExistingStateHistory($id: String!, $after: String) {
 issue(id: $id) { id stateHistory(first: 50, after: $after) {
 nodes { id stateId startedAt endedAt state { id name type createdAt updatedAt team { id } } }
 pageInfo { hasNextPage endCursor } } } }"""


class ExistingReader:
    def __init__(self, client, journal, scope, claim_store):
        self.client, self.journal, self.scope, self.claim_store = (
            client,
            journal,
            scope,
            claim_store,
        )
        expected = client.expected
        if client.binding is None or (
            scope.provider,
            scope.workspace,
            scope.principal,
            scope.project,
        ) != ("linear", expected.workspace_id, expected.viewer_id, expected.project_id):
            raise AccessDenied("Discovery requires the verified provider scope")

    def metadata(self, raw):
        binding = self.client.current_binding()
        expected = self.client.expected
        try:
            if (
                not isinstance(raw, dict)
                or (raw.get("team") or {}).get("id") != expected.team_id
                or (raw.get("team") or {}).get("key") != expected.team_key
                or (raw.get("project") or {}).get("id") != expected.project_id
                or (raw.get("assignee") or {}).get("id") != expected.viewer_id
            ):
                raise AccessDenied("Issue is unavailable in this managed scope")
            if raw.get("archivedAt") is not None or raw.get("trashed") is True:
                raise AccessDenied("Archived or removed issues cannot be admitted")
            label = raw["identifier"]
            if not valid_display_identifier(label) or not re.fullmatch(
                re.escape(expected.team_key) + r"-[1-9][0-9]*", label
            ):
                raise ValueError()
            state_id = canonical_id(raw["state"]["id"])
            status = raw["state"]["name"]
            if not isinstance(status, str) or binding.states.get(status) != state_id:
                raise LinearError("unknown_workflow_state")
            return {
                "issue_id": canonical_id(raw["id"]),
                "identifier": label,
                "team_id": expected.team_id,
                "state_id": state_id,
                "status": status,
                "created_at": revision(raw["createdAt"]),
                "source_revision": revision(raw["updatedAt"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("incomplete_existing_issue") from exc

    def _read(self, query, issue):
        raw = self.client.request(query, {"id": issue}).get("issue")
        return self.metadata(raw), raw

    def find(self, query, limit=20, cursor=None):
        expected = self.client.expected
        filters = {
            "and": [
                {"title": {"containsIgnoreCase": query}},
                {"assignee": {"id": {"eq": expected.viewer_id}}},
                {"team": {"id": {"eq": expected.team_id}}},
                {"project": {"id": {"eq": expected.project_id}}},
            ]
        }
        data = self.client.request(FIND, {"filter": filters, "first": limit, "after": cursor})
        try:
            page, nodes = data["issues"]["pageInfo"], data["issues"]["nodes"]
            if (
                not isinstance(nodes, list)
                or len(nodes) > limit
                or type(page["hasNextPage"]) is not bool
                or page["hasNextPage"]
                and (
                    not isinstance(page["endCursor"], str)
                    or not page["endCursor"]
                    or page["endCursor"] == cursor
                    or len(page["endCursor"]) > 4096
                )
            ):
                raise ValueError()
            result, seen = [], set()
            for raw in nodes:
                issue_id = canonical_id(raw["id"])
                if issue_id in seen:
                    raise ValueError()
                seen.add(issue_id)
                try:
                    metadata = self.metadata(raw)
                except AccessDenied:
                    continue
                current, title = self._read(READ_TITLE, issue_id)
                if metadata != current:
                    raise LinearError("existing_issue_changed")
                if not isinstance(title.get("title"), str):
                    raise ValueError()
                result.append(
                    {
                        key: current[key]
                        for key in ("issue_id", "identifier", "status", "source_revision")
                    }
                    | {"title": title["title"]}
                )
            return {
                "candidates": result,
                "has_more": page["hasNextPage"],
                "next_cursor": page["endCursor"] if page["hasNextPage"] else None,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("incomplete_pagination") from exc

    def _history(self, issue_id):
        spans, seen, cursors, after = [], set(), set(), None
        for _ in range(min(100, self.client.max_pages)):
            data = self.client.request(HISTORY, {"id": issue_id, "after": after})
            try:
                issue = data["issue"]
                if canonical_id(issue["id"]) != issue_id:
                    raise ValueError()
                page, nodes = issue["stateHistory"]["pageInfo"], issue["stateHistory"]["nodes"]
                if not isinstance(nodes, list) or len(nodes) > 50:
                    raise ValueError()
                for node in nodes:
                    span_id = node["id"]
                    if (
                        not isinstance(span_id, str)
                        or not 1 <= len(span_id) <= 256
                        or span_id in seen
                    ):
                        raise ValueError()
                    seen.add(span_id)
                    state = node.get("state")
                    spans.append(
                        {
                            "id": span_id,
                            "state_id": canonical_id(node["stateId"]),
                            "started_at": revision(node["startedAt"]),
                            "ended_at": revision(node["endedAt"]) if node.get("endedAt") else None,
                            "state": {
                                "id": canonical_id(state["id"]),
                                "name": state["name"],
                                "type": state["type"],
                                "team_id": canonical_id(state["team"]["id"]),
                                "created_at": revision(state["createdAt"]),
                                "updated_at": revision(state["updatedAt"]),
                            }
                            if state is not None
                            else None,
                        }
                    )
                if type(page["hasNextPage"]) is not bool:
                    raise ValueError()
                if not page["hasNextPage"]:
                    return sorted(spans, key=lambda item: (item["started_at"], item["id"]))
                after = page["endCursor"]
                if not isinstance(after, str) or not after or after in cursors:
                    raise ValueError()
                cursors.add(after)
            except (KeyError, TypeError, ValueError) as exc:
                raise LinearError("incomplete_state_history") from exc
        raise LinearError("state_history_limit")

    def _snapshot(self, issue):
        if not valid_display_identifier(issue):
            raise AccessDenied("A canonical issue UUID or provider identifier is required")
        before, _ = self._read(READ_META, issue)
        current, raw = self._read(READ_PROSE, before["issue_id"])
        if current != before:
            raise LinearError("existing_issue_changed")
        if not isinstance(raw.get("title"), str) or not isinstance(raw.get("description"), str):
            raise LinearError("incomplete_existing_prose")
        history = self._history(current["issue_id"])
        final, _ = self._read(READ_META, current["issue_id"])
        if final != current:
            raise LinearError("existing_issue_changed")
        snapshot = {
            **current,
            "title": raw["title"],
            "body": raw["description"],
            "history": history,
        }
        return snapshot

    def _receipt(self, snapshot, kind, context=None):
        payload = {
            "snapshot": snapshot,
            "claim_store": self.claim_store,
            "discovery_digest": digest(snapshot),
            "observed_at": self.journal.clock(),
        }
        if context is not None:
            payload["context"] = context
        with self.journal.transaction() as conn:
            ref = self.journal._proof(
                conn, kind, self.scope, snapshot["issue_id"], payload
            )
        return ref

    def inspect(self, issue):
        snapshot = self._snapshot(issue)
        return snapshot, self._receipt(snapshot, "existing_issue_snapshot")

    def readback(self, issue, context):
        try:
            if not isinstance(context, dict) or context != readback_context(**context):
                raise EvidenceRequired("Exact readback context is required")
        except (TypeError, ValueError) as exc:
            raise EvidenceRequired("Exact readback context is required") from exc
        snapshot = self._snapshot(issue)
        return snapshot, self._receipt(snapshot, "existing_issue_readback", context)

    def receipt(self, ref):
        proof = self.journal.verify_evidence(ref)
        if (
            proof.kind != "existing_issue_snapshot"
            or proof.scope != self.scope
            or proof.expires_at is None
            or proof.expires_at <= self.journal.clock()
            or proof.payload.get("claim_store") != self.claim_store
            or proof.payload.get("discovery_digest") != digest(proof.payload.get("snapshot"))
        ):
            raise EvidenceRequired("Discovery belongs to another issue, scope or installation")
        return proof

    def fence_receipt(self, task, snapshot=None):
        if snapshot is not None:
            payload = {
                "issue_id": snapshot["issue_id"],
                "status": snapshot["status"],
                "source_revision": snapshot["source_revision"],
                "title": snapshot["title"],
                "body": snapshot["body"],
                "assigned_to_owner": True,
            }
        else:
            raw = self.client.request(READ_META, {"id": task.issue_id}).get("issue")
            try:
                self.metadata(raw)
            except AccessDenied:
                try:
                    if canonical_id(raw["id"]) != task.issue_id:
                        raise ValueError()
                    status = raw["state"]["name"]
                    payload = {
                        "issue_id": task.issue_id,
                        "source_revision": revision(raw["updatedAt"]),
                        "status": status if status in set(IssueStatus) else "unknown",
                        "assigned_to_owner": False,
                    }
                except (KeyError, TypeError, ValueError) as exc:
                    raise LinearError("incomplete_scope_observation") from exc
            else:
                raise EvidenceRequired("Ownership changed during reconciliation; inspect again")
        payload.update({"actor_kind": "unknown", "actor_authorized": False})
        with self.journal.transaction() as conn:
            return self.journal._proof(conn, "external_observation", self.scope, task.id, payload)
