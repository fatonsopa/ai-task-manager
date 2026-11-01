import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from ai_task_manager.domains.work.errors import SchemaError
from ai_task_manager.storage.database import Database
from tests.domains.helpers import Rig


def test_p21_p23_reopen_preserves_pending_intent_and_attempt_history(tmp_path):
    path = tmp_path / "fixture.sqlite"
    rig = Rig(path)
    task = rig.task(confirmed=False)
    old_events = rig.core.notifications.list_events(rig.handle)
    rig.db = Database(path)
    rig.core = rig.new_core()
    assert rig.core.get_task(rig.handle, task.id) == task
    assert rig.core.notifications.list_events(rig.handle) == old_events
    assert len(rig.core.sync.list_operations(rig.handle)) == 1
    assert rig.core.sync.list_operations(rig.handle)[0].state == "pending"


def test_p21_unknown_or_foreign_schema_is_refused_without_reset(tmp_path):
    path = tmp_path / "fixture.sqlite"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metadata SET version=999")
    with pytest.raises(SchemaError):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 999
    foreign = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE valuable_data(value TEXT)")
        conn.execute("INSERT INTO valuable_data VALUES('retained')")
    with pytest.raises(SchemaError):
        Database(foreign)
    with sqlite3.connect(foreign) as conn:
        assert (
            conn.execute("SELECT value FROM valuable_data").fetchone()[0] == "retained"
        )


def test_p22_directional_imports_and_no_provider_process_ui_or_legacy_dependency():
    root = Path(__file__).parents[2] / "src" / "ai_task_manager"
    allowed_stdlib = {
        "collections",
        "dataclasses",
        "enum",
        "typing",
        "hashlib",
        "json",
        "sqlite3",
        "pathlib",
        "contextlib",
        "secrets",
        "uuid",
        "time",
    }
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                imports = [node.module]
            else:
                continue
            for name in imports:
                assert name.split(".")[0] in allowed_stdlib | {"ai_task_manager"}, (
                    path,
                    name,
                )
                if "domains" in path.parts:
                    assert not name.startswith(
                        ("ai_task_manager.application", "ai_task_manager.storage")
                    )
                if "storage" in path.parts:
                    assert not name.startswith("ai_task_manager.application")


def test_frozen_precode_case_manifest_is_unchanged():
    path = Path(__file__).parents[1] / "domains" / "frozen" / "core-cases-v3.json"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "24fd526e6dd80e71e06c0b2b7cdd5eb12b8d3e337e4f6e9a7f3ed593079a9b75"
    )
    assert {case["id"] for case in json.loads(path.read_text())["cases"]} == {
        f"P{i:02}" for i in range(1, 27)
    }
