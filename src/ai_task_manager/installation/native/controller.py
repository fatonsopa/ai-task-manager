"""Finite invocation lifecycle for installed hosts, using existing capability consumers."""

import math
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied


def require(value, message):
    if not value:
        raise AccessDenied(message)


class NormalInvocationController:
    def __init__(self, store, permission, *, clock=None):
        self.store, self.permission = store, permission
        self.clock = clock or (lambda: datetime.now(UTC))
        self.reference = permission["reference"]
        self._bound = (store, permission, self.clock)
        self._permission_digest = digest(permission)

    def _current(self, conn, *, admission=True):
        require(
            all(a is b for a, b in zip((self.store, self.permission, self.clock), self._bound)),
            "Normal invocation owner changed",
        )
        current = self.store.read(conn, "native_permissions", self.reference)
        require(
            current is not None
            and digest(self.permission) == self._permission_digest
            and digest({**current, "revoked": self.permission["revoked"]})
            == self._permission_digest,
            "Normal permission changed or was revoked",
        )
        if admission:
            require(
                not current["revoked"] and self.clock().timestamp() < current["deadline"],
                "Normal invocation permission expired or was revoked",
            )
        return current

    def _calls(self, conn):
        import json

        return [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM native_calls WHERE permission_ref=? ORDER BY rowid",
                (self.reference,),
            )
        ]

    def snapshot(self):
        with self.store.transaction() as conn:
            self._current(conn, admission=False)
            return {
                "run_id": self.store.binding.store_id,
                "policy": {"revision": 1},
                "calls": self._calls(conn),
            }

    def reserve(
        self,
        call_id,
        *,
        label,
        paid,
        max_cost_usd,
        uses_worker,
        batch_id,
        timeout_seconds,
        metadata,
    ):
        with self.store.transaction() as conn:
            p = self._current(conn)
            require(
                type(metadata) is dict
                and set(metadata) == {"parent_call_id", "billing_basis_ref", "invocation_binding"},
                "Exact invocation metadata required",
            )
            scope = metadata.get("invocation_binding", {})
            purpose = scope.get("purpose")
            limit = p["limits"].get(purpose)
            require(
                isinstance(call_id, str)
                and call_id
                and len(call_id) <= 256
                and isinstance(label, str)
                and label
                and paid is False
                and max_cost_usd is None
                and type(uses_worker) is bool
                and uses_worker == (purpose == "host")
                and batch_id == self.reference
                and type(timeout_seconds) in {int, float}
                and math.isfinite(timeout_seconds)
                and limit is not None
                and 0 < timeout_seconds <= limit
                and set(scope)
                == {"attempt_id", "host", "configuration_digest", "model", "effort", "purpose"}
                and all(isinstance(v, str) and v for v in scope.values())
                and (scope["host"], scope["model"], scope["effort"])
                == (p["host"], p["model"], p["effort"])
                and metadata.get("billing_basis_ref") == "subscription-only:" + self.reference,
                "Invocation is outside its installed permission",
            )
            request = {
                "label": label,
                "paid": paid,
                "uses_worker": uses_worker,
                "metadata": metadata,
                "timeout_seconds": timeout_seconds,
            }
            old = self.store.read(conn, "native_calls", call_id)
            if old is not None:
                require(old["request_digest"] == digest(request), "Invocation key changed")
                return old
            if purpose == "auth":
                count = conn.execute(
                    "SELECT count(*) FROM native_calls WHERE permission_ref=? "
                    "AND attempt_id=? AND purpose='auth'",
                    (self.reference, scope["attempt_id"]),
                ).fetchone()[0]
                require(
                    count < 1 + len(p["identity_refreshes"]), "Finite identity-auth plan exhausted"
                )
            if purpose == "assignment" and p.get("kind") == "native-conformance-probe":
                count = conn.execute(
                    "SELECT count(*) FROM native_calls WHERE permission_ref=? "
                    "AND attempt_id=? AND purpose='assignment'",
                    (self.reference, scope["attempt_id"]),
                ).fetchone()[0]
                require(count < len(p["case_ids"]), "Finite canary submission plan exhausted")
            if uses_worker:
                require(
                    conn.execute(
                        "SELECT 1 FROM native_calls WHERE permission_ref=? "
                        "AND process_stopped=0 LIMIT 1",
                        (self.reference,),
                    ).fetchone()
                    is None,
                    "Normal host slot remains occupied",
                )
            row = {
                "call_id": call_id,
                "permission_ref": self.reference,
                **request,
                "request_digest": digest(request),
                "status": "reserved",
                "worker_identity": None,
                "worker_state": "reserved" if uses_worker else "released",
                "process_stopped": False,
                "outcome": None,
                "started_at": None,
                "process_pid": None,
                "process_start_identity": None,
                "invocation_bound": None,
                "launch_token": None,
            }
            self.store.put(conn, "native_calls", call_id, row)
            self.store.event(conn, "reserved", call_id, {"request_digest": row["request_digest"]})
            return row

    def claim_launch(self, call_id, *, worker_identity, not_after):
        with self.store.transaction() as conn:
            p = self._current(conn)
            row = self.store.read(conn, "native_calls", call_id)
            require(
                row is not None
                and row["permission_ref"] == self.reference
                and row["status"] == "reserved"
                and isinstance(worker_identity, str)
                and worker_identity
                and isinstance(not_after, datetime)
                and not_after.tzinfo is not None,
                "Invocation launch is not claimable",
            )
            now = self.clock()
            end = min(
                now + timedelta(seconds=row["timeout_seconds"]),
                not_after,
                datetime.fromtimestamp(p["deadline"], UTC),
            )
            parent_id = row["metadata"].get("parent_call_id")
            purpose = row["metadata"]["invocation_binding"]["purpose"]
            require(
                (purpose == "host") == (parent_id is None), "Child needs its counted host parent"
            )
            if parent_id is not None:
                parent = self.store.read(conn, "native_calls", parent_id)
                require(
                    parent is not None
                    and parent["permission_ref"] == self.reference
                    and parent["uses_worker"]
                    and not parent["process_stopped"]
                    and parent["status"]
                    in ({"launching", "running"} if purpose in {"relay", "auth"} else {"running"})
                    and parent["worker_identity"] == worker_identity
                    and all(
                        parent["metadata"]["invocation_binding"][k]
                        == row["metadata"]["invocation_binding"][k]
                        for k in ["attempt_id", "host", "configuration_digest", "model", "effort"]
                    ),
                    "Child invocation belongs to another or stopped parent",
                )
                end = min(
                    end, datetime.fromisoformat(parent["invocation_bound"]["execution_deadline"])
                )
            require(now < end, "Invocation launch deadline expired")
            row.update(
                status="launching",
                worker_identity=worker_identity,
                invocation_bound={"execution_deadline": end.isoformat()},
            )
            self.store.put(conn, "native_calls", call_id, row)
            self.store.event(conn, "launch_claimed", call_id, {"worker_identity": worker_identity})
            return row

    def _owned(self, conn, call_id, worker_identity, status):
        self._current(conn)
        row = self.store.read(conn, "native_calls", call_id)
        require(
            row is not None
            and row["permission_ref"] == self.reference
            and row["status"] == status
            and row["worker_identity"] == worker_identity
            and self.clock()
            < datetime.fromisoformat(row["invocation_bound"]["execution_deadline"]),
            "Invocation owner, state or original deadline changed",
        )
        parent_id = row["metadata"].get("parent_call_id")
        if parent_id is not None:
            parent = self.store.read(conn, "native_calls", parent_id)
            purpose = row["metadata"]["invocation_binding"]["purpose"]
            require(
                parent is not None
                and parent["permission_ref"] == self.reference
                and parent["uses_worker"]
                and not parent["process_stopped"]
                and parent["status"]
                in ({"launching", "running"} if purpose in {"relay", "auth"} else {"running"})
                and parent["worker_identity"] == worker_identity
                and self.clock()
                < datetime.fromisoformat(parent["invocation_bound"]["execution_deadline"])
                and all(
                    parent["metadata"]["invocation_binding"][k]
                    == row["metadata"]["invocation_binding"][k]
                    for k in ["attempt_id", "host", "configuration_digest", "model", "effort"]
                ),
                "Parent invocation is stopped, expired or no longer owns this child",
            )
        return row

    @contextmanager
    def launch_boundary(self, call_id, *, worker_identity):
        # Durable entry precedes the physical effect. A later SQLite rollback
        # cannot erase the possibility that this one launch already happened.
        token = str(uuid4())
        with self.store.transaction() as conn:
            row = self._owned(conn, call_id, worker_identity, "launching")
            require(
                row["launch_token"] is None, "A prior launch entry is unresolved; never replay it"
            )
            row["launch_token"] = token
            self.store.put(conn, "native_calls", call_id, row)
            self.store.event(conn, "launch_entered", call_id, {"token": token})
        with self.store.transaction() as conn:
            row = self._owned(conn, call_id, worker_identity, "launching")
            require(row["launch_token"] == token, "Launch entry belongs to another owner")
            reported = False

            def report(*, process_pid, process_start_identity):
                nonlocal reported
                require(
                    not reported
                    and (
                        process_pid is None
                        or type(process_pid) is int
                        and process_pid > 0
                        and isinstance(process_start_identity, str)
                        and process_start_identity
                    ),
                    "Actual start may be reported only once",
                )
                row.update(
                    status="running",
                    started_at=self.clock().isoformat(),
                    process_pid=process_pid,
                    process_start_identity=process_start_identity,
                    worker_state="running" if row["uses_worker"] else "released",
                )
                self.store.put(conn, "native_calls", call_id, row)
                self.store.event(conn, "started", call_id, {"worker_identity": worker_identity})
                reported = True

            yield report
            require(reported, "Missing start report leaves launch unresolved")

    @contextmanager
    def effect_boundary(self, call_id, *, worker_identity):
        with self.store.transaction() as conn:
            row = self._owned(conn, call_id, worker_identity, "running")
            yield {"run_id": self.store.binding.store_id, "policy_revision": 1, "call": row}

    @contextmanager
    def identity_boundary(self, invocation, *, not_after, cancelled=None):
        from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding

        require(
            type(invocation) is InvocationBinding
            and type(not_after) in {int, float}
            and math.isfinite(not_after)
            and not_after <= invocation.deadline,
            "Original finite identity parent authority required",
        )

        def validate(conn):
            self._current(conn)
            row = self._owned(conn, invocation.call_id, invocation.worker_identity, "running")
            require(
                row["uses_worker"]
                and not row["process_stopped"]
                and InvocationBinding.observed(
                    {"run_id": self.store.binding.store_id, "policy_revision": 1, "call": row}
                )
                == invocation
                and self.clock().timestamp() < not_after,
                "Original identity parent, scope or validity changed",
            )

        with self.store.settlement_snapshot() as conn:
            validate(conn)
            deadline = min(not_after, self.permission["deadline"])
        with self.store.identity_acquisition(
            not_after=deadline,
            clock=lambda: self.clock().timestamp(),
            cancelled=cancelled,
            validate=validate,
        ):
            # The body performs each admission/launch/accounting operation once.
            # Only its zero-effect writer acquisition may wait.
            with self.store.transaction():
                pass
            yield
            with self.store.transaction():
                pass

    def settle_outcome(self, invocation, **observed):
        return self.record_outcome(invocation.call_id, invocation=invocation, **observed)

    def record_outcome(
        self,
        call_id,
        *,
        outcome,
        process_stopped,
        evidence_ref,
        invocation=None,
        cancelled=None,
        settlement_check=None,
    ):
        require(
            outcome in {"succeeded", "failed", "interrupted", "unknown"}
            and type(process_stopped) is bool
            and isinstance(evidence_ref, str)
            and evidence_ref.startswith("host-record:"),
            "Trusted accounting outcome required",
        )
        wait = {}
        if invocation is not None:
            from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding

            require(
                type(invocation) is InvocationBinding and invocation.call_id == call_id,
                "Original native settlement binding required",
            )
            # Read the original persisted authority before waiting. A supplied
            # extended/foreign binding cannot buy a longer lock-acquisition wait.
            with self.store.settlement_snapshot() as conn:
                current = self._current(conn, admission=False)
                original = self.store.read(conn, "native_calls", call_id)
                require(
                    original is not None
                    and original["permission_ref"] == self.reference
                    and InvocationBinding.observed(
                        {
                            "run_id": self.store.binding.store_id,
                            "policy_revision": 1,
                            "call": original,
                        }
                    )
                    == invocation,
                    "Original native settlement owner or deadline differs",
                )
                deadline = min(invocation.deadline, current["deadline"])
            wait = {
                "wait_until": deadline,
                "clock": lambda: self.clock().timestamp(),
                "cancelled": cancelled,
            }
        with self.store.transaction(**wait) as conn:
            if settlement_check is not None:
                require(callable(settlement_check), "Current settlement guard must be callable")
                settlement_check()
            # Settlement remains possible after expiry/revocation; it cannot launch work.
            self._current(conn, admission=False)
            row = self.store.read(conn, "native_calls", call_id)
            require(
                row is not None
                and row["permission_ref"] == self.reference
                and (not row["process_stopped"] or process_stopped)
                and (row["status"] not in {"succeeded", "failed"} or row["outcome"] == outcome)
                and (
                    outcome != "succeeded" or row["status"] in {"running", "unknown", "succeeded"}
                ),
                "Outcome cannot replace ownership, history or an observed stop",
            )
            if invocation is not None:
                require(
                    InvocationBinding.observed(
                        {"run_id": self.store.binding.store_id, "policy_revision": 1, "call": row}
                    )
                    == invocation,
                    "Native settlement owner, scope or original deadline changed",
                )
            row.update(
                status="failed" if outcome == "interrupted" else outcome,
                outcome=outcome,
                process_stopped=process_stopped,
                evidence=evidence_ref,
                worker_state="released"
                if process_stopped or not row["uses_worker"]
                else "uncertain",
            )
            self.store.put(conn, "native_calls", call_id, row)
            self.store.event(
                conn,
                "outcome",
                call_id,
                {
                    "outcome": outcome,
                    "process_stopped": process_stopped,
                    "evidence_ref": evidence_ref,
                },
            )
            if settlement_check is not None:
                settlement_check()
            return row

    def _abort_unreturned_claims(self, rows, *, worker_identity, evidence_ref):
        require(
            isinstance(evidence_ref, str) and evidence_ref.startswith("host-record:"),
            "Producer abort needs its retained accounting observation",
        )
        with self.store.transaction() as conn:
            self._current(conn, admission=False)
            for expected in rows:
                row = self.store.read(conn, "native_calls", expected["call_id"])
                require(
                    row == expected
                    and row["permission_ref"] == self.reference
                    and row["status"] in {"reserved", "launching"}
                    and row["worker_identity"] in {None, worker_identity}
                    and row["started_at"] is None
                    and row["launch_token"] is None
                    and row["process_pid"] is None
                    and row["process_start_identity"] is None
                    and not row["process_stopped"],
                    "Started or changed invocation cannot use pre-return abort",
                )
                row.update(
                    status="failed",
                    outcome="failed",
                    process_stopped=True,
                    worker_state="released",
                    evidence=evidence_ref,
                )
                self.store.put(conn, "native_calls", row["call_id"], row)
                self.store.event(
                    conn, "unreturned_abort", row["call_id"], {"evidence_ref": evidence_ref}
                )
