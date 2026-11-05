"""Retained Core migration images; never a restore or work-authority API."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_task_manager.domains.work.errors import SchemaError
from ai_task_manager.storage.database import Database
from tests.scheduling.helpers import LegacyMigrationFixture


def legacy(tmp_path, version=2):
    path = tmp_path / "core.sqlite"
    if version == 3:
        from tests.scheduling.test_migration import legacy_store

        legacy_store(path)
    else:
        fixture = LegacyMigrationFixture(path, version)
        fixture.task(confirmed=False)
    return path


@pytest.mark.parametrize("version", [1, 2, 3])
def test_existing_core_migration_retains_private_original_image(tmp_path, version):
    path = legacy(tmp_path, version)
    before, inode = path.read_bytes(), path.stat().st_ino
    Database(path)
    backup = path.with_name(path.name + f".schema{version}-before.sqlite")
    receipt = path.with_name(path.name + f".schema{version}-before.json")
    assert backup.read_bytes() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text())["sha256"] == hashlib.sha256(before).hexdigest()
    assert path.stat().st_ino == inode
    with sqlite3.connect("file:" + str(backup) + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == version
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_failed_upgrade_recovers_in_place_using_same_retained_image(tmp_path, monkeypatch):
    from ai_task_manager.storage import publication_schema

    path = legacy(tmp_path)
    before, inode = path.read_bytes(), path.stat().st_ino
    original = publication_schema.migrate

    def fail(conn):
        original(conn)
        raise RuntimeError("authored interruption after DDL")

    monkeypatch.setattr(publication_schema, "migrate", fail)
    with pytest.raises(RuntimeError, match="authored interruption"):
        Database(path)
    assert path.read_bytes() == before
    backup = path.with_name(path.name + ".schema2-before.sqlite")
    assert backup.read_bytes() == before
    backup_inode = backup.stat().st_ino
    monkeypatch.setattr(publication_schema, "migrate", original)
    Database(path)
    assert backup.stat().st_ino == backup_inode
    assert path.stat().st_ino == inode
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 5


@pytest.mark.parametrize("collision", ["partial", "changed", "symlink"])
def test_incomplete_or_foreign_beforeimage_refuses_without_migration(tmp_path, collision):
    path = legacy(tmp_path)
    before = path.read_bytes()
    backup = path.with_name(path.name + ".schema2-before.sqlite")
    if collision == "symlink":
        foreign = tmp_path / "foreign"
        foreign.write_bytes(b"keep")
        backup.symlink_to(foreign)
    else:
        backup.write_bytes(b"partial" if collision == "partial" else before)
        backup.chmod(0o600)
        if collision == "changed":
            receipt = path.with_name(path.name + ".schema2-before.json")
            receipt.write_text("{}")
            receipt.chmod(0o600)
    with pytest.raises(SchemaError):
        Database(path)
    assert path.read_bytes() == before
    if collision == "symlink":
        assert foreign.read_bytes() == b"keep"


def test_concurrent_migrators_keep_one_image_and_original_inode(tmp_path):
    path = legacy(tmp_path)
    inode = path.stat().st_ino
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Database(path), range(2)))
    assert path.stat().st_ino == inode
    assert len(list(tmp_path.glob("*.schema2-before.sqlite"))) == 1


def test_fresh_store_has_no_invented_beforeimage(tmp_path):
    Database(tmp_path / "fresh.sqlite")
    assert not list(tmp_path.glob("*.schema*-before.*"))


def test_size_bound_refuses_before_capture_or_migration(tmp_path, monkeypatch):
    from ai_task_manager.storage import core_migration_image

    path = legacy(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(core_migration_image, "MAX_IMAGE_BYTES", 1)
    with pytest.raises(SchemaError, match="size bound"):
        Database(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.schema*-before.*"))
