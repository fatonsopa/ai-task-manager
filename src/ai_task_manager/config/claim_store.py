"""Installer-owned enrollment; session configuration cannot elect a claim store."""

from dataclasses import dataclass
from uuid import uuid4

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


@dataclass(frozen=True)
class ClaimStore:
    path: str
    journal: object
    enrollment: dict

    @property
    def context(self):
        return {key: self.enrollment[key] for key in ("installation_id", "store_id")}

    def check(self, conn=None):
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
