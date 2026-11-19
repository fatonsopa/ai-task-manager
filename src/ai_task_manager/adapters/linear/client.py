import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import replace
from threading import RLock, local

from ai_task_manager.domains.work.models import IssueStatus, valid_display_identifier

from .connections import AgentConnection
from .errors import LinearError, ScopeMismatch
from .models import CommentSnapshot, IssueSnapshot, VerifiedBinding, canonical_id, revision
from .rate_limits import IDENTITY, retry_seconds

ISSUE_FIELDS = """id identifier title description updatedAt assignee { id } delegate { id app }
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

AGENT_USERS = """query AgentUsers($after: String) {
 organization { id } users(first: 50, after: $after) {
 nodes { id app } pageInfo { hasNextPage endCursor } }
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


NATIVE_TYPE_LABELS = """query NativeTypeLabels($after:String) {
 viewer{id} organization{id} issueLabels(includeArchived:true,first:50,after:$after) {
 nodes{id name isGroup groupType archivedAt team{id} parent{id name isGroup team{id}}}
 pageInfo{hasNextPage endCursor} } }"""
NATIVE_TYPE_READ = (
    "query NativeTypeRead($id:String!) { viewer{id} organization{id} issue(id:$id) { "
    + ISSUE_FIELDS
    + " labels(first:50){nodes{id parent{id}} pageInfo{hasNextPage endCursor}} } }"
)

DECISION_USER = "id app active url organization { id }"
DECISION_ISSUE = ISSUE_FIELDS.replace("assignee { id }", "assignee { " + DECISION_USER + " }")
DECISION_READ = (
    "query DecisionContext($id: String!) { viewer { id } organization { id urlKey } "
    "issue(id:$id) { " + DECISION_ISSUE + " creator { " + DECISION_USER + " } "
    "labels(first:50) { nodes { id } pageInfo { hasNextPage endCursor } } } }"
)
DECISION_LABELS = (
    "query DecisionLabels { "
    'issueLabels(includeArchived:true,filter:{name:{eqIgnoreCase:"Waiting '
    'for Decision"}},first:50) '
    "{ nodes { id name isGroup archivedAt team{id} } pageInfo{hasNextPage endCursor} } }"
)
DECISION_LABEL_UPDATE = (
    "mutation DecisionLabelsUpdate($id:String!,$input:IssueUpdateInput!) { "
    "issueUpdate(id:$id,input:$input) { success issue { "
    + DECISION_ISSUE
    + " labels(first:50) { nodes { id } pageInfo { hasNextPage endCursor } } } } }"
)


_NO_FIRST_PAGE = object()


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
        agent_connection=None,
    ):
        self.transport, self.credentials, self.expected = transport, credentials, expected
        if type(max_pages) is not int or not 0 < max_pages <= 10000:
            raise LinearError("invalid_pagination_limit")
        self.max_pages = max_pages
        self.binding = None
        self.agent_connection = agent_connection
        self.connection = None
        self._authorization_digest = None
        self._binding_lock = RLock()
        self._verification = local()
        self.create_capability = create_capability
        self.comment_capability = comment_capability
        for value in (
            expected.viewer_id,
            expected.workspace_id,
            expected.team_id,
            expected.project_id,
        ):
            canonical_id(value)

    def current_binding(self):
        with self._binding_lock:
            if self.binding is None:
                raise LinearError("binding_not_verified")
            return self.binding

    def request(self, query, variables, *, mutation=False, _before_dispatch=None, _binding=None):
        credentials = self.credentials()
        authorization_digest = hashlib.sha256(credentials.authorization().encode()).hexdigest()
        expected_digest = getattr(self._verification, "digest", None) or self._authorization_digest
        if expected_digest is not None and authorization_digest != expected_digest:
            raise LinearError("credential_binding_changed")
        if mutation and _binding is not None:
            current = self.current_binding()
            if _binding is not None and (
                current.expected != _binding.expected or current.states != _binding.states
            ):
                raise LinearError("binding_not_verified")
            expected_digest = (
                getattr(self._verification, "digest", None) or self._authorization_digest
            )
            if expected_digest is not None and authorization_digest != expected_digest:
                raise LinearError("credential_binding_changed")
        admission = getattr(self.transport, "admission", None)
        gate = admission(query, variables, credentials, principal=self.expected.viewer_id) if (
            callable(admission)
        ) else nullcontext(False)
        with gate as admitted:
            # Admission may wait, so credentials/current binding precede the final
            # effect fence again. No quota wait can occur after that fence.
            if admitted and self.credentials().authorization() != credentials.authorization():
                raise LinearError("credential_binding_changed")
            if (
                admitted and mutation and _binding is not None
                and self.current_binding() != _binding
            ):
                raise LinearError("binding_not_verified")
            if _before_dispatch is not None:
                _before_dispatch()
            try:
                response = self.transport.send(query, variables, credentials)
            except LinearError as exc:
                raise LinearError(
                    exc.category, uncertain=mutation and exc.uncertain, retry_after=exc.retry_after
                ) from exc
        if response.status in {401, 403}:
            raise LinearError("authentication")
        if response.status == 429:
            retry_after = retry_seconds(response.headers)
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

    def paginate(self, query, variables, path, *, first_page=_NO_FIRST_PAGE):
        after, seen_cursors, seen_ids, nodes = None, set(), set(), []
        for index in range(self.max_pages):
            data = first_page if index == 0 and first_page is not _NO_FIRST_PAGE else (
                self.request(query, {**variables, "after": after})
            )
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
        # Only revalidation and binding consumption coordinate here. Ordinary
        # provider reads do not hold this guard while performing network I/O.
        with self._binding_lock:
            self.binding = None
            self._verification.digest = hashlib.sha256(
                self.credentials().authorization().encode()
            ).hexdigest()
            try:
                result = self._verify_binding()
                if hashlib.sha256(self.credentials().authorization().encode()).hexdigest() != (
                    self._verification.digest
                ):
                    raise LinearError("credential_binding_changed")
                self._authorization_digest = self._verification.digest
                return result
            except BaseException:
                self.binding = None
                raise
            finally:
                self._verification.digest = None

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
        teams = self.paginate(
            PROJECT_TEAMS, {"project": expected.project_id}, ("project", "teams"), first_page=data
        )
        if expected.team_id not in {x["id"] for x in teams}:
            raise ScopeMismatch()
        states = self.paginate(
            STATES, {"team": expected.team_id}, ("team", "states"), first_page=data
        )
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

    def check_connection(self, scope=None, *, refresh=False):
        binding = self.current_binding()
        if self.agent_connection is None:
            raise LinearError("agent_connection_missing")
        selected = self.agent_connection()
        if not isinstance(selected, AgentConnection):
            raise LinearError("invalid_agent_connection")
        if (selected.workspace_id, selected.principal_id) != (
            self.expected.workspace_id,
            self.expected.viewer_id,
        ):
            raise LinearError("agent_connection_scope_mismatch")
        if scope is not None:
            selected.check_scope(scope)
            if scope.project != self.expected.project_id:
                raise LinearError("agent_connection_scope_mismatch")
        if self.connection is not None and selected != self.connection:
            raise LinearError("agent_connection_changed")
        if refresh or self.connection is None:
            # users is scoped by the authenticated workspace. Inspect every page and
            # reject duplicate IDs instead of choosing the first matching identity.
            after, seen, matches, cursors = None, set(), [], set()
            for _ in range(self.max_pages):
                data = self.request(AGENT_USERS, {"after": after})
                try:
                    if data["organization"]["id"] != selected.workspace_id:
                        raise LinearError("agent_connection_scope_mismatch")
                    users = data["users"]
                    page = users["pageInfo"]
                    if (
                        not isinstance(users["nodes"], list)
                        or type(page["hasNextPage"]) is not bool
                    ):
                        raise ValueError()
                    for item in users["nodes"]:
                        identity = canonical_id(item["id"])
                        if identity in seen:
                            raise LinearError("ambiguous_agent_identity")
                        seen.add(identity)
                        if identity == selected.agent_id:
                            matches.append(item)
                    if not page["hasNextPage"]:
                        break
                    cursor = page["endCursor"]
                    if not isinstance(cursor, str) or not cursor or cursor in cursors:
                        raise ValueError()
                    cursors.add(cursor)
                    after = cursor
                except (KeyError, TypeError, ValueError) as exc:
                    raise LinearError("incomplete_agent_identity") from exc
            else:
                raise LinearError("pagination_limit")
            if len(matches) != 1 or matches[0].get("app") is not True:
                raise LinearError("agent_identity_not_application")
            # Detect an operator replacement during the provider lookup.
            if self.agent_connection() != selected:
                raise LinearError("agent_connection_changed")
            self.connection = selected
        with self._binding_lock:
            current = self.current_binding()
            if current.expected != binding.expected or current.states != binding.states:
                raise LinearError("binding_not_verified")
            self.binding = replace(current, connection=selected)
        return selected

    def require_delegate(self, snapshot):
        connection = self.check_connection()
        if (
            snapshot.delegate_id != connection.agent_id
            or snapshot.delegate_is_application is not True
        ):
            raise LinearError("agent_delegate_mismatch", uncertain=True)
        return snapshot

    def snapshot(self, data, *, allow_unknown=False):
        binding = self.current_binding()
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
            if binding.states.get(status) != state_id:
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
                canonical_id(data["delegate"]["id"]) if data.get("delegate") else None,
                data["delegate"].get("app") if data.get("delegate") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("incomplete_issue") from exc

    def read_issue(self, issue_id):
        canonical_id(issue_id)
        return self.snapshot(self.request(READ, {"id": issue_id}).get("issue"), allow_unknown=True)

    def native_type(self, task_type):
        """Resolve one existing applicable single-select Type group and approved leaf."""
        try:
            return self._native_type(task_type)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise LinearError("native_type_metadata_incomplete") from exc

    def _native_type(self, task_type):
        if task_type not in {"Bug", "Chore", "Decision", "Feature", "Improvement", "Release"}:
            raise LinearError("native_type_not_supported")
        self.check_connection()
        after, rows, cursors, seen = None, [], set(), set()
        for _ in range(self.max_pages):
            data = self.request(NATIVE_TYPE_LABELS, {"after": after})
            if (data["viewer"]["id"], data["organization"]["id"]) != (
                self.expected.viewer_id,
                self.expected.workspace_id,
            ):
                raise LinearError("native_type_scope_mismatch")
            page = data["issueLabels"]
            for row in page["nodes"]:
                identifier = canonical_id(row["id"])
                if identifier in seen:
                    raise LinearError("native_type_metadata_ambiguous")
                seen.add(identifier)
                rows.append(row)
            if page["pageInfo"]["hasNextPage"] is False:
                break
            cursor = page["pageInfo"]["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise LinearError("native_type_lookup_incomplete")
            cursors.add(cursor)
            after = cursor
        else:
            raise LinearError("native_type_lookup_incomplete")

        def team(row):
            return row["team"]["id"] if row["team"] else None

        groups = [
            r
            for r in rows
            if r["name"].casefold() == "type"
            and r["archivedAt"] is None
            and r["isGroup"] is True
            and r["groupType"] == "singleSelect"
            and r["parent"] is None
            and team(r) in {None, self.expected.team_id}
        ]
        if len(groups) != 1:
            raise LinearError("native_type_group_missing_or_ambiguous")
        group = groups[0]
        leaves = [
            r
            for r in rows
            if r["name"].casefold() == task_type.casefold()
            and r["archivedAt"] is None
            and r["isGroup"] is False
            and team(r) == team(group)
            and (r["parent"] or {}).get("id") == group["id"]
            and r["parent"].get("isGroup") is True
            and r["parent"].get("name", "").casefold() == "type"
            and team(r["parent"]) == team(group)
        ]
        if len(leaves) != 1:
            raise LinearError("native_type_leaf_missing_or_ambiguous")
        self.check_connection()
        return {"task_type": task_type, "group_id": group["id"], "label_id": leaves[0]["id"]}

    def verify_native_type(self, issue_id, binding):
        try:
            return self._verify_native_type(issue_id, binding)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise LinearError("native_type_readback_incomplete", uncertain=True) from exc
        except LinearError as exc:
            raise LinearError(exc.category, uncertain=True, retry_after=exc.retry_after) from exc

    def _verify_native_type(self, issue_id, binding):
        if self.native_type(binding["task_type"]) != binding:
            raise LinearError("native_type_metadata_changed", uncertain=True)
        data = self.request(NATIVE_TYPE_READ, {"id": canonical_id(issue_id)})
        if (data["viewer"]["id"], data["organization"]["id"]) != (
            self.expected.viewer_id,
            self.expected.workspace_id,
        ):
            raise LinearError("native_type_scope_mismatch", uncertain=True)
        issue = data["issue"]
        snapshot = self.require_delegate(self.snapshot(issue, allow_unknown=True))
        labels = issue["labels"]
        typed = [
            r["id"]
            for r in labels["nodes"]
            if (r.get("parent") or {}).get("id") == binding["group_id"]
        ]
        if labels["pageInfo"]["hasNextPage"] is not False or typed != [binding["label_id"]]:
            raise LinearError("native_type_readback_conflict", uncertain=True)
        self.check_connection()
        return snapshot

    def create_issue(self, task, candidate_id=None, *, native_type=None, _prepare_dispatch=None):
        dispatch = _prepare_dispatch() if _prepare_dispatch is not None else None
        binding = self.current_binding()
        connection = self.check_connection(refresh=True)
        values = {
            "delegateId": connection.agent_id,
            "teamId": self.expected.team_id,
            "projectId": self.expected.project_id,
            "assigneeId": self.expected.viewer_id,
            "stateId": binding.states["Backlog"],
            "title": task.title,
            "description": task.body,
        }
        if native_type is not None:
            values["labelIds"] = [canonical_id(native_type["label_id"])]
        if candidate_id is not None:
            if self.create_capability is None:
                raise LinearError("creation_uuid_capability_missing")
            values["id"] = canonical_id(candidate_id)
        result = self.request(
            CREATE, {"input": values}, mutation=True, _before_dispatch=dispatch, _binding=binding
        ).get("issueCreate")
        return self.require_delegate(self.mutation_snapshot(result))

    def update_issue(self, task, kind, *, _prepare_dispatch=None):
        dispatch = _prepare_dispatch() if _prepare_dispatch is not None else None
        canonical_id(task.issue_id)
        binding = self.current_binding()
        connection = self.check_connection(refresh=True)
        values = {
            "stateId": binding.states[task.desired_status],
            "assigneeId": self.expected.viewer_id,
            "delegateId": connection.agent_id,
        }
        if kind == "update_prose":
            values.update(title=task.title, description=task.body)
        result = self.request(
            UPDATE, {"id": task.issue_id, "input": values}, mutation=True,
            _before_dispatch=dispatch,
            _binding=binding,
        ).get("issueUpdate")
        return self.require_delegate(self.mutation_snapshot(result))

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
        self.check_connection(refresh=True)
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

    def decision_context(self, issue_id):
        """Current canonical recipient and exact scoped label; never create metadata."""
        from urllib.parse import urlsplit

        self.check_connection(refresh=True)
        value = self.request(DECISION_READ, {"id": canonical_id(issue_id)})
        if (value["viewer"]["id"], value["organization"]["id"]) != (
            self.expected.viewer_id,
            self.expected.workspace_id,
        ):
            raise LinearError("decision_recipient_scope_mismatch")
        issue = value["issue"]
        snapshot = self.require_delegate(self.snapshot(issue))
        creator = issue.get("creator")
        if not isinstance(creator, dict) or type(creator.get("app")) is not bool:
            raise LinearError("decision_creator_unavailable")
        recipient = issue.get("assignee") if creator["app"] else creator
        if (
            not isinstance(recipient, dict)
            or recipient.get("app") is not False
            or recipient.get("active") is not True
            or recipient.get("organization", {}).get("id") != self.expected.workspace_id
        ):
            raise LinearError("decision_human_recipient_unavailable")
        recipient_id = canonical_id(recipient["id"])
        url = recipient["url"]
        parsed = urlsplit(url)
        workspace = value["organization"]["urlKey"]
        if (
            parsed.scheme != "https"
            or parsed.netloc != "linear.app"
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/" + workspace + "/profiles/")
            or not parsed.path.removeprefix("/" + workspace + "/profiles/")
            or any(not 0x21 <= ord(c) <= 0x7E for c in url)
        ):
            raise LinearError("decision_profile_url_invalid")
        labels = self.request(DECISION_LABELS, {})["issueLabels"]
        if labels["pageInfo"]["hasNextPage"] or issue["labels"]["pageInfo"]["hasNextPage"]:
            raise LinearError("decision_label_lookup_incomplete")
        candidates = [
            row
            for row in labels["nodes"]
            if row["name"].casefold() == "waiting for decision"
            and row.get("archivedAt") is None
            and row["isGroup"] is False
            and (row.get("team") is None or row["team"]["id"] == self.expected.team_id)
        ]
        if len(candidates) != 1:
            raise LinearError("decision_label_missing_or_ambiguous")
        return {
            "recipient": {"id": recipient_id, "url": url},
            "label_id": canonical_id(candidates[0]["id"]),
            "label_name": candidates[0]["name"],
            "labels": sorted(canonical_id(row["id"]) for row in issue["labels"]["nodes"]),
            "issue": snapshot,
        }

    def change_decision_label(self, issue_id, label_id, *, add):
        result = self.request(
            DECISION_LABEL_UPDATE,
            {
                "id": canonical_id(issue_id),
                "input": {"addedLabelIds" if add else "removedLabelIds": [canonical_id(label_id)]},
            },
            mutation=True,
        ).get("issueUpdate")
        if not result or result.get("success") is not True:
            raise LinearError("decision_label_effect_unconfirmed", uncertain=True)
        issue = result["issue"]
        snapshot = self.require_delegate(self.snapshot(issue))
        if issue["labels"]["pageInfo"]["hasNextPage"]:
            raise LinearError("decision_label_lookup_incomplete", uncertain=True)
        return {
            "revision": snapshot.revision,
            "labels": sorted(canonical_id(row["id"]) for row in issue["labels"]["nodes"]),
        }
