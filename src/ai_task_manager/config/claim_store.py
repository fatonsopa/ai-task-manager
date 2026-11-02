"""Installer-owned enrollment; session configuration cannot elect a claim store."""

import sqlite3
from dataclasses import dataclass
from uuid import UUID, uuid4

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.database import Database

from .files import private_path


def file_identity(path):
    value = private_path(path).stat()
    return {"device": value.st_dev, "inode": value.st_ino}


def enroll_claim_store(database, journal):
    """Trusted installer port only. Existing enrollment is never replaced here."""
    with database.transaction() as conn:
        store_id = database.identity(conn)
    payload = {
        "installation_id": str(uuid4()),
        "store_id": store_id,
        "file_identity": file_identity(database.path),
    }
    return journal.bind_claim_store(payload)


def _validate_transaction(conn, expected):
    """Exercise moved-file write protection before read-only authority can yield.

    This is bounded rollback-journal validation on the actual connection, not
    descriptor attestation. Unsupported modes must not inherit its guarantee.
    """
    if not conn.in_transaction:
        raise AccessDenied("Claim-store validation requires an owned transaction")
    try:
        modes = [row[0] for row in conn.execute("PRAGMA main.journal_mode")]
    except sqlite3.Error as exc:
        raise AccessDenied("Claim-store journal mode is unavailable") from exc
    if modes != ["delete"]:
        raise AccessDenied("Enrolled claim stores require DELETE journal mode")
    try:
        temporary = str(uuid4())
        if str(UUID(temporary)) != temporary or temporary == expected:
            raise ValueError("Temporary store identity must be valid and different")
    except Exception as exc:
        raise AccessDenied("Claim-store validation identity is unavailable") from exc
    try:
        conn.execute("SAVEPOINT aitm_claim_store_validation")
        changed = conn.execute(
            "UPDATE main.store_identity SET id=? WHERE id=?", (temporary, expected)
        ).rowcount
        if changed != 1:
            raise AccessDenied("Expected claim-store identity was not uniquely matched")
        conn.execute("ROLLBACK TO aitm_claim_store_validation")
        conn.execute("RELEASE aitm_claim_store_validation")
        if Database.identity(conn) != expected:
            raise AccessDenied("Claim-store identity was not restored")
    except sqlite3.Error as exc:
        raise AccessDenied("Claim-store connection validation failed") from exc


@dataclass(frozen=True)
class ClaimStore:
    path: str
    journal: object
    enrollment: dict

    @property
    def context(self):
        return {key: self.enrollment[key] for key in ("installation_id", "store_id")}

    def _binding(self, conn):
        current = self.journal.claim_store()
        if current != self.enrollment or file_identity(self.path) != current["file_identity"]:
            raise AccessDenied("The enrolled claim store changed; trusted recovery is required")
        if conn is not None and Database.identity(conn) != current["store_id"]:
            raise AccessDenied("Transaction does not use the enrolled claim store")
        if conn is not None:
            actual = conn.execute("PRAGMA database_list").fetchall()
            main = [row[2] for row in actual if row[1] == "main"]
            if len(main) != 1 or file_identity(main[0]) != current["file_identity"]:
                raise AccessDenied("Transaction claim-store file changed")
        return current

    def check(self, conn=None):
        current = self._binding(conn)
        if conn is not None:
            _validate_transaction(conn, current["store_id"])
            self._binding(conn)
        return self.context


def verify_claim_store(database, journal):
    enrollment = journal.claim_store()
    guard = ClaimStore(database.path, journal, enrollment)
    guard.check()
    with database.transaction() as conn:
        guard.check(conn)
    database.guard = guard.check
    database.claim_context = guard.context
    return guard


def open_claim_store(path, journal):
    guard = ClaimStore(path, journal, journal.claim_store())
    guard.check()
    database = Database(path, guard=guard.check)
    database.claim_context = guard.context
    return database, guard
