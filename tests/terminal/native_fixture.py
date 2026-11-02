"""Disposable non-model terminal peer; never a Claude or Codex substitute."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def host():
    value = "ready"

    def draw(*_):
        size = shutil.get_terminal_size()
        text = (
            "\x1b[2J\x1b[HHOST FIXTURE\r\n" + value + f"\x1b[{size.lines};1HNATIVE BOTTOM\x1b[4;1H"
        )
        os.write(1, text.encode())

    signal.signal(signal.SIGWINCH, draw)
    draw()
    for line in sys.stdin:
        value = "HOST GOT " + line.strip()
        draw()


if __name__ == "__main__":
    if sys.argv[1] == "descendant":
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time,os,pathlib,sys; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(4)",
                sys.argv[2],
            ]
        )
        host()
    elif sys.argv[1] == "host":
        host()
    else:
        config, context = sys.argv[2:]
        until = time.monotonic() + 5
        while not Path(context).exists():
            if time.monotonic() >= until:
                raise SystemExit("owned_context_unavailable")
            time.sleep(0.01)
        from ai_task_manager.presentation.cli.main import main

        raise SystemExit(
            main(["--config", config, "--layout-context", context, "--ascii", "view", "sidebar"])
        )
