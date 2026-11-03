"""Original local history for the narrow expired, unexecuted workflow case."""

import os
import sqlite3
import subprocess
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.config.files import private_path, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired

from .identity import OwnerBinding, file_binding

# These are fixed product schemas, never identifiers supplied by a model.
_DATABASES = {
    "core": ("metadata", 5),
    "workflow": ("workflow_meta", 2),
    "runtime": ("runtime_metadata", 1),
    "host": ("host_version", 2),
    "provider": ("metadata", 4),
}


def physical(path):
    path = private_path(path)
    info = path.stat()
    if path.resolve() != path or info.st_nlink != 1:
        raise AccessDenied("Recovery requires original physical history files")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def stopped_owner(history, scope):
    """An absent descriptor or unknown process observation is not quiescence."""
    expected = history["owner"]
    if file_binding(expected["path"]) != expected:
        raise AccessDenied("Original owner descriptor changed")
    row = read_private(expected["path"])
    if row != history["descriptor"] or row["scope"] != asdict(scope):
        raise AccessDenied("Original owner history differs from the admitted scope")
    endpoint = Path(row["endpoint"]["path"])
    if endpoint.exists() or endpoint.is_symlink():
        raise EvidenceRequired("The previous owner endpoint is still present")
    process = subprocess.run(
        ["ps", "-p", str(row["pid"]), "-o", "uid=,lstart="],
        capture_output=True,
        text=True,
        timeout=2,
    )
    fields = process.stdout.strip().split(maxsplit=1)
    dead = process.returncode == 1 and not process.stdout.strip() and not process.stderr.strip()
    reused = (
        process.returncode == 0
        and len(fields) == 2
        and (fields[0] != str(os.getuid()) or fields[1] != row["start"])
    )
    if not dead and not reused:
        raise EvidenceRequired("The previous owner process is live or unknown")


def _probe(conn, binding, name):
    if physical(binding["path"]) != binding:
        raise AccessDenied("Original recovery journal was replaced")
    table, version = _DATABASES[name]
    if [tuple(r) for r in conn.execute(f"SELECT version FROM {table}")] != [(version,)]:
        raise AccessDenied("Recovery journal schema is unknown")
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise AccessDenied("Recovery requires DELETE journal mode")
    conn.execute("SAVEPOINT recovery_identity")
    if (
        conn.execute(f"UPDATE {table} SET version=version WHERE version=?", (version,)).rowcount
        != 1
    ):
        raise AccessDenied("Recovery journal identity is ambiguous")
    conn.execute("ROLLBACK TO recovery_identity")
    conn.execute("RELEASE recovery_identity")
    if physical(binding["path"]) != binding:
        raise AccessDenied("Recovery journal changed during connection validation")


@contextmanager
def _existing(binding, name):
    if physical(binding["path"]) != binding:
        raise AccessDenied("Original recovery journal is missing or replaced")
    # mode=rw is essential: an absent journal must never be recreated here.
    conn = sqlite3.connect(
        Path(binding["path"]).as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=2
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        _probe(conn, binding, name)
        yield conn
        _probe(conn, binding, name)
    finally:
        conn.rollback()
        conn.close()


class RecoveryHistory:
    """Installer-owned file bindings; not a public request or an evidence assertion."""

    def __init__(self, config_file, *, core, workflow, runtime, host, provider):
        self.config_file = Path(config_file).absolute()
        self.paths = dict(
            core=core, workflow=workflow, runtime=runtime, host=host, provider=provider
        )

    def capture(self, scope):
        descriptor = OwnerBinding(self.config_file, asdict(scope)).check()
        config = read_private(self.config_file)
        if (
            str(Path(self.paths["core"]).absolute()) != config["core_database"]
            or str(Path(self.paths["provider"]).absolute()) != config["linear"]["journal"]
        ):
            raise AccessDenied("Recovery history differs from installed stores")
        return {
            "version": 1,
            "files": {name: physical(path) for name, path in self.paths.items()},
            "owner": file_binding(config["workflow_control"]["record"]),
            "descriptor": descriptor,
        }

    def verify_owner(self, history, scope):
        if not isinstance(history, dict) or history.get("version") != 1:
            raise EvidenceRequired("Original recovery history requires explicit reconciliation")
        if set(history.get("files", {})) != set(_DATABASES):
            raise EvidenceRequired("Complete original journal history is required")
        for name, path in self.paths.items():
            if physical(path) != history["files"][name]:
                raise AccessDenied("Recovery cannot adopt missing or different journal history")
        stopped_owner(history, scope)

    def check_bindings(self, original, successor):
        """Recheck local identity immediately before publishing the successor."""
        for name, path in self.paths.items():
            if physical(path) != original["files"][name]:
                raise AccessDenied("Original recovery history changed")
        for history in (original, successor):
            if file_binding(history["owner"]["path"]) != history["owner"]:
                raise AccessDenied("Recovery owner descriptor changed")
        if file_binding(self.config_file) != successor["descriptor"]["config"]:
            raise AccessDenied("Current recovery configuration changed")
        endpoint = Path(original["descriptor"]["endpoint"]["path"])
        if endpoint.exists() or endpoint.is_symlink():
            raise EvidenceRequired("Previous owner endpoint reappeared before recovery")

    @contextmanager
    def boundary(self, history, scope):
        """Hold journal writers until atomic successor publication has finished."""
        with ExitStack() as stack:
            connections = {
                name: stack.enter_context(_existing(history["files"][name], name))
                for name in ("runtime", "host", "provider")
            }
            runtime, host, provider = (connections[n] for n in ("runtime", "host", "provider"))
            for table in (
                "runtime_attempts",
                "runtime_effects",
                "runtime_observations",
                "runtime_receipts",
            ):
                if runtime.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise EvidenceRequired("Runtime history prevents zero-effect recovery")
            # This first slice is conservative: any native record or intent needs
            # its own reconciliation, including records without a Core attempt.
            for table in ("host_intents", "host_records"):
                if host.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise EvidenceRequired("Native history prevents zero-effect recovery")
            from ai_task_manager.adapters.linear.receipts import encoded

            scoped = encoded(asdict(scope))
            if provider.execute(
                "SELECT 1 FROM sends s WHERE scope=? AND state NOT IN ('confirmed','failed') "
                "AND attempt=(SELECT MAX(t.attempt) FROM sends t WHERE t.scope=s.scope "
                "AND t.operation_id=s.operation_id) LIMIT 1",
                (scoped,),
            ).fetchone():
                raise EvidenceRequired("Unsettled provider sends prevent workflow recovery")
            tables = {r[0] for r in provider.execute("SELECT name FROM sqlite_master")}
            if (
                "progress_publications" in tables
                and provider.execute(
                    "SELECT 1 FROM progress_publications WHERE scope=? "
                    "AND state NOT IN ('confirmed','failed') LIMIT 1",
                    (scoped,),
                ).fetchone()
            ):
                raise EvidenceRequired("Unsettled provider progress prevents workflow recovery")
            if provider.execute(
                "SELECT 1 FROM deliveries WHERE scope=? AND state NOT IN ('applied','ignored') "
                "LIMIT 1",
                (scoped,),
            ).fetchone():
                raise EvidenceRequired("Unapplied provider observations prevent workflow recovery")
            yield
