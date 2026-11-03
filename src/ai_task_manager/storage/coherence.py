"""Bounded DELETE-journal connection checks shared by independent owned stores.

The probe exercises SQLite moved-file write protection. It does not attest a
file descriptor or make concurrent live replacement supported.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ai_task_manager.config.files import private_path
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError, WorkError

_STATEMENTS = {
    "workflow_meta": (
        "SELECT version,store_id FROM main.workflow_meta",
        "UPDATE main.workflow_meta SET store_id=? WHERE version=? AND store_id=?",
    ),
    "check_meta": (
        "SELECT version,store_id FROM main.check_meta",
        "UPDATE main.check_meta SET store_id=? WHERE version=? AND store_id=?",
    ),
    "merge_meta": (
        "SELECT version,store_id FROM main.merge_meta",
        "UPDATE main.merge_meta SET store_id=? WHERE version=? AND store_id=?",
    ),
}


class ConnectionBusy(WorkError):
    """Retryable infrastructure contention, not an identity or ownership verdict."""

    retryable = True


def is_busy_sqlite(error):
    return getattr(error, "sqlite_errorcode", 0) & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


@dataclass(frozen=True)
class StoreBinding:
    path: str | Path
    device: int
    inode: int
    table: str
    version: int
    store_id: str

    def __post_init__(self):
        if (type(self.path) is not str and not isinstance(self.path, Path)) or not str(self.path):
            raise ValidationError("An enrolled file path is required")
        if any(type(v) is not int or v < 0 for v in (self.device, self.inode)):
            raise ValidationError("Enrolled device and inode must be explicit integers")
        if type(self.table) is not str or self.table not in _STATEMENTS:
            raise ValidationError("Unsupported owned-store metadata schema")
        if type(self.version) is not int or self.version < 1:
            raise ValidationError("An explicit supported store version is required")
        try:
            if str(UUID(self.store_id)) != self.store_id:
                raise ValueError()
        except (TypeError, ValueError, AttributeError) as error:
            raise ValidationError("A canonical enrolled store UUID is required") from error


def _binding(conn, binding, select):
    expected_path = private_path(binding.path)
    identity = expected_path.stat()
    if (identity.st_dev, identity.st_ino) != (binding.device, binding.inode):
        raise AccessDenied("Enrolled store file identity changed")
    main = [r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"]
    if len(main) != 1 or Path(main[0]).absolute() != expected_path:
        raise AccessDenied("Connection does not use the exact enrolled store path")
    actual = private_path(main[0]).stat()
    if (actual.st_dev, actual.st_ino) != (binding.device, binding.inode):
        raise AccessDenied("Connection store path identity changed")
    rows = conn.execute(select).fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (binding.version, binding.store_id):
        raise AccessDenied("Connection metadata differs from the enrolled store")


def probe_connection(conn, binding: StoreBinding):
    """Validate inside an active transaction, before its body and before commit.

    Caller owns transaction lifetime, busy policy and exception mapping. Nothing
    is committed or retried by this function.
    """
    if type(binding) is not StoreBinding or not conn.in_transaction:
        raise AccessDenied("Connection validation requires a binding and active transaction")
    select, update = _STATEMENTS[binding.table]
    try:
        _binding(conn, binding, select)
        if [r[0] for r in conn.execute("PRAGMA main.journal_mode")] != ["delete"]:
            raise AccessDenied("Connection validation requires DELETE journal mode")
        temporary = str(uuid4())
        if str(UUID(temporary)) != temporary or temporary == binding.store_id:
            raise AccessDenied("Temporary validation identity is unavailable")
        conn.execute("SAVEPOINT aitm_connection_validation")
        changed = conn.execute(update, (temporary, binding.version, binding.store_id)).rowcount
        if changed != 1:
            raise AccessDenied("Store identity was not uniquely matched")
        conn.execute("ROLLBACK TO aitm_connection_validation")
        conn.execute("RELEASE aitm_connection_validation")
        _binding(conn, binding, select)
    except sqlite3.Error as error:
        if is_busy_sqlite(error):
            raise ConnectionBusy(
                "Store connection is busy; retry only zero-effect reads"
            ) from error
        raise AccessDenied("Store connection coherence could not be validated") from error
