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
            data = payload["data"]
            issue_id = canonical_id(data["id"])
            canonical_id(data["teamId"])
            revision(data["updatedAt"])
            registered = {
                task.issue_id for task in self.core.list_tasks(self.handle) if task.issue_id
            }
            changed_team = data.get("teamId") != self.binding.expected.team_id
            changed_project = data.get("projectId") != self.scope.project
            if issue_id not in registered:
                self.journal.ignore_delivery(
                    self.scope, delivery_id, raw_body, "unregistered_issue", self.clock()
                )
                return HttpResponse(200)
            if changed_team or changed_project:
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
                                {"team": data.get("teamId"), "project": data.get("projectId")},
                                sort_keys=True,
                            ).encode()
                        ).hexdigest(),
                    },
                }
        except WorkError:
            return HttpResponse(403)
        except sqlite3.DatabaseError:
            return HttpResponse(503)
        except (ValueError, KeyError, TypeError, UnicodeError, LinearError):
            return HttpResponse(400)
        try:
            receipt, _ = self.journal.accept_delivery(self.scope, delivery_id, raw_body, payload)
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
