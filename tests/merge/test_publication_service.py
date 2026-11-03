from dataclasses import fields, replace

import pytest
from unit_fixtures import Harness

from ai_task_manager.application.merging.service import MergeService
from ai_task_manager.domains.merging.contracts import Binding, encode
from ai_task_manager.domains.work.errors import AccessDenied, WorkError


@pytest.fixture
def h(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return Harness(private / "merge.sqlite")


@pytest.mark.parametrize("field", [f.name for f in fields(Binding)])
@pytest.mark.parametrize("mode", ["missing", "foreign"])
def test_exact_task_and_configuration_binding(h, field, mode):
    value = "" if mode == "missing" else "f" * 64 if field.endswith("_digest") else "foreign"
    before = h.snapshot()
    with pytest.raises(WorkError):
        h.service.register(replace(h.binding, **{field: value}))
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "change",
    [
        "missing_observer",
        "missing_authority",
        "wrong_ref",
        "wrong_binding",
        "wrong_kind",
        "wrong_head",
        "false_permission",
        "wrong_source",
        "missing_contract",
        "extra_field",
        "raw_boolean",
        "revoked",
        "before_intent",
    ],
)
def test_checks_and_authority_omissions_refuse_without_effect(h, change):
    before = h.snapshot()
    fact = h.checks.fact
    payload = {
        "head": h.request.new,
        "permitted": True,
        "contract_digest": "f" * 64,
        "source_digest": h.binding.source_digest,
    }
    if change == "missing_observer":
        h.service.checks = None
    elif change == "missing_authority":
        h.service.authority = None
    elif change in {"wrong_ref", "wrong_binding", "wrong_kind"}:
        key = {
            "wrong_ref": "operation_ref",
            "wrong_binding": "binding_digest",
            "wrong_kind": "kind",
        }[change]
        h.checks.fact = replace(fact, **{key: "foreign"})
    elif change == "raw_boolean":
        h.checks.fact = True
    elif change == "revoked":
        h.authority.active = False
    elif change == "before_intent":
        with pytest.raises(AccessDenied):
            with h.service.authority_boundary(h.ref):
                raise AssertionError("No premature work")
        assert h.snapshot() == before
        return
    else:
        if change == "wrong_head":
            payload["head"] = "1" * 40
        elif change == "false_permission":
            payload["permitted"] = False
        elif change == "wrong_source":
            payload["source_digest"] = "0" * 64
        elif change == "missing_contract":
            payload.pop("contract_digest")
        elif change == "extra_field":
            payload["human_accepted"] = True
        h.checks.fact = replace(fact, payload_json=encode(payload))
    with pytest.raises(WorkError):
        h.prepare()
    assert h.snapshot() == before and h.authority.entries == 0


def test_valid_boundary_rechecks_receipt_before_physical_effect(h):
    h.prepare()
    h.checks.fact = None
    before = h.snapshot()
    with pytest.raises(AccessDenied):
        with h.service.authority_boundary(h.ref):
            raise AssertionError("Stale checks must not permit effect")
    assert h.snapshot() == before and h.authority.entries == 0


def test_absent_transport_and_old_lifecycle_surfaces_refuse(h):
    service = MergeService(h.journal, h.authority, h.checks)
    with pytest.raises(AccessDenied):
        service.publish(h.ref, 0, "publish", h.request, "checked")
    assert not any(
        hasattr(service, name)
        for name in [
            "execute_registered",
            "model_ended",
            "request_stop",
            "complete_task",
            "set_status",
        ]
    )
    assert h.journal.current(h.ref)["facts"] == {}
