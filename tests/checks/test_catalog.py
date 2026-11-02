import hashlib
import json

import pytest

from ai_task_manager.adapters.projects.ai_index.check_catalog import load_project_inputs
from ai_task_manager.domains.checks.catalog import validate_catalog
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from tests.checks.fixtures import catalog_data, sha


@pytest.mark.parametrize("mutation", ["duplicate", "database", "switch", "cpus", "why", "lane"])
def test_actual_catalog_contract(mutation):
    data = catalog_data()
    row = data["groups"][0]
    if mutation == "duplicate":
        data["groups"][1]["labels"].append("  ALIAS A ")
    elif mutation == "database":
        row["database"] = "production"
    elif mutation == "switch":
        row["switchable"] = True
    elif mutation == "cpus":
        row["cpus"] = True
    elif mutation == "why":
        del row["why"]
    else:
        row["lane"] = "invented"
    with pytest.raises(ValidationError):
        validate_catalog(data, sha("catalog"))


def project_inputs():
    values = {
        "catalog": json.dumps(catalog_data()).encode(),
        "static_inputs": json.dumps(
            {
                "all_members_when": ["lock"],
                "members": {"types": {"inputs": ["src/**"], "whole": "Authored program"}},
            }
        ).encode(),
        "package": b'{"packageManager":"fixture@1"}',
        "lockfile": b"authored lockfile",
    }
    return values, {k: hashlib.sha256(v).hexdigest() for k, v in values.items()}


def test_project_adapter_uses_verified_inputs():
    values, expected = project_inputs()
    result = load_project_inputs(**values, expected=expected, source_pin="a" * 40)
    assert result.catalog.resolve(" ALIAS A ").key == "checks a"
    assert result.catalog.resolve("checks b").lane == "plain"
    assert result.static_members == ("types",)
    assert result.package_manager == "fixture@1"
    assert len(result.source_digest) == 64
    with pytest.raises(AccessDenied):
        load_project_inputs(
            **dict(values, lockfile=b"changed"), expected=expected, source_pin="a" * 40
        )


def test_optional_lane_is_not_invented():
    data = catalog_data()
    del data["groups"][0]["lane"]
    assert validate_catalog(data, sha("catalog")).resolve("checks a").lane is None


def test_hook_and_actor_scope_remain_external():
    values, expected = project_inputs()
    result = load_project_inputs(**values, expected=expected, source_pin="a" * 40)
    assert not hasattr(result, "authorize_commit")
    assert not hasattr(result, "change_task_status")
    # Hook selection/actor authority and physical throughput require their real integrations.
