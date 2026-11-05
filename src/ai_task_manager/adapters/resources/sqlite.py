"""A disposable application fixture, separate from all coordinator metadata."""

import sqlite3
from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused

from .paths import identity, unchanged


class SQLiteFixtures:
    def __init__(self, forbidden_paths=()):
        self.forbidden = {Path(p).resolve() for p in forbidden_paths}

    def create(self, path, owner):
        if path.resolve() in self.forbidden or path.exists() or path.is_symlink():
            raise RuntimeRefused("Application fixture must be a new separate database")
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL prevents sqlite.connect from adopting a database created by another owner.
        path.touch(exist_ok=False, mode=0o600)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE fixture_owner(attempt_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO fixture_owner VALUES(?)", (owner,))
        return self.inspect(path, owner)

    def inspect(self, path, owner):
        if path.resolve() in self.forbidden:
            raise RuntimeRefused("A control database cannot be an application resource")
        physical = identity(path)
        if path.lstat().st_nlink != 1:
            raise RuntimeRefused("Application database must have exactly one physical link")
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                rows = conn.execute("SELECT attempt_id FROM fixture_owner").fetchall()
            if rows != [(owner,)]:
                raise RuntimeRefused("Application database ownership does not match")
        except sqlite3.DatabaseError as error:
            raise RuntimeRefused("Application database has no verified ownership") from error
        return {"path": str(path), "physical": physical, "owner": owner}

    def remove(self, path, receipt):
        unchanged(path, receipt["physical"])
        self.inspect(path, receipt["owner"])
        with sqlite3.connect(path, timeout=0) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RuntimeRefused("Database sidecars require explicit recovery before removal")
        self.inspect(path, receipt["owner"])
        unchanged(path, receipt["physical"])
        path.unlink()
