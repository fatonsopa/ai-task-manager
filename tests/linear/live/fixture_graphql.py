"""Static fixture operations over the product transport and error parser."""

import hashlib
import time
from dataclasses import dataclass

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.models import (
    CommentCreateCapability,
    ExpectedBinding,
    IssueCreateCapability,
)
from ai_task_manager.config.files import digest

from .fixture_actions import ActionJournal
from .fixture_http import FixtureRequestTransport
from .ledger import DriverRefused

OPERATIONS = {
    "identity": (
        "query FixtureIdentity($team: String!) { viewer { id admin } organization"
        " { id } team(id: $team) { id key } }"
    ),
    "project_create": (
        "mutation FixtureProjectCreate($input: ProjectCreateInput!) { "
        "projectCreate(input: $input) { success project { id } } }"
    ),
    "project_metadata": (
        "query FixtureProjectMetadata($id: String!, $after: String) { viewer { id"
        " } organization { id } project(id: $id) { id creator { id } lead { id } "
        "archivedAt trashed updatedAt teams(first: 50, after: $after) { nodes { "
        "id } pageInfo { hasNextPage endCursor } } } }"
    ),
    "project_marker": (
        "query FixtureProjectMarker($id: String!) { project(id: $id) { id "
        "updatedAt description content } }"
    ),
    "project_archive": (
        "mutation FixtureProjectArchive($id: String!) { projectArchive(id: $id, "
        "trash: false) { success entity { id archivedAt } } }"
    ),
    "webhook_create": (
        "mutation FixtureWebhookCreate($input: WebhookCreateInput!) { "
        "webhookCreate(input: $input) { success webhook { id enabled } } }"
    ),
    "webhook_metadata": (
        "query FixtureWebhookMetadata($id: String!) { viewer { id } organization "
        "{ id } webhook(id: $id) { id creator { id } team { id } teamIds "
        "allPublicTeams enabled resourceTypes url updatedAt archivedAt } }"
    ),
    "webhook_marker": (
        "query FixtureWebhookMarker($id: String!) { webhook(id: $id) { id updatedAt label } }"
    ),
    "webhook_update": (
        "mutation FixtureWebhookEnabled($id: String!, $input: "
        "WebhookUpdateInput!) { webhookUpdate(id: $id, input: $input) { success "
        "webhook { id enabled } } }"
    ),
    "webhook_delete": (
        "mutation FixtureWebhookDelete($id: String!) { webhookDelete(id: $id) { "
        "success entityId } }"
    ),
    "webhook_ids": (
        "query FixtureWebhookIds($after: String) { viewer { id } organization { "
        "id } webhooks(first: 50, after: $after, includeArchived: true) { nodes {"
        " id } pageInfo { hasNextPage endCursor } } }"
    ),
    "issue_metadata": (
        "query FixtureIssueMetadata($id: String!) { viewer { id } organization { "
        "id } issue(id: $id) { id creator { id } assignee { id } team { id key } "
        "project { id } state { id } archivedAt updatedAt } }"
    ),
    "issue_marker": (
        "query FixtureIssueMarker($id: String!) { issue(id: $id) { id updatedAt "
        "title description } }"
    ),
    "issue_archive": (
        "mutation FixtureIssueArchive($id: String!) { issueArchive(id: $id, "
        "trash: false) { success entity { id archivedAt } } }"
    ),
}
REQUIRED = {
    "Project": {
        "id",
        "creator",
        "lead",
        "archivedAt",
        "trashed",
        "updatedAt",
        "teams",
        "description",
        "content",
    },
    "Webhook": {
        "id",
        "creator",
        "team",
        "teamIds",
        "allPublicTeams",
        "enabled",
        "resourceTypes",
        "url",
        "updatedAt",
        "archivedAt",
        "label",
    },
    "Issue": {
        "id",
        "creator",
        "assignee",
        "team",
        "project",
        "state",
        "archivedAt",
        "updatedAt",
        "title",
        "description",
    },
    "ProjectPayload": {"success", "project"},
    "ProjectArchivePayload": {"success", "entity"},
    "IssueArchivePayload": {"success", "entity"},
    "WebhookPayload": {"success", "webhook"},
    "DeletePayload": {"success", "entityId"},
    "Query": {"viewer", "organization", "team", "project", "issue", "webhook", "webhooks"},
    "Mutation": {
        "projectCreate",
        "projectArchive",
        "issueArchive",
        "webhookCreate",
        "webhookUpdate",
        "webhookDelete",
    },
}
INPUTS = {
    "ProjectCreateInput": {"id", "name", "description", "content", "teamIds", "leadId"},
    "WebhookCreateInput": {
        "id",
        "enabled",
        "secret",
        "url",
        "resourceTypes",
        "teamId",
        "allPublicTeams",
        "label",
    },
    "WebhookUpdateInput": {"enabled"},
    "IssueCreateInput": {
        "id",
        "title",
        "description",
        "teamId",
        "projectId",
        "assigneeId",
        "stateId",
    },
    "CommentCreateInput": {"id", "issueId", "body"},
}
# The catalog is constant; no caller-supplied query or field name is accepted.
SHAPE = (
    "query FixtureShape { "
    + " ".join(
        f't{i}: __type(name: "{name}") {{ name fields(includeDeprecated: true) {{ name args {{ name }} }} }}'  # noqa: E501
        for i, name in enumerate(REQUIRED)
    )
    + " "
    + " ".join(
        f'i{i}: __type(name: "{name}") {{ name inputFields {{ name type {{ kind name ofType {{ kind name }} }} }} }}'  # noqa: E501
        for i, name in enumerate(INPUTS)
    )
    + " }"
)
ARGUMENTS = {
    ("Query", "webhooks"): {"first", "after", "includeArchived"},
    ("Project", "teams"): {"first", "after"},
    ("Mutation", "projectCreate"): {"input"},
    ("Mutation", "projectArchive"): {"id", "trash"},
    ("Mutation", "issueArchive"): {"id", "trash"},
    ("Mutation", "webhookCreate"): {"input"},
    ("Mutation", "webhookUpdate"): {"id", "input"},
    ("Mutation", "webhookDelete"): {"id"},
}


