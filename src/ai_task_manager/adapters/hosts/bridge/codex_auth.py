"""Bounded native existing-status observation without storing diagnostic secrets."""

import os
import selectors
import subprocess
from dataclasses import asdict

from ai_task_manager.adapters.process.groups import (
    OwnedProcessGroup,
    require_group_monitoring,
    supervise,
)
from ai_task_manager.domains.work.errors import AccessDenied


class CodexAuthProbe:
    MAX_OUTPUT = 16384

    def __init__(self, journal, permits):
        self.journal, self.permits = journal, permits

    def observe(self, policy, binding, directory):
        settings = policy.codex_settings
        if settings is None:
            raise AccessDenied("A protected coherent Codex source plan is required")
        policy.check()
        before = settings.sources.credential_metadata()
        require_group_monitoring()
        argv = (str(policy.executable), *settings.arguments(), "login", "status")
        settings.sources.require_acl_acknowledgement()
        self.permits.authorize_auth(policy, binding)
        settings.sources.require_acl_acknowledgement()
        output = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = False
        process = subprocess.Popen(
            argv,
            cwd=policy.cwd,
            env=dict(policy.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        group = OwnedProcessGroup(process)
        group.capture()
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)

            def drain():
                nonlocal overflow
                while ready := selector.select(0):
                    for key, _ in ready:
                        part = os.read(key.fd, 4096)
                        if not part:
                            selector.unregister(key.fileobj)
                            continue
                        if sum(map(len, output.values())) + len(part) > self.MAX_OUTPUT:
                            overflow = True
                            raise AccessDenied("Existing-auth output exceeded its finite limit")
                        output[key.data].extend(part)

            def boundary():
                policy.check()
                drain()

            try:
                completion = supervise(
                    group,
                    timeout_seconds=min(10, binding.deadline - self.journal.clock()),
                    boundary_check=boundary,
                )
                try:
                    drain()
                except AccessDenied:
                    overflow = True
            finally:
                process.stdout.close()
                process.stderr.close()
        self.permits.record_auth(binding, completion)
        valid = (
            completion["outcome"] == "succeeded"
            and completion["group"]["confirmed_stop"]
            and not overflow
            and not output["stdout"]
            and bytes(output["stderr"]).rstrip(b"\r\n") == b"Logged in using ChatGPT"
        )
        counts = {name: len(value) for name, value in output.items()}
        # Neither diagnostic bytes nor their hashes become durable credential artifacts.
        output.clear()
        process_ref = self.journal._append(
            "trace",
            {
                "effect": "codex_status_process",
                "attempt_id": binding.attempt_id,
                "worker_id": binding.worker_id,
                "configuration_digest": binding.configuration_digest,
                "outcome": completion["outcome"],
                "group": completion["group"],
                "counts": counts,
            },
        )
        if not valid:
            self.journal._append(
                "trace",
                {
                    "effect": "codex_auth_refused",
                    "attempt_id": binding.attempt_id,
                    "classification": "unknown_or_failed_status",
                    "counts": counts,
                    "stop_confirmed": completion["group"]["confirmed_stop"],
                },
                sources=(process_ref,),
            )
            raise AccessDenied("Existing native subscription status is unconfirmed")
        settings.sources.check_readonly_credential(before)
        source_fingerprint = settings.fingerprint
        settings.sources.require_acl_acknowledgement()
        return self.journal._append(
            "trace",
            {
                "effect": "codex_existing_auth",
                "auth_method": "chatgpt",
                "attempt_id": binding.attempt_id,
                "worker_id": binding.worker_id,
                "scope": asdict(binding.scope),
                "core_session": binding.core_session,
                "principal_ref": binding.principal_ref,
                "deadline": binding.deadline,
                "configuration_digest": binding.configuration_digest,
                "source_fingerprint": source_fingerprint,
                "executable_hash": policy.executable_sha256,
                "uid": os.getuid(),
                "credential_metadata": before,
                "counts": counts,
                "group": completion["group"],
            },
            sources=(process_ref,),
            expires_at=min(binding.deadline, self.journal.clock() + 60),
        )

    def _validate(self, policy, binding, reference):
        policy.check()
        data = self.journal._read(reference, "trace")["payload"]
        expected = {
            "effect": "codex_existing_auth",
            "auth_method": "chatgpt",
            "attempt_id": binding.attempt_id,
            "worker_id": binding.worker_id,
            "scope": asdict(binding.scope),
            "core_session": binding.core_session,
            "principal_ref": binding.principal_ref,
            "deadline": binding.deadline,
            "configuration_digest": binding.configuration_digest,
            "source_fingerprint": policy.codex_settings.fingerprint,
            "executable_hash": policy.executable_sha256,
            "uid": os.getuid(),
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise AccessDenied("Existing-auth receipt belongs to another launch")
        before = policy.codex_settings.sources.credential_metadata()
        from ai_task_manager.config.files import digest

        if digest(before) != digest(data.get("credential_metadata")):
            raise AccessDenied("Native credential metadata changed before forced launch")
        policy.codex_settings.sources.require_acl_acknowledgement()
        return expected

    def consume(self, policy, binding, reference):
        expected = self._validate(policy, binding, reference)
        if not self.journal.intend("codex-forced-launch:" + reference, expected):
            raise AccessDenied("Existing-auth receipt already consumed its one launch")

    def check_launch(self, policy, binding, reference):
        expected = self._validate(policy, binding, reference)
        if self.journal.intent("codex-forced-launch:" + reference) != expected:
            raise AccessDenied("The forced native launch has no matching consumed preflight")
