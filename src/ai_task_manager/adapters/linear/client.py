import hashlib
import json
import re

from ai_task_manager.domains.work.models import IssueStatus, valid_display_identifier

from .errors import LinearError, ScopeMismatch
from .models import CommentSnapshot, IssueSnapshot, VerifiedBinding, canonical_id, revision

ISSUE_FIELDS = """id identifier title description updatedAt assignee { id }
 team { id key } project { id } state { id name }"""
READ = "query ReadIssue($id: String!) { issue(id: $id) { " + ISSUE_FIELDS + " } }"
CREATE = (
    "mutation CreateIssue($input: IssueCreateInput!) { "
    "issueCreate(input: $input) { success issue { " + ISSUE_FIELDS + " } } }"
)
UPDATE = (
    "mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) { "
    "issueUpdate(id: $id, input: $input) { success issue { " + ISSUE_FIELDS + " } } }"
)
IDENTITY = """query VerifyBinding($team: String!, $project: String!) {
 viewer { id } organization { id } team(id: $team) { id key } project(id: $project) { id }
}"""
STATES = """query States($team: String!, $after: String) {
 team(id: $team) { states(first: 50, after: $after) {
 nodes { id name } pageInfo { hasNextPage endCursor } } }
}"""
PROJECT_TEAMS = """query ProjectTeams($project: String!, $after: String) {
 project(id: $project) { teams(first: 50, after: $after) {
 nodes { id } pageInfo { hasNextPage endCursor } } }
}"""
COMMENT_FIELDS = "id body updatedAt user { id } issue { " + ISSUE_FIELDS + " }"
COMMENT_READ = "query ReadComment($id: String!) { comment(id: $id) { " + COMMENT_FIELDS + " } }"
COMMENT_CREATE = (
    "mutation CreateComment($input: CommentCreateInput!) { "
    "commentCreate(input: $input) { success comment { " + COMMENT_FIELDS + " } } }"
)


