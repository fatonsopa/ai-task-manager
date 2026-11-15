"""Human instructions enter through a private independent operator process, not tools."""

import json
import os
import secrets
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.decisions.authority import HumanInstruction
from ai_task_manager.config.files import digest, strict_json
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope


class OperatorChannel:
    def __init__(self, journal):
        self.journal = journal

    def _exchange(self, request, timeout):
        parent, child = socket.socketpair()
        process = None
        error_path = self.journal.path.parent / ("operator-" + request["request_id"] + ".stderr")
        error_stream = error_path.open("xb")
        os.chmod(error_path, 0o600)
        try:
            parent.settimeout(timeout)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("console.py")),
                    str(child.fileno()),
                ],
                pass_fds=(child.fileno(),),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=error_stream,
                env={"PATH": os.defpath},
            )
            child.close()
            parent.sendall((json.dumps(request) + "\n").encode())
            data = b""
            while b"\n" not in data and len(data) <= 65536:
                part = parent.recv(4096)
                if not part:
                    raise AccessDenied("Independent operator channel closed without confirmation")
                data += part
            reply = strict_json(data)
            if process.wait(timeout=timeout) != 0 or reply.get("operator_pid") != process.pid:
                raise AccessDenied("Independent operator process did not confirm its own response")
            return reply, process.pid
        finally:
            error_stream.close()
            parent.close()
            child.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)

    def capture_instruction(
        self,
        *,
        scope,
        task_id,
        target,
        revision,
        reason,
        task_version,
        prose_digest,
        proposed_prose_digest=None,
        timeout=60,
        decision_request_id=None,
        decision_option_id=None,
        decision_digest=None,
        decision_actor_id=None,
    ):
        if not isinstance(scope, Scope) or not reason.strip() or type(revision) is not int:
            raise AccessDenied("Exact scoped human instruction is required")
        payload = {
            "scope": asdict(scope),
            "task_id": task_id,
            "target": target,
            "revision": revision,
            "reason": reason,
            "task_version": task_version,
            "prose_digest": prose_digest,
            "proposed_prose_digest": proposed_prose_digest,
        }
        if decision_request_id is not None:
            payload.update(
                decision_request_id=decision_request_id,
                decision_option_id=decision_option_id,
                decision_digest=decision_digest,
                decision_actor_id=decision_actor_id,
            )
        return self._capture("human", payload, timeout)

    def capture_enrollment(self, *, scope, actions, startup_ref, timeout=60):
        source = self.journal._read(startup_ref, "startup")
        if source["payload"]["scope"] != asdict(scope):
            raise AccessDenied("Enrollment scope differs from the actual authenticated native host")
        payload = {
            "scope": asdict(scope),
            "logical_session": scope.logical_session,
            "actions": sorted(set(actions)),
            "startup_ref": startup_ref,
        }
        return self._capture("enrollment", payload, timeout, sources=(startup_ref,))

    def _capture(self, kind, payload, timeout, *, sources=()):
        request = {
            "request_id": str(uuid4()),
            "challenge": secrets.token_urlsafe(24),
            "instruction": payload,
        }
        # Only this installer-owned private descriptor reaches the independent console.
        # isatty is supplementary: channel ownership, exact one-use challenge, process
        # identity and human-typed instruction approval jointly establish this source.
        reply, pid = self._exchange(request, timeout)
        if (
            reply.get("request_id") != request["request_id"]
            or reply.get("challenge") != request["challenge"]
            or reply.get("accepted") is not True
            or reply.get("instruction_hash") != digest(payload)
        ):
            raise AccessDenied("Operator did not approve this exact instruction")
        return self.journal._append(
            kind,
            {**payload, "source_hash": digest(reply), "operator_pid": pid},
            sources=sources,
            expires_at=self.journal.clock() + 300,
        )


class HumanReceipts:
    def __init__(self, journal):
        self.journal = journal

    def verify_instruction(self, ref, *, fresh=True):
        data = self.journal._read(ref, "human", fresh=fresh)["payload"]
        return HumanInstruction(
            Scope(**data["scope"]),
            data["task_id"],
            data["target"],
            data["revision"],
            data["reason"],
            data["source_hash"],
            data["task_version"],
            data["prose_digest"],
            data["proposed_prose_digest"],
            data.get("decision_request_id"),
            data.get("decision_option_id"),
            data.get("decision_digest"),
            data.get("decision_actor_id"),
        )