@dataclass(frozen=True)
class Capabilities:
    schema_sha256: str
    checked_at: float


class FixtureGraphQL:
    def __init__(
        self, ledger, transport, credentials, *, team_key="TST", max_pages=8, clock=time.time
    ):
        self.ledger, self.clock, self.max_pages = ledger, clock, max_pages
        if type(max_pages) is not int or not 0 < max_pages <= 32:
            raise DriverRefused("invalid_fixture_page_bound")
        self._credentials, self._pin = credentials, None
        scope = ledger.scope
        self.client = LinearClient(
            FixtureRequestTransport(ledger, transport),
            self._bound_credentials,
            ExpectedBinding(scope.owner, scope.workspace, scope.team, team_key, scope.project),
            max_pages=max_pages,
        )
        self.capabilities = None
        self._shape = None
        self.actions = ActionJournal(ledger)

    def _bound_credentials(self):
        credentials = self._credentials()
        current = hashlib.sha256(credentials.authorization().encode()).hexdigest()
        if self._pin is not None and current != self._pin:
            raise DriverRefused("fixture_credentials_changed")
        return credentials

    def _identity(self, data):
        expected = self.ledger.scope
        viewer, organization = data.get("viewer"), data.get("organization")
        if (
            not isinstance(viewer, dict)
            or not isinstance(organization, dict)
            or (viewer.get("id"), organization.get("id")) != (expected.owner, expected.workspace)
        ):
            raise DriverRefused("fixture_identity_mismatch")

    def preflight(self, *, cleanup_key=None, recovery_key=None, action_key=None):
        self._pin = hashlib.sha256(self._bound_credentials().authorization().encode()).hexdigest()
        self._read_charge(cleanup_key, recovery_key, action_key)
        shape = self.client.request(SHAPE, {})
        try:
            for i, (name, fields) in enumerate(REQUIRED.items()):
                item = shape[f"t{i}"]
                if item["name"] != name or not fields <= {f["name"] for f in item["fields"]}:
                    raise ValueError()
                for field in item["fields"]:
                    needed = ARGUMENTS.get((name, field["name"]), set())
                    if not needed <= {a["name"] for a in field["args"]}:
                        raise ValueError()
            for i, (name, fields) in enumerate(INPUTS.items()):
                item = shape[f"i{i}"]
                actual = {f["name"]: f["type"] for f in item["inputFields"]}
                if item["name"] != name or not fields <= actual.keys():
                    raise ValueError()
                if "id" in fields and actual["id"] != {
                    "kind": "SCALAR",
                    "name": "String",
                    "ofType": None,
                }:
                    raise ValueError()
        except (KeyError, TypeError, ValueError) as error:
            raise DriverRefused("fixture_shape_unsupported") from error
        self._read_charge(cleanup_key, recovery_key, action_key)
        identity = self.client.request(OPERATIONS["identity"], {"team": self.ledger.scope.team})
        self._identity(identity)
        if identity.get("viewer", {}).get("admin") is not True or identity.get("team") != {
            "id": self.ledger.scope.team,
            "key": self.client.expected.team_key,
        }:
            raise DriverRefused("fixture_admin_scope_required")
        self.capabilities = Capabilities(digest(shape), self.clock())
        self._shape = shape
        return self.capabilities

    def product_capabilities(self):
        self.require_ready()
        values = {self._shape[f"i{i}"]["name"]: self._shape[f"i{i}"] for i in range(len(INPUTS))}
        return (
            IssueCreateCapability.from_introspection(values["IssueCreateInput"]),
            CommentCreateCapability.from_introspection(values["CommentCreateInput"]),
        )

    def _read_charge(self, cleanup_key=None, recovery_key=None, action_key=None):
        if sum(key is not None for key in (cleanup_key, recovery_key, action_key)) > 1:
            raise DriverRefused("ambiguous_read_context")
        if cleanup_key is not None:
            self.ledger.cleanup_read(cleanup_key)
        elif recovery_key is not None:
            self.ledger.create_recovery_read(recovery_key)
        elif action_key is not None:
            self.actions.recovery_read(action_key)
        else:
            self.ledger.request("read")

    def require_ready(self):
        if self.capabilities is None or not 0 <= self.clock() - self.capabilities.checked_at < 300:
            raise DriverRefused("fixture_preflight_required")
        self._bound_credentials()

    def refresh_if_needed(self, *, cleanup_key=None, recovery_key=None, action_key=None):
        if self.capabilities is None or not 0 <= self.clock() - self.capabilities.checked_at < 300:
            return self.preflight(
                cleanup_key=cleanup_key, recovery_key=recovery_key, action_key=action_key
            )
        self._bound_credentials()
        return self.capabilities

    def request(
        self,
        operation,
        variables,
        *,
        cleanup_key=None,
        recovery_key=None,
        action_key=None,
        effect=None,
    ):
        self.require_ready()
        if operation not in OPERATIONS:
            raise DriverRefused("unknown_fixture_operation")
        mutation = OPERATIONS[operation].startswith("mutation ")
        if mutation:
            self.actions.consume(effect, operation, variables)
        else:
            if recovery_key is not None:
                row = self.ledger.get(recovery_key)
                if variables.get("id") != row["candidate"] or operation not in {
                    row["kind"] + "_metadata",
                    row["kind"] + "_marker",
                }:
                    raise DriverRefused("recovery_read_context_mismatch")
            if action_key is not None:
                row = self.actions.get(action_key)
                if variables.get("id") != row["resource_id"] or operation not in {
                    "webhook_metadata",
                    "webhook_marker",
                }:
                    raise DriverRefused("ordinary_recovery_context_mismatch")
            self._read_charge(cleanup_key, recovery_key, action_key)
        return self.client.request(OPERATIONS[operation], variables, mutation=mutation)
