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
        assert conn.execute("SELECT value FROM valuable_data").fetchone()[0] == "retained"


def _assert_core_imports(path, source, root):
    allowed_stdlib = {
        "collections",
        # Pure data copying in publication_fences predates DP04 (96894ab).
        "copy",
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
        "datetime",
    }
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = ["ai_task_manager", *path.relative_to(root).parts[:-1]]
                base = package[: len(package) - node.level + 1]
                imports = [
                    ".".join(
                        [
                            *base,
                            *(node.module.split(".") if node.module else []),
                            *([] if node.module else [alias.name]),
                        ]
                    )
                    for alias in node.names
                ]
            else:
                imports = [node.module]
        else:
            continue
        for name in imports:
            assert name.split(".")[0] in allowed_stdlib | (
                {"os"} if "storage" in path.relative_to(root).parts else set()
            ) | {"ai_task_manager"}, (
                path,
                name,
            )
            assert not name.startswith(
                (
                    "ai_task_manager.adapters",
                    "ai_task_manager.supervisor",
                    "ai_task_manager.presentation",
                    "ai_task_manager.legacy",
                )
            ), (path, name)
            if "domains" in path.parts:
                assert not name.startswith(
                    ("ai_task_manager.application", "ai_task_manager.storage")
                )
            if "storage" in path.parts:
                assert not name.startswith("ai_task_manager.application")


def test_p22_directional_imports_and_no_provider_process_ui_or_legacy_dependency():
    root = Path(__file__).parents[2] / "src" / "ai_task_manager"
    core_paths = [
        *root.joinpath("domains", "work").rglob("*.py"),
        *root.joinpath("storage").rglob("*.py"),
        *root.joinpath("application").glob("*.py"),
    ]
    for path in core_paths:
        _assert_core_imports(path, path.read_text(), root)


def test_frozen_precode_case_manifest_is_unchanged():
    path = Path(__file__).parents[1] / "domains" / "frozen" / "core-cases-v3.json"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "24fd526e6dd80e71e06c0b2b7cdd5eb12b8d3e337e4f6e9a7f3ed593079a9b75"
    )
    assert {case["id"] for case in json.loads(path.read_text())["cases"]} == {
        f"P{i:02}" for i in range(1, 27)
    }


@pytest.mark.parametrize(
    "layer,source,allowed",
    [
        ("storage", "import os", True),
        ("domains/work", "import os", False),
        ("application", "import os", False),
        ("application", "from copy import deepcopy", True),
        ("storage", "import subprocess", False),
        ("storage", "import socket", False),
        ("storage", "import http.client", False),
        ("storage", "import requests", False),
        ("storage", "import ai_task_manager.adapters.linear.client", False),
        ("storage", "import ai_task_manager.application.core", False),
        ("domains/work", "import ai_task_manager.storage.database", False),
        ("domains/work", "import ai_task_manager.application.core", False),
        ("application", "import ai_task_manager.presentation.terminal", False),
    ],
)
def test_p22_layer_specific_ast_controls(layer, source, allowed):
    root = Path("/authored-core")
    path = root / layer / "fixture.py"
    if allowed:
        _assert_core_imports(path, source, root)
    else:
        with pytest.raises(AssertionError):
            _assert_core_imports(path, source, root)
