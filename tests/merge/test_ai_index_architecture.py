import ast
import importlib.util
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.projects.ai_index.journal import Journal
from ai_task_manager.adapters.projects.ai_index.operations import Operations
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src/ai_task_manager/adapters/projects/ai_index"
VARIANTS = ["relative_imports", "core_sql", "missing_runner", "import_time", "no_generic_issuer"]


def modules():
    return sorted([PACKAGE.parent / "__init__.py", *PACKAGE.glob("*.py")])


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_no_external_effect_or_duplicate_policy(harness, monkeypatch, variant):
    if variant == "relative_imports":
        assert Path(sys.modules[Journal.__module__].__file__).resolve() == PACKAGE / "journal.py"
        allowed = {
            "json",
            "re",
            "contextlib",
            "dataclasses",
            "enum",
            "typing",
            "os",
            "sqlite3",
            "pathlib",
            "uuid",
        }
        shared = {"ai_task_manager.config.files", "ai_task_manager.domains.work.errors"}
        for path in modules():
            module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    assert all(n.name.split(".")[0] in allowed for n in node.names)
                elif isinstance(node, ast.ImportFrom):
                    name = (
                        importlib.util.resolve_name(
                            "." * node.level + (node.module or ""), module.rsplit(".", 1)[0]
                        )
                        if node.level
                        else node.module
                    )
                    assert (
                        name in shared
                        or name.split(".")[0] in allowed
                        or name.startswith("ai_task_manager.adapters.projects.ai_index.")
                    )
    elif variant == "core_sql":
        for path in modules():
            text = path.read_text()
            assert not any(
                x in text
                for x in [
                    "UPDATE tasks",
                    "INSERT INTO tasks",
                    "FROM tasks",
                    "UPDATE outbox",
                    "TaskStatus",
                    '"Backlog"',
                    '"In Progress"',
                ]
            )
        foreign = harness.journal.path.parent / "foreign.sqlite"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE foreign_table(value TEXT)")
        conn.commit()
        conn.close()
        foreign.chmod(0o600)
        with pytest.raises(SchemaError):
            Journal(foreign)
        conn = sqlite3.connect(foreign)
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("foreign_table",)
        ]
        conn.close()
    elif variant == "missing_runner":
        adapter = Operations(harness.journal, harness.authority)
        with pytest.raises(AccessDenied, match="not configured"):
            adapter.execute_registered("model-supplied-command", SimpleNamespace())
    elif variant == "import_time":

        def denied(*args, **kwargs):
            raise AssertionError("Import attempted an external effect")

        source_units = [(path, path.read_text()) for path in modules()]
        monkeypatch.setattr(socket, "socket", denied)
        monkeypatch.setattr(subprocess, "Popen", denied)
        monkeypatch.setattr(sqlite3, "connect", denied)
        monkeypatch.setattr(Path, "open", denied)
        # Execute each reviewed module's top-level code in a disposable namespace.
        # Existing module objects/classes remain unchanged for other test cases.
        for path, raw in source_units:
            module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
            namespace = {
                "__name__": module,
                "__package__": module.rsplit(".", 1)[0],
                "__file__": str(path),
            }
            exec(compile(raw, str(path), "exec"), namespace)
    else:
        for path in modules():
            tree = ast.parse(path.read_text())
            names = {
                n.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert not names & {
                "issue_evidence",
                "mint",
                "issue_proof",
                "issue_human",
                "complete_task",
                "set_status",
            }
            text = path.read_text()
            assert not any(
                v in text
                for v in [
                    "os.environ",
                    "os.getenv",
                    ".env.local",
                    "read_private(",
                    "urllib",
                    "requests",
                    "subprocess",
                    "linear_sync",
                ]
            )
