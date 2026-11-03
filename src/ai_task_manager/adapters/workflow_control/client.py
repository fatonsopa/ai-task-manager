"""No client starts, replaces or reconstructs a missing execution owner."""

import hmac
import secrets
import socket

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .identity import MAX_FRAME, OwnerBinding, canonical, receive, signature


class WorkflowClient:
    def __init__(self, config_file, scope):
        self.binding = OwnerBinding(config_file, scope)

    def _call(self, operation, data):
        row = self.binding.check()
        body = {
            "nonce": secrets.token_hex(32),
            "operation": operation,
            "data": data,
            "scope": self.binding.scope,
            "instance": row["instance"],
        }
        request = dict(body, mac=signature(row["key"], body))
        raw = canonical(request) + b"\n"
        if len(raw) > MAX_FRAME:
            raise AccessDenied("Local request exceeds its bound")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(120)
                stream.connect(row["endpoint"]["path"])
                self.binding.check()
                stream.sendall(raw)
                response = receive(stream)
        except (AccessDenied, OSError, TimeoutError) as exc:
            raise RuntimeUncertain(
                "Local owner reply is unavailable; resume recorded work"
            ) from exc
        if type(response) is not dict or set(response) != {"nonce", "envelope", "mac"}:
            raise AccessDenied("Invalid local owner response")
        signed = {k: v for k, v in response.items() if k != "mac"}
        if (
            response["nonce"] != body["nonce"]
            or type(response["mac"]) is not str
            or not hmac.compare_digest(response["mac"], signature(row["key"], signed))
        ):
            raise AccessDenied("Local owner response authentication failed")
        self.binding.check()
        return response["envelope"]

    def forward(self, operation, data):
        return self._call(operation, data)

    def run(self, task_id, plan_ref, operation_key):
        return self._call(
            "workflow_run", dict(task_id=task_id, plan_ref=plan_ref, operation_key=operation_key)
        )

    def resume(self, workflow_ref):
        return self._call("workflow_resume", dict(workflow_ref=workflow_ref))
