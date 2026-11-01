"""Separate-process command-slot fixture with a separate SQLite connection."""

import sys

from ai_task_manager.adapters.hosts.bridge.container_admission import ContainerAdmissions
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.domains.work.errors import AccessDenied

journal = HostJournal(sys.argv[1], sys.argv[2])
try:
    with ContainerAdmissions(journal, sys.argv[3]).slot():
        raise RuntimeError("A competing process acquired the command slot")
except AccessDenied:
    print("BLOCKED", flush=True)
