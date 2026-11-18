"""Separate signed local records; verification performs no provider or Core I/O."""

import hashlib
import hmac
import json
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.decisions.journal import AuthorityJournal, canonical
from ai_task_manager.domains.scheduling import policy
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict, StaleVersion
from ai_task_manager.storage.scheduling import snapshot_from

PREFIX = "linear-dependency:"


def key(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class DependencyReceipts(AuthorityJournal):
    """Use its own private file/key, never the general decision-authority store."""

    def __init__(self, path, key_path, **kwargs):
        super().__init__(path, key_path, **kwargs)
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dependency_heads "
                "(id TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dependency_operations "
                "(id TEXT PRIMARY KEY, context TEXT NOT NULL, "
                "generation INTEGER NOT NULL, receipt TEXT)"
            )

    @staticmethod
    def _keys(scope, task_id, operation_key):
        if not isinstance(operation_key, str) or not 0 < len(operation_key) <= 512:
            raise AccessDenied("A bounded dependency operation key is required")
        identity = {"scope": asdict(scope), "task_id": task_id}
        return key(identity), key(dict(identity, operation_key=operation_key))

    def lookup(self, scope, task_id, operation_key):
        _, operation = self._keys(scope, task_id, operation_key)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT context,generation,receipt FROM dependency_operations WHERE id=?",
                (operation,),
            ).fetchone()
        if row and row[2]:
            # Only historical context resolution ignores expiry. New consumption
            # still uses the normal signed freshness check in verify_snapshot.
            with self.transaction() as conn:
                record = conn.execute(
                    "SELECT data,mac FROM records WHERE ref=?", (row[2],)
                ).fetchone()
            if record is None or not hmac.compare_digest(
                record[1], hmac.new(self._key, record[0].encode(), hashlib.sha256).hexdigest()
            ):
                raise AccessDenied("Dependency operation receipt changed")
            value = json.loads(record[0])
            if (
                value.get("ref") != row[2]
                or value.get("context") != json.loads(row[0])
                or value.get("snapshot", {}).get("source_revision") != row[1]
            ):
                raise AccessDenied("Dependency operation context changed")
        return (
            {
                "context": json.loads(row[0]),
                "generation": row[1],
                "receipt": row[2],
                "source_id": value["snapshot"]["source_id"] if row[2] else None,
            }
            if row
            else None
        )

    def begin(self, scope, task, graph_version, operation_key):
        head, operation = self._keys(scope, task.id, operation_key)
        context = {
            "scope": asdict(scope),
            "task_id": task.id,
            "issue_id": task.issue_id,
            "prose_digest": task.prose_digest,
            "task_version": task.version,
            "decision_revision": task.decision_revision,
            "previous_graph_version": graph_version,
            "started_at": self.clock(),
        }
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT context,generation,receipt FROM dependency_operations WHERE id=?",
                (operation,),
            ).fetchone()
            if row:
                return {"context": json.loads(row[0]), "generation": row[1], "receipt": row[2]}
            previous = conn.execute(
                "SELECT generation FROM dependency_heads WHERE id=?", (head,)
            ).fetchone()
            generation = (previous[0] if previous else 0) + 1
            conn.execute(
                "INSERT INTO dependency_heads VALUES(?,?) ON CONFLICT(id) "
                "DO UPDATE SET generation=excluded.generation",
                (head, generation),
            )
            conn.execute(
                "INSERT INTO dependency_operations VALUES(?,?,?,NULL)",
                (operation, canonical(context), generation),
            )
        return {"context": context, "generation": generation, "receipt": None}

    def finish(self, scope, task_id, operation_key, context, snapshot, raw):
        policy.validate(snapshot, self.clock())
        head, operation = self._keys(scope, task_id, operation_key)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT context,generation,receipt FROM dependency_operations WHERE id=?",
                (operation,),
            ).fetchone()
            if row is None or json.loads(row[0]) != context:
                raise IdempotencyConflict("Dependency collection context changed")
            if row[2]:
                return row[2]
            generation = conn.execute(
                "SELECT generation FROM dependency_heads WHERE id=?", (head,)
            ).fetchone()[0]
            if generation != row[1] or snapshot.source_revision != generation:
                raise StaleVersion("A newer dependency observation has started")
            if (
                snapshot.scope != scope
                or snapshot.task_id != task_id
                or snapshot.prose_digest != context["prose_digest"]
                or snapshot.canonical_issue_id != context["issue_id"]
                or snapshot.previous_graph_version != context["previous_graph_version"]
            ):
                raise AccessDenied("Dependency receipt does not match collection authority")
            reference = PREFIX + str(uuid4())
            value = {
                "ref": reference,
                "purpose": "dependency_snapshot",
                "context": context,
                "snapshot": asdict(snapshot),
                "raw": raw,
                "raw_sha256": key(raw),
                "expires_at": snapshot.expires_at,
                "head": head,
            }
            encoded = canonical(value)
            mac = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
            conn.execute("INSERT INTO records VALUES(?,?,?)", (reference, encoded, mac))
            conn.execute(
                "UPDATE dependency_operations SET receipt=? WHERE id=?", (reference, operation)
            )
            return reference

    def verify_snapshot(self, reference):
        value = self._get(reference, PREFIX)
        if value.get("purpose") != "dependency_snapshot" or value.get("raw_sha256") != key(
            value.get("raw")
        ):
            raise AccessDenied("Dependency receipt is invalid")
        snapshot = snapshot_from(value["snapshot"])
        policy.validate(snapshot, self.clock())
        context = value["context"]
        if (
            asdict(snapshot.scope) != context["scope"]
            or snapshot.task_id != context["task_id"]
            or snapshot.canonical_issue_id != context["issue_id"]
            or snapshot.prose_digest != context["prose_digest"]
            or snapshot.previous_graph_version != context["previous_graph_version"]
        ):
            raise AccessDenied("Dependency snapshot context changed")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT generation FROM dependency_heads WHERE id=?", (value["head"],)
            ).fetchone()
        if row is None or row[0] != snapshot.source_revision:
            raise AccessDenied("Dependency receipt was superseded")
        return snapshot

    def published_reference(self, scope, task_id, source_revision):
        """Locate one signed receipt for the revision already published in Core."""
        head, _ = self._keys(scope, task_id, "published-reference")
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT ref FROM records WHERE json_extract(data,'$.head')=? "
                "AND json_extract(data,'$.snapshot.source_revision')=?",
                (head, source_revision),
            ).fetchall()
        if len(rows) != 1:
            raise AccessDenied("Current published dependency receipt is unavailable")
        # Selection is never authority: the normal MAC/context/latest-head and
        # original freshness checks still decide whether it can be consumed.
        self.verify_snapshot(rows[0][0])
        return rows[0][0]
