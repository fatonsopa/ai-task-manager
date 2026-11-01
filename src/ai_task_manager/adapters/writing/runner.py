"""Run only a verified validator with structured arguments and retained attempts."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

from ai_task_manager.config.files import strict_json, write_private
from ai_task_manager.domains.work.errors import ApprovalRequired


class WriterRunner:
    def __init__(self, package, *, timeout=30):
        self.package, self.timeout = package, timeout

    def run(self, workdir, command, *arguments):
        self.package.verify()
        workdir = Path(workdir)
        attempt = workdir / ("invocation-" + str(uuid4()) + ".json")
        argv = [
            self.package.python,
            "-E",
            "-s",
            str(self.package.root / "scripts/validate_document.py"),
            command,
            *map(str, arguments),
        ]
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        try:
            result = subprocess.run(
                argv, cwd=workdir, env=env, capture_output=True, timeout=self.timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            write_private(
                attempt, {"command": command, "outcome": "failed", "reason": type(exc).__name__}
            )
            raise ApprovalRequired(
                "Writer execution failed; the attempted invocation was retained"
            ) from exc
        try:
            payload = strict_json(result.stdout)
        except Exception as exc:
            write_private(
                attempt,
                {
                    "command": command,
                    "returncode": result.returncode,
                    "outcome": "failed",
                    "reason": "invalid_validator_output",
                },
            )
            raise ApprovalRequired("Writer returned invalid output") from exc
        write_private(
            attempt,
            {
                "command": command,
                "returncode": result.returncode,
                "result": payload,
                "outcome": "succeeded" if result.returncode == 0 else "failed",
            },
        )
        if result.returncode:
            raise ApprovalRequired(
                "Writer validation refused the document; retained evidence describes the attempt"
            )
        return payload
