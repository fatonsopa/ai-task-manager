"""One command slot per configured runner, with durable unresolved-work fencing."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


class ContainerAdmissions:
    def __init__(self, journal, root):
        self.journal, self.root = journal, Path(root).absolute()

    @contextmanager
    def slot(self):
        fd = os.open(self.root / "command.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AccessDenied("Another controller owns the finite command slot") from error
            completed = {
                row["payload"]["intent"]
                for row in self.journal._history("trace")
                if row["payload"].get("effect") == "container_outcome"
                and row["payload"].get("confirmed_stop") is True
            }
            for identity, data in self.journal._intents():
                if (
                    identity.startswith("container:")
                    and Path(data["directory"]).parent == self.root
                    and data["name"] not in completed
                ):
                    raise RuntimeUncertain("A durable unresolved container intent retains the slot")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
