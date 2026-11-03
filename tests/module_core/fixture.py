"""Supported setup against the existing loopback provider; no production observations."""

import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import write_private
from ai_task_manager.installation.sessions import setup
from tests.linear.helpers import CONNECTION, EXPECTED, ProviderFixture


class Demonstration:
    def __init__(self, root, database, writer_root):
        self.root = Path(root).resolve()
        if not self.root.is_dir() or any(self.root.iterdir()):
            raise ValueError("Demonstration requires its own existing empty directory")
        self.root.chmod(0o700)
        database = Path(database).resolve()
        if not database.is_file() or database.stat().st_size != 0:
            raise ValueError("An explicitly owned empty disposable database is required")
        self.provider = ProviderFixture()
        self.endpoint = self.provider.endpoint
        self.config = self.root / "service.json"
        package = WriterPackage.capture(writer_root, sys.executable)
        credential = self.root / "credential.env"
        credential.write_text("LINEAR_API_KEY=authored-local-contract-only\n")
        credential.chmod(0o600)
        linear = {
            "expected": asdict(EXPECTED),
            "agent_connection": asdict(CONNECTION),
            "credential_file": str(credential),
            "credential_key": "LINEAR_API_KEY",
            "journal": str(self.root / "provider.sqlite"),
        }
        config = {
            "version": 1,
            "core_database": str(database),
            "authority_database": str(self.root / "authority.sqlite"),
            "authority_key": str(self.root / "authority.key"),
            "session_file": str(self.root / "session.json"),
            "approval_policy": asdict(package.policy()),
            "linear": linear,
            "writer": {
                "root": str(package.root),
                "python": package.python,
                "pins": package.pins,
                "sessions_root": str(self.root / "writing"),
                "journal": str(self.root / "writing.sqlite"),
                "key": str(self.root / "writing.key"),
            },
        }
        write_private(self.config, config)
        self.components = LinearComponents(
            linear,
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(_test_endpoint=self.endpoint),
                credentials,
                expected,
            ),
        )
        try:
            self.setup = setup(
                self.config,
                logical_session=CONNECTION.logical_session,
                actions=(
                    "enqueue",
                    "dequeue",
                    "hold",
                    "start",
                    "request_review",
                    "non_code_complete",
                ),
                deadline=time.time() + 1800,
                operation_key="module-core-setup",
                components=self.components,
            )
        except BaseException:
            self.close()
            raise

    def close(self):
        self.provider.close()
        if self.provider.thread.is_alive():
            raise RuntimeError("Owned loopback fixture did not stop")


# The endpoint override is a declared transport fixture seam, not an offline
# product option. The installed CLI and its default load_service remain intact.
CLI = """import sys
from urllib.parse import urlparse
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.presentation.cli.main import main
endpoint=sys.argv.pop(1)
mode=sys.argv.pop(1)
if urlparse(endpoint).hostname != '127.0.0.1': raise RuntimeError('Loopback fixture only')
HttpTransport.ENDPOINT=endpoint
if mode == 'drop-reply':
 import os
 class LostReply:
  def write(self, value): os._exit(70)
  def flush(self): pass
 sys.stdout=LostReply()
raise SystemExit(main())
"""
