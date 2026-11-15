import hashlib
import hmac
import json
import re
import sqlite3

from ai_task_manager.domains.work.errors import WorkError

from .errors import LinearError
from .models import HttpResponse, canonical_id, revision


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON")
        result[key] = value
    return result


def ownership_unverified(payload):
    """Retain only safe facts until fresh scoped readback supplies missing ownership."""
    identities = {"id", "teamId", "projectId", "assigneeId", "delegateId", "stateId"}
    data = {
        key: value
        for key, value in payload["data"].items()
        if key in identities or key == "updatedAt"
    }
    for key in identities & data.keys():
        if data[key] is not None:
            canonical_id(data[key])
    result = {
        key: payload[key]
        for key in ("action", "type", "organizationId", "webhookId", "webhookTimestamp")
    }
    result["data"] = data
    actor = payload.get("actor") or {}
    if actor.get("id"):
        result["actor"] = {
            "id": canonical_id(actor["id"]),
            "type": "user" if actor.get("type") == "user" else "integration",
        }
    if type(payload.get("updatedFrom")) is dict:
        delta = {}
        for key, value in payload["updatedFrom"].items():
            if key in identities:
                delta[key] = canonical_id(value) if value is not None else None
            elif key == "updatedAt":
                delta[key] = revision(value) if value is not None else None
            elif key == "labelIds" and type(value) is list:
                delta[key] = [canonical_id(item) for item in value]
            else:
                # Keep the fact of another change, never its possibly foreign text.
                delta["otherFieldsChanged"] = True
        result["updatedFrom"] = delta
    return result


class WebhookReceiver:
    def __init__(
        self,
        core,
        handle,
        journal,
        binding,
        webhook_id,
        secret,
        *,
        clock,
        wake=lambda: None,
        max_body_bytes=1048576,
        freshness_seconds=60,
    ):
        if not 0 < max_body_bytes <= 4194304 or not 0 < freshness_seconds <= 60:
            raise LinearError("invalid_receiver_limits")
        self.core, self.handle, self.journal = core, handle, journal
        self.binding, self.webhook_id, self.secret = binding, canonical_id(webhook_id), secret
        self.clock, self.wake = clock, wake
        self.maximum, self.freshness = max_body_bytes, freshness_seconds
        _, self.scope = core.session_binding(handle)
        expected = binding.expected
        if (
            self.scope.provider != "linear"
            or self.scope.workspace != expected.workspace_id
            or self.scope.principal != expected.viewer_id
            or self.scope.project != expected.project_id
        ):
            raise LinearError("scope_mismatch")

    def receive(self, method, headers, raw_body):
        if method != "POST":
            return HttpResponse(405)
        if not isinstance(raw_body, bytes) or len(raw_body) > self.maximum:
            return HttpResponse(413)
        normalized = {}
        for key, value in headers:
            key = key.lower()
            if key in normalized or not isinstance(value, str):
                return HttpResponse(400)
            normalized[key] = value
        if normalized.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return HttpResponse(415)
        signature = normalized.get("linear-signature", "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
            return HttpResponse(401)
        expected = hmac.new(self.secret(), raw_body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(signature)):
            return HttpResponse(401)
        try:
            payload = json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            stamp = payload["webhookTimestamp"]
            if type(stamp) is not int or abs(self.clock() * 1000 - stamp) > self.freshness * 1000:
                return HttpResponse(401)
            if "linear-timestamp" in normalized and normalized["linear-timestamp"] != str(stamp):
                return HttpResponse(400)
            delivery_id = canonical_id(normalized.get("linear-delivery"))
            if (
                payload["organizationId"] != self.scope.workspace
                or payload["webhookId"] != self.webhook_id
                or payload["type"] != "Issue"
                or normalized.get("linear-event") != "Issue"
                or payload["action"] not in {"create", "update", "remove"}
            ):
                return HttpResponse(403)
            event_payload = payload
            data = payload["data"]
            issue_id = canonical_id(data["id"])
            canonical_id(data["teamId"])
            revision(data["updatedAt"])
            registered = {
                task.issue_id for task in self.core.list_tasks(self.handle) if task.issue_id
            }
            changed_team = data.get("teamId") != self.binding.expected.team_id
            changed_project = data.get("projectId") != self.scope.project
            changed_assignee = "assigneeId" in data and data["assigneeId"] != self.scope.principal
            connection = self.binding.connection
            changed_delegate = (
                connection is not None
                and "delegateId" in data
                and data["delegateId"] != connection.agent_id
            )
            if issue_id not in registered:
                self.journal.ignore_delivery(
                    self.scope, delivery_id, raw_body, "unregistered_issue", self.clock()
                )
                return HttpResponse(200)
            if "delegateId" in data and connection is None:
                return HttpResponse(403)  # No installed selection witness for this claim.
            if changed_team or changed_project or changed_assignee or changed_delegate:
                scope_fields = {"team": data.get("teamId"), "project": data.get("projectId")}
                if changed_assignee or changed_delegate:
                    scope_fields.update(
                        assignee=data.get("assigneeId"), delegate=data.get("delegateId")
                    )
                payload = {
                    "action": payload["action"],
                    "type": "Issue",
                    "organizationId": payload["organizationId"],
                    "webhookId": payload["webhookId"],
                    "webhookTimestamp": stamp,
                    "data": {
                        "id": issue_id,
                        "updatedAt": data["updatedAt"],
                        "scopeRemoval": True,
                        "teamChanged": changed_team,
                        "projectChanged": changed_project,
                        "scopeDigest": hashlib.sha256(
                            json.dumps(
                                scope_fields,
                                sort_keys=True,
                            ).encode()
                        ).hexdigest(),
                    },
                }
                if changed_assignee or changed_delegate:
                    payload["data"].update(
                        assigneeChanged=changed_assignee, delegateChanged=changed_delegate
                    )
            elif "assigneeId" not in data or "delegateId" not in data:
                payload = ownership_unverified(payload)
        except WorkError:
            return HttpResponse(403)
        except sqlite3.DatabaseError:
            return HttpResponse(503)
        except (ValueError, KeyError, TypeError, UnicodeError, LinearError):
            return HttpResponse(400)
        try:
            receipt, _ = self.journal.accept_delivery(
                self.scope, delivery_id, raw_body, payload, event_payload=event_payload
            )
            stored_payload = json.loads(receipt["payload"])
            self.core.sync.ingest(
                self.handle, receipt["delivery_id"], stored_payload, receipt["ingress_ref"]
            )
        except LinearError as exc:
            return HttpResponse(409 if exc.category == "delivery_payload_conflict" else 503)
        except sqlite3.DatabaseError:
            return HttpResponse(503)
        except WorkError:
            return HttpResponse(503)
        # A failed wake is recoverable from the durable queue, not a lost delivery.
        try:
            self.wake()
        except (OSError, RuntimeError) as exc:
            self.journal.record_wake_failure(receipt["id"], type(exc).__name__)
        return HttpResponse(200)
