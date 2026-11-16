"""Fixed installed child entrypoint for a signed original-config replacement."""

import signal
import sys
from threading import Event

from ai_task_manager.installation.workflows import launch
from ai_task_manager.installation.workflows.cold import ColdRecovery


def main():
    if len(sys.argv) != 4:
        raise ValueError("Original configuration, installation and recovery reference required")
    config, installation, reference = sys.argv[1:]
    recovery = ColdRecovery(config, installation, reference)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    launch(config, installation, recovery=recovery, stop=stop)


if __name__ == "__main__":
    main()
