"""Session establishment requires an enrolled host and a verified provider."""

import time
from dataclasses import asdict

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope, SessionHandle

from .files import private_path, read_private, write_private


class SessionFile:
    def __init__(self, path):
        self.path = path

    def save(self, handle, scope, *, replace=False):
        write_private(
            self.path,
            {"version": 1, "capability": handle.token, "scope": asdict(scope)},
            replace=replace,
        )

    def save_successor(self, handle, scope):
        """Publish a staged token whose retained stores require existing-only loads."""
        write_private(self.path, {"version": 2, "capability": handle.token, "scope": asdict(scope)})

    def load(self, core):
        data = read_private(self.path)
        if (
            set(data) != {"version", "capability", "scope"}
            or type(data["version"]) is not int
            or data["version"] not in {1, 2}
        ):
            raise AccessDenied("Unknown session capability format")
        handle = SessionHandle(data["capability"])
        if not isinstance(handle.token, str) or not 16 <= len(handle.token) <= 256:
            raise AccessDenied("Invalid session capability")
        session_id, scope = core.session_binding(handle)
        from ai_task_manager.storage.sessions import TaskSession

        if (data["version"] == 2) != isinstance(session_id, TaskSession):
            raise AccessDenied("Capability version does not match its enrolled authority")
        if asdict(scope) != data["scope"]:
            raise AccessDenied("Session capability binding changed")
        return handle


class SessionControl:
    """Trusted installer/host operation; no model-visible grant issuer exists."""

    def __init__(
        self,
        core,
        provider_client,
        provider_journal,
        enrollment_verifier,
        destination=None,
        bound_scope=None,
    ):
        self.core, self.client, self.journal = core, provider_client, provider_journal
        self.enrollments = enrollment_verifier
        self.destination = destination
        self.bound_scope = bound_scope

    def resume(self, enrollment_ref, resume_ref):
        if self.destination is None:
            raise AccessDenied("Session destination is not configured")
        grant = self.enrollments.verify_enrollment(enrollment_ref)
        if self.bound_scope is None or grant.scope != self.bound_scope:
            raise AccessDenied("Resume cannot change this terminal's logical session")
        return self.establish(enrollment_ref, self.destination, resume_ref)

    def establish(self, enrollment_ref, destination, resume_ref=None):
        grant = self.enrollments.verify_enrollment(enrollment_ref)
        if not isinstance(grant, HostGrant) or grant.expires_at <= time.time():
            raise AccessDenied("Trusted host enrollment is invalid or expired")
        binding = self.client.verify_binding()
        expected = binding.expected
        scope = Scope(
            "linear",
            expected.workspace_id,
            expected.viewer_id,
            expected.project_id,
            grant.logical_session,
        )
        try:
            self.client.check_connection(scope, refresh=True)
        except LinearError as exc:
            raise AccessDenied("Session does not match the selected agent connection") from exc
        binding = self.client.binding
        if grant.scope != scope:
            raise AccessDenied("Enrolled host does not match the verified provider identity")
        target = private_path(destination, existing=False)
        if target.exists():
            old = read_private(target)
            if resume_ref is None or old.get("scope") != asdict(scope):
                raise AccessDenied("Capability publication cannot replace another session")
        if (
            target.parent.is_symlink()
            or not target.parent.is_dir()
            or target.parent.stat().st_mode & 0o077
        ):
            raise AccessDenied("Capability publication requires an existing private directory")
        principal = self.journal.bind_principal(binding, scope)
        handle = self.core.register_session(principal, resume_ref)
        SessionFile(destination).save(handle, scope, replace=resume_ref is not None)
        return {"scope": asdict(scope), "capability_saved": True}
