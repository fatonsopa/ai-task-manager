"""Typed run-owned administration with independent fresh provider readback."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from ai_task_manager.adapters.linear.models import revision
from ai_task_manager.config.files import digest

from .fixture_control import Observation
from .installed_endpoint import installed_route
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
    receiver_kind: str = "fixture"


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
        routes = {"fixture": receiver_route, "installed_cli": installed_route}
        if observed.receiver_kind not in routes:
            raise DriverRefused("endpoint_receiver_kind_unknown")
        parsed = urlsplit(observed.url)
        s = self.ledger.scope
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != routes[observed.receiver_kind](self.ledger.run_id)
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
                    or (item["trashed"] is not False and item["trashed"] is not None)
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

    def _product_issue(self, config_file, submit_key, task_id, writing_ref):
        """Read actual installed product intent and approval; do not initialize its stores."""
        from ai_task_manager.adapters.decisions.observations import identity
        from ai_task_manager.adapters.linear.connections import AgentConnection
        from ai_task_manager.adapters.workflow_control.identity import source_digest
        from ai_task_manager.adapters.writing.journal import ApprovalView
        from ai_task_manager.adapters.writing.package import WriterPackage
        from ai_task_manager.adapters.writing.service import WritingService
        from ai_task_manager.application.intake import submit_payload
        from ai_task_manager.config.claim_store import file_identity
        from ai_task_manager.config.files import FileSeal, read_private
        from ai_task_manager.domains.work.models import SessionHandle, TaskProse
        from ai_task_manager.installation.sessions.operator import (
            ReadOnlyAuthority,
            current_authority,
            readonly,
        )
        from ai_task_manager.installation.sessions.recovery import (
            installed_authority,
            installed_provider_history,
        )
        from ai_task_manager.storage import sessions, work
        from ai_task_manager.storage.database import Database
        from ai_task_manager.storage.publication_schema import validate as publication
        from ai_task_manager.storage.scheduling_schema import validate as scheduling

        uuid(task_id)
        seal = FileSeal(config_file)
        config = read_private(config_file)
        root = Path(config_file).parent
        setup = read_private(root / "operator-intent.json")
        complete = read_private(root / "operator-result.json")
        if setup["source"] != source_digest() or complete["config"] != identity(
            config_file, content=True
        ):
            raise DriverRefused("product_installation_changed")
        installed_authority(config)
        installed_provider_history(config)
        journal = ReadOnlyAuthority(config["authority_database"], config["authority_key"])
        enrollment = journal._get(config["grant_ref"], "session-enrollment:")
        capability = read_private(config["session_file"])
        with readonly(config["core_database"]) as conn:
            scheduling(conn)
            publication(conn)
            if [tuple(row) for row in conn.execute("SELECT version FROM metadata")] != [(5,)]:
                raise DriverRefused("current_product_schema_required")
            claim = journal.claim_store()
            if (
                Database.identity(conn) != claim["store_id"]
                or file_identity(config["core_database"]) != claim["file_identity"]
            ):
                raise DriverRefused("product_claim_store_changed")
            sid, scope = sessions.resolve(conn, SessionHandle(capability["capability"]))
            s = self.ledger.scope
            if (
                (scope.provider, scope.workspace, scope.principal, scope.project)
                != ("linear", s.workspace, s.owner, s.project)
                or capability["scope"] != asdict(scope)
                or config["linear"]["expected"]["team_id"] != s.team
            ):
                raise DriverRefused("product_scope_mismatch")
            task = work.get(conn, sid, task_id)
            if (
                task.approval_ref != writing_ref
                or task.sync_state != "confirmed"
                or task.observed_status != "Backlog"
                or task.desired_status != "Backlog"
                or task.queued_once
                or task.execution_state != "idle"
                or not task.issue_id
            ):
                raise DriverRefused("confirmed_product_backlog_required")
            uuid(task.issue_id)
            indexed = conn.execute(
                "SELECT issue_id,version FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            binding = conn.execute(
                "SELECT task_id FROM issue_bindings WHERE identity_key=?",
                (
                    digest(
                        {
                            "provider": scope.provider,
                            "workspace": scope.workspace,
                            "issue_id": task.issue_id,
                        }
                    ),
                ),
            ).fetchone()
            if (
                tuple(indexed) != (task.issue_id, task.version)
                or not binding
                or binding[0] != task.id
            ):
                raise DriverRefused("product_task_identity_changed")
            values = config["writer"]
            # Only verifier methods are used; WritingService.__init__ creates local state.
            writer = object.__new__(WritingService)
            writer.package = WriterPackage(Path(values["root"]), values["python"], values["pins"])
            writer.journal = ReadOnlyAuthority(values["journal"], values["key"])
            writer.root = Path(values["sessions_root"])
            writer.reviewer_identity, writer.reviewer_model = "Current host self-review", None
            approved = ApprovalView(writer, "task", scope).record(writing_ref)
            if (
                approved["digest"] != task.prose_digest
                or approved["representation"] != task.representation
                or approved["policy"] != config["approval_policy"]
                or approved["prose"]
                != {
                    "title": task.title,
                    "body": task.body,
                    "code_bearing": task.code_bearing,
                    "required_work": list(task.required_work),
                }
                or "Fixture run: " + self.ledger.run_id not in task.body.splitlines()
            ):
                raise DriverRefused("current_product_approval_required")
            prose = TaskProse(task.title, task.body, task.code_bearing, task.required_work)
            prior = work.replay(
                conn, sid, submit_key, submit_payload(prose, writing_ref, None, None)
            )
            if (
                prior is None
                or prior["id"] != task.id
                or prior["issue_id"] is not None
                or prior["desired_status"] != "Backlog"
            ):
                raise DriverRefused("original_product_submit_required")
            rows = conn.execute(
                "SELECT * FROM outbox WHERE session_id=? AND task_id=? AND kind='create_backlog'",
                (sid, task.id),
            ).fetchall()
            if len(rows) != 1 or rows[0]["state"] != "confirmed":
                raise DriverRefused("confirmed_product_intent_required")
            operation = dict(rows[0])
            payload = json.loads(operation["payload"])
            if (
                payload["title"] != task.title
                or payload["body"] != task.body
                or payload["prose_digest"] != task.prose_digest
                or payload["desired_status"] != "Backlog"
                or payload["issue_id"] is not None
                or payload["task_version"] != prior["version"]
                or payload["decision_revision"] != prior["decision_revision"]
            ):
                raise DriverRefused("product_intent_changed")
            event = conn.execute(
                "SELECT kind FROM events WHERE id=? AND task_id=?", (payload["event_id"], task.id)
            ).fetchone()
            if not event or event[0] != "task_submitted":
                raise DriverRefused("original_product_intent_required")
            if conn.execute("SELECT 1 FROM attempts WHERE session_id=? LIMIT 1", (sid,)).fetchone():
                raise DriverRefused("product_execution_not_allowed")
        with readonly(config["linear"]["journal"]) as conn:
            send = conn.execute(
                "SELECT * FROM sends WHERE operation_id=? ORDER BY attempt DESC LIMIT 1",
                (operation["id"],),
            ).fetchone()
            if (
                not send
                or send["state"] != "confirmed"
                or send["candidate_id"] is not None
                or send["attempt"] != operation["attempt_count"]
                or json.loads(send["scope"]) != asdict(scope)
            ):
                raise DriverRefused("confirmed_server_assigned_product_send_required")
            snapshot = json.loads(send["snapshot"])
            selected = AgentConnection(**config["linear"]["agent_connection"])
            if send["connection_digest"] != selected.digest or (
                snapshot["id"],
                snapshot["title"],
                snapshot["body"],
                snapshot["status"],
                snapshot["team_id"],
                snapshot["project_id"],
                snapshot["assignee_id"],
                snapshot["delegate_id"],
            ) != (
                task.issue_id,
                task.title,
                task.body,
                "Backlog",
                s.team,
                s.project,
                s.owner,
                selected.agent_id,
            ):
                raise DriverRefused("product_confirmation_changed")
            send_id = send["id"]
        current_authority(
            config_file,
            config,
            journal,
            config["grant_ref"],
            asdict(scope),
            enrollment["expires_at"],
        )
        seal.check()
        provenance = {
            "config": identity(config_file, content=True),
            "source": source_digest(),
            "scope": asdict(scope),
            "task_id": task.id,
            "task_version": task.version,
            "session_id": sid,
            "claim_store": claim,
            "issue_id": task.issue_id,
            "submit_key": submit_key,
            "writing_ref": writing_ref,
            "outbox_id": operation["id"],
            "request_fingerprint": digest(submit_payload(prose, writing_ref, None, None)),
            "send_id": send_id,
            "prose_digest": task.prose_digest,
            "approval_output_sha256": approved["handoff"]["output_sha256"],
            "state_id": snapshot["state_id"],
            "title_sha256": digest(task.title),
            "body_sha256": digest(task.body),
        }
        return SimpleNamespace(
            provenance=provenance,
            title=task.title,
            body=task.body,
            issue_id=task.issue_id,
            state_id=snapshot["state_id"],
        )

    def observe_product_issue(self, config_file, submit_key, task_id, writing_ref):
        """Bind confirmed default-CLI creation to cleanup using truthful post-effect provenance."""
        try:
            before = self._product_issue(config_file, submit_key, task_id, writing_ref)
            self.ledger.resource(self.ledger.scope.project)
            self.graphql.refresh_if_needed()
            item, stamp = self._metadata("issue", before.issue_id, None)
            marker = self._call("issue_marker", {"id": before.issue_id})["issue"]
            if (
                marker["id"] != before.issue_id
                or revision(marker["updatedAt"]) != stamp
                or marker["title"] != before.title
                or marker["description"] != before.body
                or item["state"]["id"] != before.state_id
                or item["archivedAt"] is not None
            ):
                raise DriverRefused("product_fresh_readback_changed")
            # Re-observe after I/O; local intent, approval or authority may have changed.
            after = self._product_issue(config_file, submit_key, task_id, writing_ref)
            if before.provenance != after.provenance:
                raise DriverRefused("product_changed_during_readback")
            provenance = {**after.provenance, "provider_revision": stamp}
            return self.ledger.observe_product_issue(
                "observed-product:" + submit_key, after.issue_id, provenance
            )
        except DriverRefused:
            raise
        except Exception:
            raise DriverRefused("confirmed_product_observation_refused") from None

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
