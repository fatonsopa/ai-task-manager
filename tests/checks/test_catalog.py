from dataclasses import replace
from importlib.util import find_spec

import pytest

from ai_task_manager.application.merge_checks.catalogs import Catalogs, ProjectCatalog
from ai_task_manager.domains.checks.catalog import validate_catalog
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from tests.checks.fixtures import capabilities, catalog_data, catalogs, private_dir, sha


@pytest.mark.parametrize(
    "mutation", ["duplicate", "resource", "required", "runner", "extra", "category"]
)
def test_actual_catalog_contract(mutation):
    data = catalog_data()
    row = data["groups"][0]
    if mutation == "duplicate":
        data["groups"][1]["labels"].append("  ALIAS A ")
    elif mutation == "resource":
        row["resources"] = ["unconfigured"]
    elif mutation == "required":
        row["required"] = 1
    elif mutation == "runner":
        row["runner"] = "unconfigured"
    elif mutation == "extra":
        row["lane"] = "plain"
    else:
        row["category"] = "unconfigured"
    with pytest.raises(ValidationError):
        validate_catalog(data, sha("catalog"), capabilities())


def test_project_adapter_uses_verified_inputs(tmp_path):
    root = private_dir(tmp_path)
    registry = catalogs(root)
    result = registry.for_project("project")
    assert result.resolve(" ALIAS A ").key == "checks a"
    assert result.resolve("checks b").category == "text-consistency"
    path = root / "project-checks.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(AccessDenied):
        registry.for_project("project")
    with pytest.raises(AccessDenied):
        Catalogs((ProjectCatalog("project", str(path), sha("wrong"), capabilities()),))


def test_optional_lane_is_not_invented():
    result = validate_catalog(catalog_data(), sha("catalog"), capabilities())
    assert result.resolve("checks a").resources == ()
    assert not hasattr(result.resolve("checks a"), "lane")


def test_hook_and_actor_scope_remain_external(tmp_path):
    result = catalogs(private_dir(tmp_path)).for_project("project")
    assert not hasattr(result, "authorize_commit")
    assert not hasattr(result, "change_task_status")
    parent = "ai_task_manager.adapters.projects.ai_index"
    assert find_spec(parent) is None or find_spec(parent + ".check_catalog") is None


@pytest.mark.parametrize(
    "mutation", ["runner_type", "mutable", "digest", "input_kind", "resource_keys", "duplicate"]
)
def test_capabilities_are_closed_typed_configuration(mutation):
    caps = capabilities()
    runner = caps.runners[0]
    if mutation == "runner_type":
        caps = replace(caps, runners=(object(),))
    elif mutation == "mutable":
        caps = replace(caps, runners=list(caps.runners))
    elif mutation == "digest":
        caps = replace(caps, runners=(replace(runner, implementation_digest="bad"),))
    elif mutation == "input_kind":
        caps = replace(caps, runners=(replace(runner, input_kinds=("invented",)),))
    elif mutation == "resource_keys":
        caps = replace(caps, resources=(replace(caps.resources[0], semantic_keys=()),))
    else:
        caps = replace(caps, runners=(runner, runner))
    with pytest.raises(ValidationError):
        validate_catalog(catalog_data(), sha("catalog"), caps)


@pytest.mark.parametrize(
    "mutation", ["omitted", "weakened", "cycle", "unknown_parent", "duplicate_resources"]
)
def test_required_policy_and_prerequisites(mutation):
    data, caps = catalog_data(), capabilities()
    if mutation == "omitted":
        caps = replace(caps, required_groups=("missing",))
    elif mutation == "weakened":
        caps = replace(caps, required_groups=("checks a",))
    elif mutation == "cycle":
        data["groups"][0]["prerequisites"] = ["checks b"]
        data["groups"][1]["prerequisites"] = ["checks a"]
    elif mutation == "unknown_parent":
        data["groups"][0]["prerequisites"] = ["missing"]
    else:
        data["groups"][0]["resources"] = ["scratch", "scratch"]
    with pytest.raises(ValidationError):
        validate_catalog(data, sha("catalog"), caps)


def test_configuration_reread_must_match_sealed_bytes(tmp_path, monkeypatch):
    from ai_task_manager.application.merge_checks import catalogs as module

    root = private_dir(tmp_path)
    catalogs(root)
    path = root / "project-checks.json"
    expected = sha(path.read_bytes())
    original = module.FileSeal

    def change_after_seal(path):
        sealed = original(path)
        sealed.path.write_text(sealed.path.read_text() + " ")
        return sealed

    monkeypatch.setattr(module, "FileSeal", change_after_seal)
    with pytest.raises(AccessDenied):
        Catalogs((ProjectCatalog("project", str(path), expected, capabilities()),))
