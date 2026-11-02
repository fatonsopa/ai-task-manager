"""Runtime record verification has no development-controller dependency."""

import hashlib
from types import MappingProxyType

import pytest

from ai_task_manager.config.evaluation_records import EvaluationRecords
from ai_task_manager.config.files import MAX_JSON_BYTES
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


def installed(tmp_path, raw=b'{"purpose":"authored fixture"}'):
    path = tmp_path / "installed.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    ref = "evaluation_grant:" + sha
    binding = {
        "kind": "evaluation_grant",
        "run_id": "fixture-run",
        "path": str(path),
        "sha256": sha,
    }
    return EvaluationRecords({ref: binding}), ref, path, binding


def test_runtime_record_preserves_exact_purpose_run_hash_and_installed_binding(tmp_path):
    records, ref, _, binding = installed(tmp_path)
    binding["run_id"] = "foreign"
    observed = records.resolve(ref, "evaluation_grant", "fixture-run")
    assert observed.kind == "evaluation_grant"
    assert observed.run_id == "fixture-run"
    assert observed.sha256 == ref.split(":")[1]
    with pytest.raises(TypeError):
        records.bindings[ref]["run_id"] = "foreign"


@pytest.mark.parametrize("field", ["run", "purpose", "reference"])
def test_wrong_record_scope_refuses(tmp_path, field):
    records, ref, _, _ = installed(tmp_path)
    with pytest.raises(AccessDenied):
        records.resolve(
            "foreign" if field == "reference" else ref,
            "native_bootstrap_review" if field == "purpose" else "evaluation_grant",
            "foreign" if field == "run" else "fixture-run",
        )


@pytest.mark.parametrize(
    "raw",
    [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"{}" + b" " * (MAX_JSON_BYTES + 1)],
    ids=["duplicates", "nonfinite", "nonobject", "oversized"],
)
def test_strict_runtime_record_rejects_ambiguous_or_unbounded_input(tmp_path, raw):
    records, ref, _, _ = installed(tmp_path, raw)
    with pytest.raises((AccessDenied, ValidationError)):
        records.resolve(ref, "evaluation_grant", "fixture-run")


def test_hash_mode_symlink_and_authority_rebinding_refuse(tmp_path):
    records, ref, path, _ = installed(tmp_path)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AccessDenied):
        records.resolve(ref, "evaluation_grant", "fixture-run")
    records, ref, path, _ = installed(tmp_path)
    path.chmod(0o644)
    with pytest.raises(AccessDenied):
        records.resolve(ref, "evaluation_grant", "fixture-run")
    records, ref, path, _ = installed(tmp_path)
    destination = tmp_path / "other.json"
    path.rename(destination)
    path.symlink_to(destination)
    with pytest.raises(AccessDenied):
        records.resolve(ref, "evaluation_grant", "fixture-run")
    path.unlink()
    records, ref, _, _ = installed(tmp_path)
    records.bindings = MappingProxyType(dict(records.bindings))
    with pytest.raises(AccessDenied):
        records.resolve(ref, "evaluation_grant", "fixture-run")


def test_build_review_purpose_cannot_become_runtime_authority(tmp_path):
    _, ref, _, binding = installed(tmp_path)
    binding["kind"] = "batch_review"
    with pytest.raises(AccessDenied):
        EvaluationRecords({ref: binding})
