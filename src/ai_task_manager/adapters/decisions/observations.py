"""Consume operator-enrolled delegation; stored signatures do not imply human input."""

import hashlib
import hmac
import json
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.config.files import digest, private_path, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import Scope

from .authority import HostGrant
from .journal import canonical

PREFIX = "session-enrollment:"


def identity(path, *, content=False):
    path = private_path(path)
    info = path.stat()
    if path.resolve() != path or info.st_nlink != 1:
        raise AccessDenied("Session observation sources must use original physical files")
    value = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    if content:
        value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def intent(config):
    return digest(
        {k: v for k, v in config.items() if k not in {"grant_ref", "session_observations"}}
    )


def bind_record(journal, reference, value):
    """Trusted setup uses stable keys; retries never replace an original record."""
    raw = canonical({**value, "ref": reference})
    mac = hmac.new(journal._key, raw.encode(), hashlib.sha256).hexdigest()
    with journal.transaction() as conn:
        old = conn.execute("SELECT data,mac FROM records WHERE ref=?", (reference,)).fetchone()
        if old is not None:
            if not hmac.compare_digest(
                old[1], hmac.new(journal._key, old[0].encode(), hashlib.sha256).hexdigest()
            ):
                raise AccessDenied("Session observation record changed")
            if old[0] != raw:
                raise IdempotencyConflict("Session setup key has different immutable arguments")
            return False
        conn.execute("INSERT INTO records VALUES(?,?,?)", (reference, raw, mac))
    return True


class SessionObservations:
    def __init__(self, journal, client, config_file, pointer):
        if type(pointer) is not dict or set(pointer) != {"reference", "journal", "key"}:
            raise AccessDenied("Exact installed session observation binding required")
        self.journal, self.client = journal, client
        self.config_file = Path(config_file).absolute()
        self.pointer = json.loads(json.dumps(pointer))
        self._bound = (journal, client, self.config_file, digest(self.pointer))
        self._check()

    def _check(self):
        if (
            any(
                a is not b
                for a, b in zip((self.journal, self.client, self.config_file), self._bound[:3])
            )
            or digest(self.pointer) != self._bound[3]
        ):
            raise AccessDenied("Installed session observation owner changed")
        if (identity(self.journal.path), identity(self.journal.key_path, content=True)) != (
            self.pointer["journal"],
            self.pointer["key"],
        ):
            raise AccessDenied("Original session observation storage changed")

    def verify_enrollment(self, reference):
        grant = self._pending_enrollment(reference)
        completed = self.journal._get(
            "session-setup-result:" + digest(reference), "session-setup-result:"
        )
        destination = Path(read_private(self.config_file)["session_file"]).absolute()
        if (
            completed["scope"] != asdict(grant.scope)
            or completed["session_file"]["path"] != str(destination)
            or read_private(destination).get("scope") != asdict(grant.scope)
        ):
            raise AccessDenied("Completed session setup belongs to another capability")
        # Core checks the current capability. A legitimate resume may rotate its
        # token/file; the original publication witness stays immutable history.
        return grant

    def _pending_enrollment(self, reference):
        self._check()
        if reference != self.pointer["reference"]:
            raise AccessDenied("Session enrollment belongs to another installation")
        data = self.journal._get(reference, PREFIX)
        revocation = "session-revoked:" + digest(reference)
        with self.journal.transaction() as conn:
            revoked = conn.execute("SELECT 1 FROM records WHERE ref=?", (revocation,)).fetchone()
        if revoked:
            self.journal._get(revocation, "session-revoked:")
            raise AccessDenied("Session enrollment was revoked")
        config = read_private(self.config_file)
        if data["config_path"] != str(self.config_file) or data["config_intent"] != intent(config):
            raise AccessDenied("Session enrollment configuration or source changed")
        binding = self.client.verify_binding()
        if asdict(binding.expected) != data["provider"]:
            raise AccessDenied("Current provider account or project differs from enrollment")
        scope = Scope(**data["scope"])
        if (scope.provider, scope.workspace, scope.principal, scope.project) != (
            "linear",
            binding.expected.workspace_id,
            binding.expected.viewer_id,
            binding.expected.project_id,
        ):
            raise AccessDenied("Session observation scope changed")
        return HostGrant(
            scope, scope.logical_session, tuple(data["actions"]), reference, data["expires_at"]
        )

    def verify_instruction(self, reference):
        raise EvidenceRequired("Actual human instruction observation is unavailable")

    def verify_host_observation(self, reference):
        raise EvidenceRequired("Actual host transcript observation is unavailable")

    def revoke(self):
        self._check()
        bind_record(
            self.journal,
            "session-revoked:" + digest(self.pointer["reference"]),
            {"enrollment": self.pointer["reference"]},
        )
