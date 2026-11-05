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


def owned_image_receipt(path, *, source_alias=False):
    """Authored existing collision evidence; never an authority issuer."""
    import os

    path.chmod(0o600)
    image = path.with_name(path.name + ".schema2-before.sqlite")
    receipt = path.with_name(path.name + ".schema2-before.json")
    before = path.read_bytes()
    if source_alias:
        os.link(path, image)
    else:
        image.write_bytes(before)
    image.chmod(0o600)
    source_stat, image_stat = path.stat(), image.stat()
    receipt.write_text(
        json.dumps(
            {
                "version": 1,
                "source": str(path.absolute()),
                "source_identity": [source_stat.st_dev, source_stat.st_ino],
                "source_schema": 2,
                "sha256": hashlib.sha256(before).hexdigest(),
                "backup_identity": [image_stat.st_dev, image_stat.st_ino],
            }
        )
    )
    receipt.chmod(0o600)
    return image, receipt


def test_matching_receipt_cannot_adopt_source_image_hardlink(tmp_path):
    path = legacy(tmp_path)
    image, receipt = owned_image_receipt(path, source_alias=True)
    before, inode, receipt_bytes = path.read_bytes(), path.stat().st_ino, receipt.read_bytes()
    assert path.stat().st_ino == image.stat().st_ino
    with pytest.raises(SchemaError):
        Database(path)
    assert path.read_bytes() == image.read_bytes() == before
    assert path.stat().st_ino == inode
    assert receipt.read_bytes() == receipt_bytes
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("linked", ["source", "image", "receipt"])
def test_multilink_migration_artifacts_refuse_without_mutation(tmp_path, linked):
    import os

    path = legacy(tmp_path)
    image, receipt = owned_image_receipt(path)
    target = {"source": path, "image": image, "receipt": receipt}[linked]
    alias = tmp_path / "another-owned-name"
    os.link(target, alias)
    originals = {p: p.read_bytes() for p in (path, image, receipt, alias)}
    identities = {p: (p.stat().st_dev, p.stat().st_ino) for p in originals}
    with pytest.raises(SchemaError):
        Database(path)
    for p, data in originals.items():
        assert p.read_bytes() == data
        assert (p.stat().st_dev, p.stat().st_ino) == identities[p]


def test_matching_independent_existing_image_remains_reusable(tmp_path):
    path = legacy(tmp_path)
    image, receipt = owned_image_receipt(path)
    before = {p: p.read_bytes() for p in (image, receipt)}
    inode = path.stat().st_ino
    Database(path)
    Database(path)
    assert path.stat().st_ino == inode
    for p, data in before.items():
        assert p.read_bytes() == data
        assert p.stat().st_nlink == 1


def test_image_link_created_during_verification_refuses_before_schema_edits(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    path = legacy(tmp_path)
    image, _ = owned_image_receipt(path)
    before, inode = path.read_bytes(), path.stat().st_ino
    alias = tmp_path / "late-image-alias"
    read_bytes = Path.read_bytes

    def linked_read(candidate):
        data = read_bytes(candidate)
        if candidate == image and not alias.exists():
            os.link(image, alias)
        return data

    monkeypatch.setattr(Path, "read_bytes", linked_read)
    with pytest.raises(SchemaError, match="one physical link"):
        Database(path)
    assert path.read_bytes() == image.read_bytes() == alias.read_bytes() == before
    assert path.stat().st_ino == inode
