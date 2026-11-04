"""Observe the explicit native Claude profile using its real local status command."""

import os
import subprocess

from ai_task_manager.adapters.process.groups import (
    OwnedProcessGroup,
    require_group_monitoring,
    supervise,
)
from ai_task_manager.config.files import digest, strict_json
from ai_task_manager.domains.work.errors import AccessDenied

from .invocations import require_invocation, spawn_claimed

AUTH_OBSERVATION_MAX_SECONDS = 15


def claude_account_hash(status):
    return digest([status.get(k) for k in ("email", "orgId", "userId")])


class ClaudeAuthProbe:
    def __init__(self, journal, permits):
        self.journal, self.permits = journal, permits

    def observe(self, policy, binding, directory):
        policy.check()
        permit = require_invocation(
            self.permits.authorize_auth(policy, binding), policy, binding, "auth"
        )
        end = self.journal.clock() + permit.bounded_timeout(
            AUTH_OBSERVATION_MAX_SECONDS,
            original_deadline=binding.deadline,
            clock=self.journal.clock,
        )
        require_group_monitoring()
        stdout, stderr = directory / "auth.stdout", directory / "auth.stderr"
        with stdout.open("xb") as out, stderr.open("xb") as err:
            os.chmod(stdout, 0o600)
            os.chmod(stderr, 0o600)
            process = spawn_claimed(
                permit,
                [str(policy.executable), "auth", "status"],
                cwd=policy.cwd,
                env=dict(policy.environment),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        group = OwnedProcessGroup(process)
        group.capture()
        completion = supervise(
            group,
            timeout_seconds=max(0, end - self.journal.clock()),
        )
        self.permits.record_auth(binding, completion)
        if completion.get("outcome") != "succeeded" or not completion["group"]["confirmed_stop"]:
            raise AccessDenied("Actual intended-profile authentication preflight failed")
        data = strict_json(stdout.read_bytes())
        if (
            not isinstance(data, dict)
            or data.get("loggedIn") is not True
            or data.get("authMethod") != "claude.ai"
            or data.get("apiProvider") != "firstParty"
            or data.get("configDirectory") != policy.environment["CLAUDE_CONFIG_DIR"]
        ):
            raise AccessDenied("Native profile did not authenticate with the approved subscription")
        return self.journal._append(
            "trace",
            {
                "auth_method": "claude.ai",
                "account_hash": claude_account_hash(data),
                "configuration_digest": binding.configuration_digest,
                "stdout_hash": digest(data),
                "owned_group": completion["group"],
            },
            expires_at=min(binding.deadline, self.journal.clock() + 60),
        )
