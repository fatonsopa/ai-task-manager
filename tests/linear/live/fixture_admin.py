"""Typed run-owned administration with independent fresh provider readback."""

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from ai_task_manager.adapters.linear.models import revision
from ai_task_manager.config.files import digest

from .fixture_control import Observation
from .ledger import DriverRefused, Limits, uuid
from .receiver import receiver_route


@dataclass(frozen=True)
class VerifiedEndpoint:
    url: str
    receiver_identity: str
    tunnel_identity: str
    expires_at: float
    run_id: str
    workspace: str
    owner: str
    team: str
    project: str
    webhook_id: str


class RejectEndpoint:
    def verify(self, reference):
        raise DriverRefused("actual_endpoint_admission_required")


def adapter_limits_v1(started_at, deadline):
    """An explicit new-run preset; never update an existing ledger's limits."""
    return Limits(deadline, started_at=started_at, cleanup_requests=48, cleanup_seconds=180)


class FixtureAdministration:
    def __init__(self, graphql, *, endpoint_authority=None, quiescence=None):
        self.graphql, self.ledger = graphql, graphql.ledger
        self.endpoint_authority = endpoint_authority or RejectEndpoint()
        self.quiescence = quiescence

    def _endpoint(self, reference, candidate):
        observed = self.endpoint_authority.verify(reference)
        if not isinstance(observed, VerifiedEndpoint):
            raise DriverRefused("actual_endpoint_admission_required")
        parsed = urlsplit(observed.url)
        s = self.ledger.scope
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != receiver_route(self.ledger.run_id)
            or not observed.receiver_identity
            or not observed.tunnel_identity
            or not self.graphql.clock() < observed.expires_at
            or (
                observed.run_id,
                observed.workspace,
                observed.owner,
                observed.team,
                observed.project,
                observed.webhook_id,
            )
            != (self.ledger.run_id, s.workspace, s.owner, s.team, s.project, candidate)
        ):
            raise DriverRefused("endpoint_admission_mismatch")
        return observed.url

    def _call(self, operation, variables, cleanup_key=None, recovery_key=None, action_key=None):
        return self.graphql.request(
            operation,
            variables,
            cleanup_key=cleanup_key,
            recovery_key=recovery_key,
            action_key=action_key,
        )

    def _metadata(self, kind, candidate, cleanup_key, recovery_key=None, action_key=None):
        after, cursors, teams = None, set(), set()
        first_revision = None
        for _ in range(self.graphql.max_pages):
            variables = {"id": candidate}
            if kind == "project":
                variables["after"] = after
            data = self._call(kind + "_metadata", variables, cleanup_key, recovery_key, action_key)
            self.graphql._identity(data)
            item = data[kind]
            scope = self.ledger.scope
            if item["id"] != candidate or item.get("creator", {}).get("id") != scope.owner:
                raise DriverRefused("fixture_metadata_scope_mismatch")
            stamp = revision(item["updatedAt"])
            if first_revision is not None and stamp != first_revision:
                raise DriverRefused("fixture_metadata_revision_changed")
            first_revision = stamp
            if kind == "project":
                if (
                    candidate != scope.project
                    or item.get("lead", {}).get("id") != scope.owner
                    or item["trashed"] is not False
                ):
                    raise DriverRefused("fixture_metadata_scope_mismatch")
                connection = item["teams"]
                for node in connection["nodes"]:
                    identity = uuid(node["id"])
                    if identity in teams:
                        raise DriverRefused("duplicate_fixture_page_node")
                    teams.add(identity)
                page = connection["pageInfo"]
                if type(page["hasNextPage"]) is not bool:
                    raise DriverRefused("incomplete_fixture_pagination")
                if page["hasNextPage"]:
                    after = page["endCursor"]
                    if not isinstance(after, str) or not after or after in cursors:
                        raise DriverRefused("incomplete_fixture_pagination")
                    cursors.add(after)
                    continue
                if teams != {scope.team}:
                    raise DriverRefused("fixture_metadata_scope_mismatch")
            elif kind == "webhook":
                if (
                    item.get("team", {}).get("id") != scope.team
                    or item["teamIds"] not in (None, [], [scope.team])
                    or item["allPublicTeams"] is not False
                    or item["resourceTypes"] != ["Issue"]
                    or type(item["enabled"]) is not bool
                ):
                    raise DriverRefused("fixture_metadata_scope_mismatch")
            elif kind == "issue":
                if (
                    item.get("team", {}).get("id") != scope.team
                    or item.get("project", {}).get("id") != scope.project
                    or item.get("assignee", {}).get("id") != scope.owner
                ):
                    raise DriverRefused("fixture_metadata_scope_mismatch")
            else:
                raise DriverRefused("unsupported_fixture_kind")
            return item, stamp
        raise DriverRefused("fixture_pagination_limit")

    def _webhook_absent(self, candidate, cleanup_key):
        after, seen_cursors, seen = None, set(), set()
        for _ in range(self.graphql.max_pages):
            data = self._call("webhook_ids", {"after": after}, cleanup_key)
            self.graphql._identity(data)
            connection = data["webhooks"]
            for item in connection["nodes"]:
                identity = uuid(item["id"])
                if identity in seen:
                    raise DriverRefused("duplicate_fixture_page_node")
                seen.add(identity)
            page = connection["pageInfo"]
            if type(page["hasNextPage"]) is not bool:
                raise DriverRefused("incomplete_fixture_pagination")
            if not page["hasNextPage"]:
                return candidate not in seen
            after = page["endCursor"]
            if not isinstance(after, str) or not after or after in seen_cursors:
                raise DriverRefused("incomplete_fixture_pagination")
            seen_cursors.add(after)
        raise DriverRefused("fixture_pagination_limit")

    def read(
        self,
        kind,
        candidate,
        *,
        cleanup_key=None,
        recovery_key=None,
        action_key=None,
        deleted=False,
        expected_url=None,
    ):
        uuid(candidate)
        if kind not in {"project", "webhook", "issue"}:
            raise DriverRefused("unsupported_fixture_kind")
        if candidate == self.ledger.scope.forbidden_project:
            raise DriverRefused("development_project_forbidden")
        if deleted:
            resource = self.ledger.resource(candidate)
            if (
                kind != "webhook"
                or resource["kind"] != kind
                or not self._webhook_absent(candidate, cleanup_key)
            ):
                raise DriverRefused("webhook_absence_unconfirmed")
            return self._observation(kind, candidate, "deleted")
        try:
            item, stamp = self._metadata(kind, candidate, cleanup_key, recovery_key, action_key)
            if kind == "webhook" and expected_url is not None and item["url"] != expected_url:
                raise DriverRefused("fixture_webhook_url_changed")
            marker = self._call(
                kind + "_marker", {"id": candidate}, cleanup_key, recovery_key, action_key
            )[kind]
            if marker["id"] != candidate or revision(marker["updatedAt"]) != stamp:
                raise DriverRefused("fixture_marker_revision_changed")
            expected = self.ledger.run_id
            matched = (
                marker.get("content") == expected
                and marker.get("description") == "Fixture run " + expected
                if kind == "project"
                else marker.get("label") == expected
                if kind == "webhook"
                else ("Fixture run: " + expected) in marker.get("description", "").splitlines()
            )
            if not matched:
                raise DriverRefused("fixture_marker_mismatch")
            if item["archivedAt"] is not None:
                revision(item["archivedAt"])
            state = (
                "archived"
                if item["archivedAt"] is not None
                else "disabled"
                if kind == "webhook" and not item["enabled"]
                else "active"
            )
            return self._observation(kind, candidate, state)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise DriverRefused("fixture_readback_malformed") from error

    def _observation(self, kind, candidate, state):
        s = self.ledger.scope
        return Observation(
            kind, candidate, s.workspace, s.team, s.project, s.owner, self.ledger.run_id, state
        )

    def _create(self, key, kind, candidate, variables, *, endpoint_reference=None):
        row = self.ledger.begin(key, kind, candidate, digest(variables))
        if row["state"] == "confirmed":
            return self.ledger.resource(candidate)
        if row["state"] == "abandoned":
            raise DriverRefused("create_was_abandoned")
        if row["state"] != "intent":
            raise DriverRefused("uncertain_create_needs_readback")
        self.graphql.require_ready()
        if (
            kind == "webhook"
            and self._endpoint(endpoint_reference, candidate) != variables["input"]["url"]
        ):
            raise DriverRefused("endpoint_admission_mismatch")
        self.graphql.actions.remember_create(key, variables)
        self.ledger.charged_create_send(key)
        try:
            data = self.graphql.request(kind + "_create", variables, effect=("create", key))
            result = data[kind + "Create"]
            if result["success"] is not True or result[kind]["id"] != candidate:
                raise DriverRefused("candidate_uuid_not_honored")
            observed = self.read(kind, candidate, expected_url=variables["input"].get("url"))
            if observed.state != ("disabled" if kind == "webhook" else "active"):
                raise DriverRefused("unexpected_created_resource_state")
            self.ledger.confirm(key, candidate, kind)
            return self.ledger.resource(candidate)
        except Exception:
            self.ledger.uncertain(key)
            raise DriverRefused("fixture_create_unconfirmed") from None

    def create_project(self, key):
        s = self.ledger.scope
        variables = {
            "input": {
                "id": s.project,
                "name": "AI task manager fixture",
                "description": "Fixture run " + self.ledger.run_id,
                "content": self.ledger.run_id,
                "teamIds": [s.team],
                "leadId": s.owner,
            }
        }
        return self._create(key, "project", s.project, variables)

    def create_webhook(self, key, candidate, endpoint_reference, secret):
        uuid(candidate)
        self.ledger.resource(self.ledger.scope.project)
        self.graphql.actions.receiver_candidate(candidate)
        url = self._endpoint(endpoint_reference, candidate)
        value = secret()
        if not isinstance(value, str) or len(value) < 32 or any(c in value for c in "\r\n"):
            raise DriverRefused("invalid_fixture_webhook_secret")
        variables = {
            "input": {
                "id": candidate,
                "enabled": False,
                "secret": value,
                "url": url,
                "resourceTypes": ["Issue"],
                "teamId": self.ledger.scope.team,
                "allPublicTeams": False,
                "label": self.ledger.run_id,
            }
        }
        return self._create(
            key, "webhook", candidate, variables, endpoint_reference=endpoint_reference
        )

    def recover_create(self, key):
        row = self.ledger.get(key)
        if row["state"] == "confirmed":
            return self.ledger.resource(row["candidate"])
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("no_possible_effect")
        self.graphql.refresh_if_needed(recovery_key=key)
        context = self.graphql.actions.create_context(key)
        observed = self.read(
            row["kind"], row["candidate"], recovery_key=key, expected_url=context["url"]
        )
        if observed.state != ("disabled" if row["kind"] == "webhook" else "active"):
            raise DriverRefused("unexpected_created_resource_state")
        self.ledger.confirm(key, row["candidate"], row["kind"])
        return self.ledger.resource(row["candidate"])

    def enable_webhook(self, key, candidate, endpoint_reference):
        variables = {"id": uuid(candidate), "input": {"enabled": True}}
        row = self.graphql.actions.begin(key, candidate, "enable_webhook", variables)
        if row["state"] in {"confirmed", "abandoned"}:
            return row
        if row["state"] in {"sent", "uncertain"}:
            return self.recover_enable(key, endpoint_reference)
        expected = self._endpoint(endpoint_reference, candidate)
        self.graphql.actions.remember_action(key, expected)
        self.read("webhook", candidate)
        metadata, _ = self._metadata("webhook", candidate, None)
        if metadata["url"] != expected:
            raise DriverRefused("endpoint_admission_mismatch")
        self.graphql.actions.sent(key)
        try:
            data = self.graphql.request("webhook_update", variables, effect=("ordinary", key))
            if data["webhookUpdate"]["success"] is not True:
                raise DriverRefused("fixture_mutation_rejected")
            return self._settle_enable(key)
        except Exception:
            self.graphql.actions.uncertain(key)
            raise DriverRefused("webhook_enable_unconfirmed") from None

    def recover_enable(self, key, endpoint_reference=None):
        prior = self.graphql.actions.get(key)
        if prior["state"] == "confirmed":
            return prior
        self.graphql.refresh_if_needed(action_key=key)
        return self._settle_enable(key, action_key=key)

    def _settle_enable(self, key, *, action_key=None):
        row = self.graphql.actions.get(key)
        if row["state"] == "confirmed":
            return row
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("ordinary_action_not_sent")
        expected = self.graphql.actions.action_context(key)["url"]
        observed = self.read(
            "webhook", row["resource_id"], action_key=action_key, expected_url=expected
        )
        if observed.state != "active":
            raise DriverRefused("webhook_enable_readback_mismatch")
        return self.graphql.actions.confirm(key, asdict(observed))

    def cleanup(self, key, candidate, action):
        resource = self.ledger.resource(uuid(candidate))
        kind = resource["kind"]
        try:
            previous = self.ledger.cleanup_action(key)
        except DriverRefused as error:
            if str(error) != "cleanup_intent_missing":
                raise
        else:
            previous = self.ledger.begin_cleanup(key, candidate, action)
            if previous["state"] == "confirmed":
                return resource
        self.graphql.actions.require_settled(None if kind == "project" else candidate)
        if (kind, action) not in {
            ("project", "archive"),
            ("issue", "archive"),
            ("webhook", "disable"),
            ("webhook", "delete"),
        }:
            raise DriverRefused("cleanup_action_forbidden")
        if kind == "project":
            if self.quiescence is None:
                raise DriverRefused("actual_runtime_quiescence_required")
            self.quiescence()
            self.ledger.require_closed_children(exclude_cleanup_key=key)
        row = self.ledger.begin_cleanup(key, candidate, action)
        if row["state"] == "confirmed":
            return self.ledger.resource(candidate)
        if row["state"] in {"sent", "uncertain"}:
            return self.recover_cleanup(key)
        self.graphql.refresh_if_needed(cleanup_key=key)
        before = self.read(kind, candidate, cleanup_key=key)
        if action == "delete" and before.state != "disabled":
            raise DriverRefused("owned_disabled_webhook_required")
        self.ledger.cleanup_prepared(key, digest(asdict(before)))
        self.ledger.cleanup_sent(key)
        operation = (
            kind + "_archive"
            if action == "archive"
            else "webhook_update"
            if action == "disable"
            else "webhook_delete"
        )
        variables = {"id": candidate}
        if action == "disable":
            variables["input"] = {"enabled": False}
        try:
            self.graphql.request(operation, variables, effect=("cleanup", key))
            return self.recover_cleanup(key)
        except Exception:
            self.ledger.cleanup_uncertain(key)
            raise DriverRefused("fixture_cleanup_unconfirmed") from None

    def recover_cleanup(self, key):
        row = self.ledger.cleanup_action(key)
        if row["state"] == "confirmed":
            return self.ledger.resource(row["resource_id"])
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("cleanup_no_possible_effect")
        self.graphql.refresh_if_needed(cleanup_key=key)
        observed = self.read(
            row["kind"], row["resource_id"], cleanup_key=key, deleted=row["action"] == "delete"
        )
        if observed.state != row["target"]:
            raise DriverRefused("cleanup_readback_mismatch")
        self.ledger.cleanup_confirm(key, digest(asdict(observed)))
        return self.ledger.resource(row["resource_id"])
