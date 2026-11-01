"""Actual local provider verification; trusted enrollment controls, no host enrollment claim."""

from dataclasses import replace

import pytest

from ai_task_manager.config.sessions import SessionControl, SessionFile
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired


def test_a33_verified_establishment_and_one_use_resume(composed, tmp_path):
    service, _, _, components = composed
    observations = service.decisions.observations
    scope = replace(service.scope, logical_session="another-trusted-terminal")
    observations.grants["second-enrollment"] = replace(
        observations.grants["enrollment"],
        scope=scope,
        logical_session=scope.logical_session,
        actions=("resume",),
    )
    destination = tmp_path / "second-session.json"
    control = SessionControl(
        service.core,
        components.client,
        components.journal,
        observations,
        destination,
        bound_scope=scope,
    )
    result = control.establish("second-enrollment", destination)
    assert result == {"scope": vars(scope), "capability_saved": True}
    old = SessionFile(destination).load(service.core)
    session_id, actual = service.core.session_binding(old)
    assert actual == scope and session_id != service.session_id
    proof = service.decisions.resume(scope, session_id, "second-enrollment")
    assert control.resume("second-enrollment", proof)["capability_saved"]
    current = SessionFile(destination).load(service.core)
    assert current != old and service.core.session_binding(current) == (session_id, scope)
    with pytest.raises(AccessDenied):
        service.core.session_binding(old)
    with pytest.raises(EvidenceRequired):
        control.resume("second-enrollment", proof)
    assert SessionFile(destination).load(service.core) == current
    assert service.core.session_binding(service.handle)[1] == service.scope


def test_a33_resume_cannot_switch_terminal_or_overwrite_foreign_file(composed, tmp_path):
    service, _, _, components = composed
    observations = service.decisions.observations
    foreign = replace(service.scope, logical_session="foreign")
    observations.grants["foreign"] = replace(
        observations.grants["enrollment"],
        scope=foreign,
        logical_session=foreign.logical_session,
        actions=("resume",),
    )
    response = service.call("session_resume", {"enrollment_ref": "foreign", "resume_ref": "any"})
    assert response["outcome"] == "denied"
    assert service.core.session_binding(service.handle)[1] == service.scope
    destination = tmp_path / "existing-capability.json"
    SessionFile(destination).save(service.handle, service.scope)
    before = destination.read_bytes()
    control = SessionControl(service.core, components.client, components.journal, observations)
    with pytest.raises(AccessDenied):
        control.establish("foreign", destination)
    assert destination.read_bytes() == before
    observations.grants["mismatch"] = replace(
        observations.grants["foreign"], scope=replace(foreign, principal="unverified-principal")
    )
    with pytest.raises(AccessDenied):
        control.establish("mismatch", tmp_path / "mismatch.json")
    assert not (tmp_path / "mismatch.json").exists()
