"""Runtime receipts are issued from observed facts, never caller-supplied Evidence."""

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.hosts.startup import HostStartup, RejectHostStartup
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence, Scope
from ai_task_manager.supervisor.journal import canonical, digest
from ai_task_manager.supervisor.models import RuntimeRefused

RUNTIME_KINDS = frozenset({"worker_start", "resources_ready", "attempt_outcome"})


class RuntimeProofs:
    prefix = "runtime-proof:"

    def __init__(self, journal, key_path: Path, *, fixture_hosts=(), host_startups=None):
        self.journal = journal
        self.fixture_hosts = frozenset(fixture_hosts)
        if any(
            not isinstance(name, str) or not name or name.casefold().startswith(("claude", "codex"))
            for name in self.fixture_hosts
        ):
            raise RuntimeRefused("Model hosts cannot be configured as fixture harnesses")
        self.host_startups = host_startups or RejectHostStartup()
        self._key_path = Path(key_path)
        self._key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
        info = self._key_path.lstat()
        if self._key_path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeRefused("Proof signing material must be private and owned")
        self._key = self._key_path.read_bytes()
        if len(self._key) != 32:
            raise RuntimeRefused("Invalid runtime signing key")

    def startup(self, scope, attempt_id, observation_id, *, host_ready_ref=None):
        fact = self.journal.observation(observation_id, attempt_id=attempt_id, kind="harness_ready")
        if not fact.get("pid") or not fact.get("start_identity") or not fact.get("nonce_matched"):
            raise EvidenceRequired("Actual waiting-harness identity must be observed")
        owner = self.journal.attempt(attempt_id)
        if owner["host"] in self.fixture_hosts:
            return self._from_observation(
                "worker_start", scope, attempt_id, fact, {"startup_origin": "fixture_harness"}
            )
        observed = self.host_startups.verify_host_ready(host_ready_ref)
        if (
            not isinstance(observed, HostStartup)
            or observed.scope != scope
            or observed.attempt_id != attempt_id
            or observed.host != owner["host"]
            or observed.worker_id != owner["worker_id"]
            or observed.process_id == fact["pid"]
            or observed.process_id <= 0
            or not all(
                (
                    observed.process_start_identity,
                    observed.session_id,
                    observed.principal_ref,
                    observed.configuration_digest,
                    observed.observation_ref,
                )
            )
            or observed.expires_at <= self.journal.clock()
        ):
            raise EvidenceRequired("Host startup does not match the actual execution owner")
        identity = self.journal.observe(
            attempt_id,
            "model_worker_ready",
            {
                "host_observation": asdict(observed),
                "host_source_expires_at": observed.expires_at,
                "harness_observation": observation_id,
                "worker_id": owner["worker_id"],
                "model_execution": "not_established",
            },
        )
        return self._from_observation(
            "worker_start",
            scope,
            attempt_id,
            self.journal.observation(identity),
            {"startup_origin": "authenticated_host", "host_session_id": observed.session_id},
        )

    def resources(self, scope, attempt_id, observation_id):
        fact = self.journal.observation(
            observation_id, attempt_id=attempt_id, kind="resources_ready"
        )
        if not fact.get("worktree_id") or not fact.get("database_id"):
            raise EvidenceRequired("Both actual owned resources must be observed")
        return self._from_observation(
            "resources_ready",
            scope,
            attempt_id,
            fact,
            {"ready": True, "worktree_id": fact["worktree_id"], "database_id": fact["database_id"]},
        )

    def outcome(self, scope, attempt_id, observation_id):
        fact = self.journal.observation(
            observation_id, attempt_id=attempt_id, kind="runtime_outcome"
        )
        if fact.get("state") not in {"done", "failed", "paused", "canceled", "unknown"}:
            raise EvidenceRequired("Actual runtime outcome is required")
        if fact["state"] != "unknown" and fact.get("quiescent") is not True:
            raise EvidenceRequired("Unconfirmed stop cannot release capacity")
        return self._from_observation(
            "attempt_outcome",
            scope,
            attempt_id,
            fact,
            {"state": fact["state"], "quiescent": fact["quiescent"]},
        )

    def _from_observation(self, kind, scope, attempt_id, fact, extra):
        owner = self.journal.attempt(attempt_id)
        if asdict(scope) != owner["scope"]:
            raise EvidenceRequired("Runtime scope does not match the registered attempt")
        ref = self.prefix + str(uuid4())
        data = {
            "issuer": "runtime-v1",
            "kind": kind,
            "scope": asdict(scope),
            "subject": attempt_id,
            "source": fact["id"],
            "source_digest": digest(fact),
            "expires_at": min(
                owner["deadline"], self.journal.clock() + 60,
                fact.get("host_source_expires_at", owner["deadline"]),
            ),
            "payload": {
                "task_id": owner["task_id"],
                "host": owner["host"],
                "worker_id": fact.get("worker_id", owner["worker_id"]),
                **extra,
            },
        }
        # Completed observations must remain usable to settle after the work deadline.
        if kind == "attempt_outcome":
            data["expires_at"] = self.journal.clock() + 60
        signature = hmac.new(self._key, canonical(data).encode(), hashlib.sha256).hexdigest()
        with self.journal.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_receipts VALUES(?,?,?)", (ref, canonical(data), signature)
            )
        return ref

    def verify_evidence(self, proof_ref):
        if not isinstance(proof_ref, str) or not proof_ref.startswith(self.prefix):
            raise EvidenceRequired("Unknown runtime reference")
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT data,signature FROM runtime_receipts WHERE ref=?", (proof_ref,)
            ).fetchone()
        if row is None:
            raise EvidenceRequired("Runtime receipt is missing")
        try:
            data = json.loads(row[0])
            expected = hmac.new(self._key, canonical(data).encode(), hashlib.sha256).hexdigest()
            if (
                not hmac.compare_digest(expected, row[1])
                or data["issuer"] != "runtime-v1"
                or data["kind"] not in RUNTIME_KINDS
                or data["expires_at"] <= self.journal.clock()
                or (
                    data["kind"] == "worker_start"
                    and (
                        data["payload"].get("startup_origin")
                        not in {"fixture_harness", "authenticated_host"}
                        or (
                            data["payload"].get("startup_origin") == "fixture_harness"
                            and data["payload"].get("host") not in self.fixture_hosts
                        )
                    )
                )
            ):
                raise EvidenceRequired("Runtime receipt integrity or freshness failed")
            fact = self.journal.observation(data["source"], attempt_id=data["subject"])
            if digest(fact) != data["source_digest"]:
                raise EvidenceRequired("Runtime receipt source changed")
            return Evidence(
                data["kind"],
                Scope(**data["scope"]),
                data["subject"],
                data["payload"],
                data["expires_at"],
            )
        except (KeyError, TypeError, ValueError, RuntimeRefused) as error:
            raise EvidenceRequired("Runtime receipt is invalid") from error