class LinearClient:
    def __init__(
        self,
        transport,
        credentials,
        expected,
        *,
        max_pages=1000,
        create_capability=None,
        comment_capability=None,
    ):
        self.transport, self.credentials, self.expected = transport, credentials, expected
        if type(max_pages) is not int or not 0 < max_pages <= 10000:
            raise LinearError("invalid_pagination_limit")
        self.max_pages = max_pages
        self.binding = None
        self._authorization_digest = None
        self._preflight_digest = None
        self.create_capability = create_capability
        self.comment_capability = comment_capability
        for value in (
            expected.viewer_id,
            expected.workspace_id,
            expected.team_id,
            expected.project_id,
        ):
            canonical_id(value)

    def request(self, query, variables, *, mutation=False):
        credentials = self.credentials()
        authorization_digest = hashlib.sha256(credentials.authorization().encode()).hexdigest()
        expected_digest = self._preflight_digest or self._authorization_digest
        if expected_digest is not None and authorization_digest != expected_digest:
            raise LinearError("credential_binding_changed")
        try:
            response = self.transport.send(query, variables, credentials)
        except LinearError as exc:
            raise LinearError(
                exc.category, uncertain=mutation and exc.uncertain, retry_after=exc.retry_after
            ) from exc
        if response.status in {401, 403}:
            raise LinearError("authentication")
        if response.status == 429:
            try:
                retry_after = min(3600, max(0, int(response.headers.get("retry-after", "60"))))
            except ValueError:
                retry_after = 60
            raise LinearError("rate_limited", uncertain=mutation, retry_after=retry_after)
        if response.status != 200:
            raise LinearError("http_failure", uncertain=mutation)
        try:
            result = json.loads(response.body)
        except (ValueError, UnicodeError) as exc:
            raise LinearError("malformed_response", uncertain=mutation) from exc
        if not isinstance(result, dict):
            raise LinearError("malformed_response", uncertain=mutation)
        if result.get("errors"):
            if not isinstance(result["errors"], list) or any(
                not isinstance(x, dict) or not isinstance(x.get("extensions", {}), dict)
                for x in result["errors"]
            ):
                raise LinearError("malformed_response", uncertain=mutation)
            codes = {x.get("extensions", {}).get("code") for x in result["errors"]}
            raise LinearError(
                "rate_limited" if "RATELIMITED" in codes else "graphql_failure", uncertain=mutation
            )
        if not isinstance(result.get("data"), dict):
            raise LinearError("missing_data", uncertain=mutation)
        return result["data"]

    def paginate(self, query, variables, path):
        after, seen_cursors, seen_ids, nodes = None, set(), set(), []
        for _ in range(self.max_pages):
            data = self.request(query, {**variables, "after": after})
            try:
                for key in path:
                    data = data[key]
                page, items = data["pageInfo"], data["nodes"]
                if type(page["hasNextPage"]) is not bool or not isinstance(items, list):
                    raise ValueError()
                for item in items:
                    canonical_id(item["id"])
                    if item["id"] in seen_ids:
                        raise LinearError("duplicate_page_node")
                    seen_ids.add(item["id"])
                    nodes.append(item)
                if not page["hasNextPage"]:
                    return nodes
                after = page["endCursor"]
                if not isinstance(after, str) or not after or after in seen_cursors:
                    raise ValueError()
                seen_cursors.add(after)
            except (KeyError, TypeError, ValueError) as exc:
                raise LinearError("incomplete_pagination") from exc
        raise LinearError("pagination_limit")

    def verify_binding(self):
        self.binding = None
        self._preflight_digest = hashlib.sha256(
            self.credentials().authorization().encode()
        ).hexdigest()
        try:
            result = self._verify_binding()
            self._authorization_digest = self._preflight_digest
            return result
        finally:
            self._preflight_digest = None

    def _verify_binding(self):
        expected = self.expected
        data = self.request(IDENTITY, {"team": expected.team_id, "project": expected.project_id})
        try:
            if (
                data["viewer"]["id"] != expected.viewer_id
                or data["organization"]["id"] != expected.workspace_id
                or data["team"]["id"] != expected.team_id
                or data["team"]["key"] != expected.team_key
                or data["project"]["id"] != expected.project_id
            ):
                raise ScopeMismatch()
        except (KeyError, TypeError) as exc:
            raise ScopeMismatch() from exc
        teams = self.paginate(PROJECT_TEAMS, {"project": expected.project_id}, ("project", "teams"))
        if expected.team_id not in {x["id"] for x in teams}:
            raise ScopeMismatch()
        states = self.paginate(STATES, {"team": expected.team_id}, ("team", "states"))
        mapping = {}
        for item in states:
            name = item.get("name")
            if name in mapping:
                raise LinearError("ambiguous_workflow_state")
            mapping[name] = item["id"]
        if not {x.value for x in IssueStatus} <= mapping.keys():
            raise LinearError("missing_workflow_state")
        self.binding = VerifiedBinding(expected, mapping)
        return self.binding

    def snapshot(self, data, *, allow_unknown=False):
        if self.binding is None:
            raise LinearError("binding_not_verified")
        try:
            expected = self.expected
            if (
                data["team"]["id"] != expected.team_id
                or data["team"]["key"] != expected.team_key
                or data["project"]["id"] != expected.project_id
            ):
                raise ScopeMismatch(
                    {
                        "id": canonical_id(data["id"]),
                        "updatedAt": revision(data["updatedAt"]),
                        "teamChanged": data["team"]["id"] != expected.team_id,
                        "projectChanged": data["project"]["id"] != expected.project_id,
                        "scopeDigest": hashlib.sha256(
                            json.dumps(
                                {"team": data["team"]["id"], "project": data["project"]["id"]},
                                sort_keys=True,
                            ).encode()
                        ).hexdigest(),
                    }
                )
            identifier = data["identifier"]
            if not valid_display_identifier(identifier) or not re.fullmatch(
                re.escape(expected.team_key) + r"-[1-9][0-9]*", identifier
            ):
                raise LinearError("invalid_display_identifier")
            if not isinstance(data["title"], str) or not isinstance(data["description"], str):
                raise ValueError()
            canonical_id(data["id"])
            state_id = canonical_id(data["state"]["id"])
            status = data["state"]["name"]
            if self.binding.states.get(status) != state_id:
                if not allow_unknown or not isinstance(status, str):
                    raise LinearError("unknown_workflow_state")
                status = f"unknown:{status}"
            assignee = data.get("assignee")
            return IssueSnapshot(
                data["id"],
                identifier,
                data["team"]["id"],
                data["project"]["id"],
                canonical_id(assignee["id"]) if assignee else None,
                data["title"],
                data["description"],
                state_id,
                status,
                revision(data["updatedAt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("incomplete_issue") from exc

    def read_issue(self, issue_id):
        canonical_id(issue_id)
        return self.snapshot(self.request(READ, {"id": issue_id}).get("issue"), allow_unknown=True)

    def create_issue(self, task, candidate_id=None):
        if self.binding is None:
            raise LinearError("binding_not_verified")
        values = {
            "teamId": self.expected.team_id,
            "projectId": self.expected.project_id,
            "assigneeId": self.expected.viewer_id,
            "stateId": self.binding.states["Backlog"],
            "title": task.title,
            "description": task.body,
        }
        if candidate_id is not None:
            if self.create_capability is None:
                raise LinearError("creation_uuid_capability_missing")
            values["id"] = canonical_id(candidate_id)
        result = self.request(CREATE, {"input": values}, mutation=True).get("issueCreate")
        return self.mutation_snapshot(result)

    def update_issue(self, task, kind):
        canonical_id(task.issue_id)
        if self.binding is None:
            raise LinearError("binding_not_verified")
        values = {"stateId": self.binding.states[task.desired_status]}
        if kind == "update_prose":
            values.update(title=task.title, description=task.body)
        result = self.request(UPDATE, {"id": task.issue_id, "input": values}, mutation=True).get(
            "issueUpdate"
        )
        return self.mutation_snapshot(result)

    def mutation_snapshot(self, result):
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearError("mutation_not_confirmed", uncertain=True)
        try:
            return self.snapshot(result.get("issue"))
        except LinearError as exc:
            raise LinearError(exc.category, uncertain=True) from exc

    def comment_snapshot(self, data):
        try:
            if not isinstance(data["body"], str):
                raise ValueError()
            return CommentSnapshot(
                canonical_id(data["id"]),
                data["body"],
                self.snapshot(data["issue"], allow_unknown=True),
                revision(data["updatedAt"]),
                canonical_id(data["user"]["id"]) if data.get("user") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("incomplete_comment") from exc

    def create_comment(self, issue_id, body, candidate_id=None):
        canonical_id(issue_id)
        values = {"issueId": issue_id, "body": body}
        if candidate_id is not None:
            if self.comment_capability is None:
                raise LinearError("comment_uuid_capability_missing")
            values["id"] = canonical_id(candidate_id)
        result = self.request(COMMENT_CREATE, {"input": values}, mutation=True).get("commentCreate")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearError("comment_not_confirmed", uncertain=True)
        try:
            return self.comment_snapshot(result.get("comment"))
        except LinearError as exc:
            raise LinearError(exc.category, uncertain=True) from exc

    def read_comment(self, comment_id):
        canonical_id(comment_id)
        return self.comment_snapshot(self.request(COMMENT_READ, {"id": comment_id}).get("comment"))
