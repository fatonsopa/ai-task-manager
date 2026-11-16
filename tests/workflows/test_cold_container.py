"""Original watchdog cleanup is signed and checked; Docker is an authored CLI control."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge import container_watch
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.installation.workflows.cold import ColdRecovery


def test_original_watchdog_cleanup_survives_lost_controller_reply(tmp_path):
    root = tmp_path.resolve()
    docker = root / "authored-docker"
    present = root / "present"
    present.touch(mode=0o600)
    docker.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json,os,sys
from pathlib import Path
p=Path(os.environ["AUTHORED_CONTAINER_STATE"])
identity="a"*64
if sys.argv[1]=="inspect":
    if p.exists():
        print(json.dumps([{"Name":"/owned-watch", "Id":identity,
                          "Config":{"Labels":{"aitm.owner":"unit-owner"}}}]))
    else:
        print("Error: No such object: " + identity, file=sys.stderr)
        raise SystemExit(1)
elif sys.argv[1]=="rm":
    p.unlink()
    print(identity)
else:
    raise SystemExit(2)
"""
    )
    docker.chmod(0o700)
    plan = root / "watch.json"
    write_private(
        plan,
        {
            "argv": [sys.executable, "-I", "-c", "import time;time.sleep(5)"],
            "docker": str(docker),
            "name": "owned-watch",
            "owner": "unit-owner",
            "environment": {"PATH": os.defpath, "AUTHORED_CONTAINER_STATE": str(present)},
            "deadline": time.time() + 8,
            "result": str(root / "cleanup.json"),
            "recovery_key": "f" * 64,
        },
    )
    journal = HostJournal(root / "host.sqlite", root / "host.key")
    intent = {
        "owner": "unit-owner",
        "name": "owned-watch",
        "directory": str(root),
        "recovery_key": "f" * 64,
    }
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(container_watch.__file__)), str(plan)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    group = OwnedProcessGroup(process)
    group.capture()
    trace = {
        "effect": "container_watcher_created",
        "intent": "container:owned",
        "pid": process.pid,
        "start_identity": group.start_identity,
        "watch": file_binding(plan),
    }
    try:
        process.stdin.close()
        process.wait(timeout=8)
        cleanup = read_private(root / "cleanup.json")
        assert cleanup["container"]["confirmed_stop"] and cleanup["recovery_mac"]
        recovery = ColdRecovery.__new__(ColdRecovery)
        recovery.reference = "authored-current-stop-capability"
        recovery.check = lambda: None
        recovery._container_stop(journal, "container:owned", intent, [trace])
        proof = journal._history("trace")[-1]["payload"]
        assert proof["effect"] == "container_cold_stop" and proof["confirmed_stop"]
        # A changed cleanup body cannot borrow the original process/signature.
        cleanup["owner"] = "foreign"
        write_private(root / "cleanup.json", cleanup, replace=True)
        with pytest.raises(EvidenceRequired):
            recovery._container_stop(journal, "container:owned", intent, [trace])
        assert len(journal._history("trace")) == 1
    finally:
        if process.poll() is None:
            assert group.stop().confirmed
            process.wait(timeout=2)
