"""Keep public retry inputs and proof references stable before core effects."""

import json

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import IdempotencyConflict


class Invocation:
    def __init__(self, journal, scope, operation, key, arguments):
        self.journal = journal
        self.id = digest({"scope": vars(scope), "key": key})
        self.fingerprint = digest({"operation": operation, "arguments": arguments})
        with journal.transaction() as conn:
            prior = conn.execute(
                "SELECT fingerprint FROM invocations WHERE id=?", (self.id,)
            ).fetchone()
            if prior and prior[0] != self.fingerprint:
                raise IdempotencyConflict("Operation key was reused with changed arguments")
            conn.execute(
                "INSERT OR IGNORE INTO invocations VALUES(?,?,?,NULL)",
                (self.id, self.fingerprint, "{}"),
            )

    def cached(self):
        with self.journal.transaction() as conn:
            row = conn.execute("SELECT result FROM invocations WHERE id=?", (self.id,)).fetchone()
        return json.loads(row[0]) if row[0] is not None else None

    def reference(self, name, create):
        with self.journal.transaction() as conn:
            context = json.loads(
                conn.execute("SELECT context FROM invocations WHERE id=?", (self.id,)).fetchone()[0]
            )
        if name in context:
            return context[name]
        candidate = create()
        with self.journal.transaction() as conn:
            context = json.loads(
                conn.execute("SELECT context FROM invocations WHERE id=?", (self.id,)).fetchone()[0]
            )
            context.setdefault(name, candidate)
            conn.execute(
                "UPDATE invocations SET context=? WHERE id=?",
                (json.dumps(context, sort_keys=True), self.id),
            )
        return context[name]

    def finish(self, envelope):
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE invocations SET result=COALESCE(result,?) WHERE id=?",
                (json.dumps(envelope, sort_keys=True), self.id),
            )
