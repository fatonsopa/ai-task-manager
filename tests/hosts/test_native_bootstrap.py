"""Actual owned protocol controls, not real Claude/model conformance evidence."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.admission import NativeAdmissionVerifier
from ai_task_manager.adapters.hosts.bridge.bootstrap import BootstrapPermits
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph


def test_diagnostic_observes_owned_startup_without_core_work_authority(bootstrap_graph):
    item = bootstrap_graph()
    ref = item.bootstrap.start()
    observed = item.observations.verify(ref)
    assert observed["payload"]["model"] == "fixture-requested-model"
    assert observed["payload"]["requested_effort"] == "high"
    assert observed["payload"]["observed_effort"] is None
    assert observed["payload"]["full_workflow_conformance"] == "not_established"
    assert item.native.core is None and item.native.gateway.delegate is None
    with pytest.raises(AccessDenied):
        item.native.gateway.call("read_file", {"path": "unassigned.txt"})
    assert not (item.policy.cwd / "unassigned.txt").exists()
    with pytest.raises(AccessDenied):
        item.native.permits.check_admission("claude")


def test_diagnostic_cannot_restart_or_renew_its_original_receipt(bootstrap_graph):
    item = bootstrap_graph()
    item.bootstrap.start()
    with pytest.raises(AccessDenied):
        item.bootstrap.start()
    with pytest.raises(AccessDenied):
        item.observations.capture(item.native, item.native.startup.ready_ref)


@pytest.mark.parametrize("mode", ["native-tool", "fallback"])
def test_actual_protocol_refusal_never_becomes_diagnostic_readiness(bootstrap_graph, mode):
    item = bootstrap_graph(mode)
    with pytest.raises((AccessDenied, RuntimeUncertain, ExceptionGroup)):
        item.bootstrap.start()
    assert item.observations._observed == {}


def test_plain_observation_reference_and_permissive_grant_cannot_admit_worker(bootstrap_graph):
    item = bootstrap_graph()
    with pytest.raises(AccessDenied):
        item.observations.verify("host-record:authored-data-only")
    ref = item.bootstrap.start()
    with pytest.raises(AccessDenied):
        NativeAdmissionVerifier(
            item.observations, ref, SimpleNamespace(require_worker=lambda *_: True)
        )


def test_actual_native_stop_invalidates_diagnostic_readiness(bootstrap_graph):
    item = bootstrap_graph()
    ref = item.bootstrap.start()
    assert item.bootstrap.stop().confirmed
    with pytest.raises((AccessDenied, RuntimeUncertain)):
        item.observations.verify(ref)


@pytest.mark.parametrize("field", ["model", "effort", "environment"])
def test_launch_template_rejects_actual_profile_drift(bootstrap_graph, field):
    item = bootstrap_graph()
    changes = {
        "model": "foreign",
        "effort": "low",
        "environment": {**item.policy.environment, "ANTHROPIC_API_KEY": "authored-no-secret"},
    }
    with pytest.raises(AccessDenied):
        item.template.check(replace(item.policy, **{field: changes[field]}))


def test_source_change_and_template_rebinding_refuse(bootstrap_graph):
    item = bootstrap_graph()
    item.template._value["model"] = "foreign"
    with pytest.raises(AccessDenied):
        item.template.check(item.policy)
    item = bootstrap_graph()
    item.policy.executable.write_text(item.policy.executable.read_text() + "\n# changed\n")
    with pytest.raises(AccessDenied):
        item.template.check(item.policy)


def test_bootstrap_permits_reject_unregistered_and_unreviewed_authority(bootstrap_graph):
    item = bootstrap_graph()
    with pytest.raises(AccessDenied):
        BootstrapPermits("data", "data", "data", lambda: True, item.journal)
    permits = item.native.permits
    permits.authorization = SimpleNamespace(require_bootstrap=lambda *_: True)
    with pytest.raises(AccessDenied):
        permits.authorize_initialization(item.policy, item.binding)
