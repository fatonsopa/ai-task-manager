"""Durable ordinary administration actions; RunLedger owns budget policy."""

import json

from ai_task_manager.config.files import digest

from .ledger import DriverRefused, uuid


class ActionJournal:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_actions(key TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, resource_id TEXT NOT NULL, action TEXT NOT NULL, "
                "state TEXT NOT NULL, readback_digest TEXT, UNIQUE(resource_id,action))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_action_schema(version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_http_effects(reference TEXT PRIMARY KEY,"
                "operation TEXT NOT NULL,payload_digest TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_create_context("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_receiver_candidates(id TEXT PRIMARY KEY)"
            )
            rows = conn.execute("SELECT version FROM adapter_action_schema").fetchall()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_action_context("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            if not rows:
                conn.execute("INSERT INTO adapter_action_schema VALUES(1)")
            elif [r[0] for r in rows] != [1]:
                raise DriverRefused("unsupported_adapter_action_schema")

    def remember_create(self, key, variables):
        value = {"payload_digest": digest(variables), "url": variables["input"].get("url")}
        with self.ledger.transaction() as conn:
            prior = conn.execute(
                "SELECT value FROM adapter_create_context WHERE key=?", (key,)
            ).fetchone()
            encoded = json.dumps(value, sort_keys=True)
            if prior and prior[0] != encoded:
                raise DriverRefused("create_context_changed")
            conn.execute("INSERT OR IGNORE INTO adapter_create_context VALUES(?,?)", (key, encoded))

    def reserve_receiver(self, candidate):
        uuid(candidate)
        with self.ledger.transaction() as conn:
            self.ledger._ordinary(conn)
            rows = [r[0] for r in conn.execute("SELECT id FROM adapter_receiver_candidates")]
            if rows and rows != [candidate]:
                raise DriverRefused("receiver_candidate_already_reserved")
            conn.execute(
                "INSERT OR IGNORE INTO adapter_receiver_candidates VALUES(?)", (candidate,)
            )
        return candidate

    def receiver_candidate(self, candidate):
        with self.ledger.transaction() as conn:
            if (
                conn.execute(
                    "SELECT id FROM adapter_receiver_candidates WHERE id=?", (candidate,)
                ).fetchone()
                is None
            ):
                raise DriverRefused("local_receiver_reservation_required")
        return candidate

    def create_context(self, key):
        with self.ledger.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM adapter_create_context WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                raise DriverRefused("create_context_missing")
            return json.loads(row[0])

    def require_settled(self, resource_id=None):
        with self.ledger.transaction() as conn:
            query = "SELECT 1 FROM adapter_actions WHERE state NOT IN ('confirmed','abandoned')"
            args = ()
            if resource_id is not None:
                query += " AND resource_id=?"
                args = (resource_id,)
            if conn.execute(query, args).fetchone():
                raise DriverRefused("unresolved_ordinary_effect_retained")

    def abandon(self, key):
        with self.ledger.transaction() as conn:
            row = self.get(key, conn=conn)
            if row["state"] not in {"intent", "abandoned"}:
                raise DriverRefused("only_unsent_action_can_be_abandoned")
            conn.execute("UPDATE adapter_actions SET state='abandoned' WHERE key=?", (key,))
            return self.get(key, conn=conn)

    def remember_action(self, key, url):
        with self.ledger.transaction() as conn:
            self.get(key, conn=conn)
            value = json.dumps({"url": url}, sort_keys=True)
            old = conn.execute(
                "SELECT value FROM adapter_action_context WHERE key=?", (key,)
            ).fetchone()
            if old and old[0] != value:
                raise DriverRefused("ordinary_action_context_changed")
            conn.execute("INSERT OR IGNORE INTO adapter_action_context VALUES(?,?)", (key, value))

    def action_context(self, key):
        with self.ledger.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM adapter_action_context WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                raise DriverRefused("ordinary_action_context_missing")
            return json.loads(row[0])

    def recovery_read(self, key):
        with self.ledger.transaction() as conn:
            row = self.get(key, conn=conn)
            if row["state"] not in {"sent", "uncertain"}:
                raise DriverRefused("ordinary_possible_effect_required")
            self.ledger._time()
            self.ledger._reserved_charge(conn, "ordinary-recovery:" + key, "ordinary_recovery_read")

    def begin(self, key, resource_id, action, payload):
        uuid(resource_id)
        if action != "enable_webhook" or payload != {"id": resource_id, "input": {"enabled": True}}:
            raise DriverRefused("ordinary_action_forbidden")
        if not isinstance(key, str) or not 0 < len(key) <= 256:
            raise DriverRefused("invalid_action_key")
        resource = self.ledger.resource(resource_id)
        fingerprint = digest(
            {
                "run": self.ledger.run_id,
                "resource": resource_id,
                "action": action,
                "payload": payload,
            }
        )
        with self.ledger.transaction() as conn:
            prior = conn.execute("SELECT * FROM adapter_actions WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DriverRefused("ordinary_action_key_collision")
                return dict(prior)
            if resource["kind"] != "webhook" or resource["state"] != "active":
                raise DriverRefused("owned_active_webhook_required")
            self.ledger._ordinary(conn)
            if conn.execute(
                "SELECT 1 FROM adapter_actions WHERE resource_id=? AND action=?",
                (resource_id, action),
            ).fetchone():
                raise DriverRefused("ordinary_action_already_reserved")
            conn.execute(
                "INSERT INTO adapter_actions VALUES(?,?,?,?,'intent',NULL)",
                (key, fingerprint, resource_id, action),
            )
            return self.get(key, conn=conn)

    def consume(self, effect, operation, variables):
        """Consume an actual durable send once, never a caller's charged flag."""
        if not isinstance(effect, tuple) or len(effect) != 2:
            raise DriverRefused("durable_effect_intent_required")
        mode, key = effect
        if mode == "create":
            row = self.ledger.get(key)
            expected = {"project": "project_create", "webhook": "webhook_create"}.get(row["kind"])
            candidate = variables.get("input", {}).get("id")
            fingerprint = digest(
                {"kind": row["kind"], "candidate": candidate, "digest": digest(variables)}
            )
            valid = (
                row["state"] == "sent"
                and operation == expected
                and row["candidate"] == candidate
                and row["fingerprint"] == fingerprint
            )
        elif mode == "ordinary":
            row = self.get(key)
            fingerprint = digest(
                {
                    "run": self.ledger.run_id,
                    "resource": row["resource_id"],
                    "action": row["action"],
                    "payload": variables,
                }
            )
            valid = (
                row["state"] == "sent"
                and operation == "webhook_update"
                and fingerprint == row["fingerprint"]
            )
        elif mode == "cleanup":
            row = self.ledger.cleanup_action(key)
            expected = {
                ("project", "archive"): "project_archive",
                ("issue", "archive"): "issue_archive",
                ("webhook", "disable"): "webhook_update",
                ("webhook", "delete"): "webhook_delete",
            }.get((row["kind"], row["action"]))
            payload = {"id": row["resource_id"]}
            if row["action"] == "disable":
                payload["input"] = {"enabled": False}
            valid = row["state"] == "sent" and operation == expected and variables == payload
        else:
            valid = False
        if not valid:
            raise DriverRefused("effect_context_mismatch")
        reference = f"{mode}:{key}"
        with self.ledger.transaction() as conn:
            if mode == "cleanup":
                self.ledger._time()
            else:
                self.ledger._ordinary(conn)
            if conn.execute(
                "SELECT 1 FROM adapter_http_effects WHERE reference=?", (reference,)
            ).fetchone():
                raise DriverRefused("effect_already_delegated")
            conn.execute(
                "INSERT INTO adapter_http_effects VALUES(?,?,?)",
                (reference, operation, digest(variables)),
            )

    def get(self, key, *, conn=None):
        if conn is None:
            with self.ledger.transaction() as owned:
                return self.get(key, conn=owned)
        row = conn.execute("SELECT * FROM adapter_actions WHERE key=?", (key,)).fetchone()
        if row is None:
            raise DriverRefused("ordinary_action_missing")
        return dict(row)

    def sent(self, key):
        with self.ledger.ordinary_request("product_transport") as conn:
            if (
                conn.execute(
                    "UPDATE adapter_actions SET state='sent' WHERE key=? AND state='intent'", (key,)
                ).rowcount
                != 1
            ):
                raise DriverRefused("ordinary_action_not_sendable")

    def uncertain(self, key):
        with self.ledger.transaction() as conn:
            row = self.get(key, conn=conn)
            if row["state"] != "confirmed":
                if row["state"] not in {"sent", "uncertain"}:
                    raise DriverRefused("ordinary_action_not_sent")
                conn.execute("UPDATE adapter_actions SET state='uncertain' WHERE key=?", (key,))

    def confirm(self, key, readback):
        with self.ledger.transaction() as conn:
            row = self.get(key, conn=conn)
            value = digest(readback)
            if row["state"] == "confirmed" and row["readback_digest"] == value:
                return row
            if row["state"] not in {"sent", "uncertain"}:
                raise DriverRefused("ordinary_action_not_sent")
            conn.execute(
                "UPDATE adapter_actions SET state='confirmed',readback_digest=? WHERE key=?",
                (value, key),
            )
            return self.get(key, conn=conn)
