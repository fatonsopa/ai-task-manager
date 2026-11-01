"""Isolated owned test controller; never a public task or credential interface."""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.container_plan import ContainerPlan
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.application.core import Core
from ai_task_manager.domains.work.models import ApprovalPolicy, SessionHandle
from ai_task_manager.storage.database import Database
from ai_task_manager.supervisor.models import ResourcePair


def main():
    packet = json.loads(Path(sys.argv[1]).read_text())
    core = Core(Database(packet["core"]), ApprovalPolicy(**packet["policy"]))
    handle = SessionHandle(packet["handle"])
    pair = ResourcePair(
        **{
            **packet["pair"],
            "worktree": Path(packet["pair"]["worktree"]),
            "database": Path(packet["pair"]["database"]),
        }
    )
    journal = HostJournal(packet["journal"], packet["key"])
    plan = ContainerPlan(
        **{
            **packet["plan"],
            "argv": tuple(packet["plan"]["argv"]),
            "files": tuple(packet["plan"]["files"]),
        }
    )
    runner = DockerCommands(
        journal,
        packet["root"],
        packet["docker"],
        packet["endpoint"],
        {"registered": plan},
        deadline=packet["deadline"],
    )

    def boundary():
        return core.execution.validate_work_lease(handle, pair.attempt_id, packet["version"])

    @contextmanager
    def effect():
        with core.execution.work_boundary(handle, pair.attempt_id, packet["version"]):
            yield

    runner.execute_registered("registered", pair, boundary, effect, "controller-loss")


if __name__ == "__main__":
    main()
