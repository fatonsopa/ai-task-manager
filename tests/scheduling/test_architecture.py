import ast
from pathlib import Path

import pytest

from ai_task_manager.application.service.models import SCHEMAS, failure
from ai_task_manager.domains.scheduling.models import SchedulingBlocked


@pytest.mark.parametrize("variant", ["import_direction", "no_sql_in_domain", "no_status_matrix_copy", "no_provider_process_calls"])
def test_single_domain_owner(variant):
    root = Path(__file__).resolve().parents[2] / "src/ai_task_manager/domains/scheduling"
    assert root.is_dir()
    files = list(root.glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text())
        if variant in {"import_direction", "no_provider_process_calls"}:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", "") or ""
                    names = [module] + [item.name for item in node.names]
                    assert not any(word in name for name in names for word in (
                        "adapters", "application", "storage", "supervisor", "subprocess", "urllib", "socket"
                    ))
        elif variant == "no_sql_in_domain":
            assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                           and node.func.attr in {"execute", "executemany", "connect"}
                           for node in ast.walk(tree))
        else:
            assert "check_transition" not in path.read_text()
            assert "IssueStatus" not in path.read_text()


@pytest.mark.parametrize("variant", ["public_schema_hash", "service_projection", "unconfigured_source"])
def test_commands_and_schemas_unchanged(variant):
    assert "worker_start" in SCHEMAS and "task_list" in SCHEMAS
    assert not any("dependenc" in name for name in SCHEMAS)
    if variant == "public_schema_hash":
        assert set(SCHEMAS["worker_start"]["properties"]) == {
            "task_id", "expected_version", "decision_revision", "operation_key", "host", "human_ref"
        }
        assert SCHEMAS["worker_start"]["additionalProperties"] is False
    elif variant == "service_projection":
        result = failure(SchedulingBlocked("Current dependency evidence is required"))
        assert result["error"]["category"] == "blocked" and result["exit_code"] == 4
    else:
        from ai_task_manager.domains.scheduling.models import RejectSchedulingSources
        from ai_task_manager.domains.work.errors import EvidenceRequired
        with pytest.raises(EvidenceRequired):
            RejectSchedulingSources().verify_snapshot("model-supplied")
