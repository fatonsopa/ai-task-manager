"""Synthetic operator exercise, not a real user's business acceptance."""

import sys
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.human import HumanReceipts, OperatorChannel
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.domains.work.models import Scope

root = Path(sys.argv[1])
root.mkdir(mode=0o700)
journal = HostJournal(root / "host.sqlite", root / "key")
ref = OperatorChannel(journal).capture_instruction(
    scope=Scope("linear", "workspace", "viewer", "project", "logical"),
    task_id="fixture-task",
    target="In Review",
    revision=4,
    reason="Inspect the exact fixture artifact.",
    task_version=7,
    prose_digest="a" * 64,
    timeout=5,
)
value = HumanReceipts(journal).verify_instruction(ref)
print("SYNTHETIC_OPERATOR_ACCEPTED" if value.task_id == "fixture-task" else "ERROR", flush=True)
