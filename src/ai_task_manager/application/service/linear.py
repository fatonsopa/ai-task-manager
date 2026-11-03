"""Built-in Linear composition from sealed operator configuration."""

import os
import re
import shlex
import time
from pathlib import Path

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
from ai_task_manager.adapters.linear.dependencies.source import DependencySource
from ai_task_manager.adapters.linear.existing import ExistingReader
from ai_task_manager.adapters.linear.models import Credentials, ExpectedBinding
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.adapters.linear.receipts import KINDS, ProviderJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.application.existing_intake import ExistingIntake
from ai_task_manager.application.linear_sync.inbound import InboundDrain
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.linear_sync.progress import (
    ProgressApprovalContext,
    ProgressPublisher,
)
from ai_task_manager.config.environment import CREDENTIAL_ENVIRONMENT_NAME_PATTERN
from ai_task_manager.config.files import MAX_JSON_BYTES, digest, private_path
from ai_task_manager.config.sessions import SessionControl
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

DECISION_KINDS = frozenset(
    {
        "decision",
        "existing_issue_selection",
        "human_decision",
        "session_resume",
        "notification_delivered",
        "notification_ai_acknowledged",
        "notification_user_reported",
    }
)


class CredentialFile:
    def __init__(self, path, key):
        if key not in {"LINEAR_API_KEY", "LINEAR_TEST_USER_A_TOKEN", "LINEAR_TEST_USER_B_TOKEN"}:
            raise ValidationError("Unknown configured Linear credential field")
        self.path, self.key = path, key

    def __call__(self):
        path = private_path(self.path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise AccessDenied("Credential source exceeds the configured bound")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            raise AccessDenied("Configured credential cannot be parsed") from None
        matches = []
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
            if match and match[1] == self.key:
                try:
                    values = shlex.split(match[2], comments=True)
                except ValueError:
                    raise AccessDenied("Configured credential cannot be parsed") from None
                if len(values) != 1 or not values[0]:
                    raise AccessDenied("Configured credential is absent or ambiguous")
                matches.append(values[0])
        if len(matches) != 1:
            raise AccessDenied("Configured credential is absent or duplicated")
        return Credentials(matches[0])


class CredentialEnvironment:
    """Read only the name selected in trusted operator configuration."""

    def __init__(self, name):
        if (
            not isinstance(name, str)
            or re.fullmatch(CREDENTIAL_ENVIRONMENT_NAME_PATTERN, name) is None
        ):
            raise ValidationError(
                "Configured Linear environment name must use the credential namespace"
            )
        self.name = name

    def __call__(self):
        value = os.environ.get(self.name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_JSON_BYTES
            or any(not 0x21 <= ord(c) <= 0x7E for c in value)
        ):
            raise AccessDenied("Configured environment credential is absent or malformed")
        return Credentials(value)


class LinearComponents:
    def __init__(self, config, *, client_factory=None):
        file_keys = {"expected", "credential_file", "credential_key", "journal"}
        environment_keys = {"expected", "credential_environment", "journal"}
        if not isinstance(config, dict) or set(config) not in (file_keys, environment_keys):
            raise ValidationError("Unknown Linear composition configuration")
        self.configuration_hash = digest(config)
        if not isinstance(config["expected"], dict) or set(config["expected"]) != {
            "viewer_id",
            "workspace_id",
            "team_id",
            "team_key",
            "project_id",
        }:
            raise ValidationError("Complete expected Linear identity is required")
        paths = ("credential_file", "journal") if set(config) == file_keys else ("journal",)
        if any(not isinstance(config[k], str) or not Path(config[k]).is_absolute() for k in paths):
            raise ValidationError("Linear credential and journal paths must be explicitly bound")
        credentials = (
            CredentialFile(config["credential_file"], config["credential_key"])
            if set(config) == file_keys
            else CredentialEnvironment(config["credential_environment"])
        )
        expected = ExpectedBinding(**config["expected"])
        self.client = (
            client_factory(credentials, expected)
            if client_factory
            else LinearClient(HttpTransport(), credentials, expected)
        )
        self.binding = self.client.verify_binding()
        private_path(config["journal"], existing=False)
        parent = Path(config["journal"]).parent
        if not parent.is_dir() or parent.is_symlink() or parent.stat().st_mode & 0o077:
            raise AccessDenied("Linear journal requires private operator storage")
        self.journal = ProviderJournal(config["journal"])
        dependency_path = Path(config["journal"])
        self.scheduling_sources = DependencyReceipts(
            dependency_path.with_name(dependency_path.name + ".dependencies.sqlite"),
            dependency_path.with_name(dependency_path.name + ".dependencies.key"),
        )
        self.renderer = LinearMarkdownRenderer()

    def evidence(self, authority_journal):
        return EvidenceRouter(
            [
                EvidenceRoute("decision-proof:", DECISION_KINDS, authority_journal),
                EvidenceRoute("linear-proof:", KINDS, self.journal),
            ]
        )

    def check_session(self, core, handle):
        _, scope = core.session_binding(handle)
        expected = self.binding.expected
        if (scope.provider, scope.workspace, scope.principal, scope.project) != (
            "linear",
            expected.workspace_id,
            expected.viewer_id,
            expected.project_id,
        ):
            raise AccessDenied("Session does not match the authenticated Linear binding")

    def dependencies(self, core, handle):
        self.check_session(core, handle)
        return DependencySource(core, handle, self.client, self.scheduling_sources)

    def outbound(self, core, handle, decisions, writer):
        self.check_session(core, handle)
        return OutboundDrain(core, self.client, self.journal)

    def inbound(self, core, handle):
        self.check_session(core, handle)
        return InboundDrain(
            core,
            self.client,
            self.journal,
            clock=time.time,
            authorized_actors=(self.binding.expected.viewer_id,),
        )

    def existing(self, core, handle):
        self.check_session(core, handle)
        _, scope = core.session_binding(handle)
        return ExistingIntake(
            core,
            handle,
            ExistingReader(self.client, self.journal, scope, core.context.db.claim_context),
        )

    def publisher(self, core, handle, writer):
        self.check_session(core, handle)

        def create(scope, task, approvals):
            return ProgressPublisher(
                core,
                self.client,
                self.journal,
                approval_policy=core.context.approval_policy,
                approvals=approvals,
                approval_context=ProgressApprovalContext(scope, task.id, task.prose_digest),
                renderer=self.renderer,
            )

        return create

    def sessions(self, core, handle, decisions, destination):
        self.check_session(core, handle)
        _, scope = core.session_binding(handle)
        return SessionControl(
            core, self.client, self.journal, decisions.observations, destination, scope
        )
