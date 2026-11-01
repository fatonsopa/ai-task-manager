"""Actual confined interruption cases; no provider or model invocation."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.container_inspection import cleanup, docker
from ai_task_manager.adapters.hosts.bridge.container_plan import ContainerPlan
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.test_containers import DOCKER, IMAGE
from tests.hosts.test_containers import command_fixture as command_fixture

RESISTANT = (
    "import signal,subprocess,sys,time\n"
    "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
    "subprocess.Popen([sys.executable,'-I','-c',"
    "'import signal,time;HOST_BRIDGE_RESISTANT_CHILD=1;"
    "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\n"
    "time.sleep(30)\n"
)


def resistant_plan(gateway, seconds=8):
    gateway.call(
        "write_file",
        {
            "path": "resistant.py",
            "text": RESISTANT,
            "expected_hash": None,
            "operation_key": "resistant-fixture",
        },
    )
    return ContainerPlan(
        IMAGE, ("python", "-I", "resistant.py"), ("resistant.py",), seconds=seconds
    )


def await_descendant(journal, root, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for path in root.glob("aitm-fixture-*/watch.json"):
            plan = json.loads(path.read_text())
            rows = journal._history("trace")
            if any(
                r["payload"].get("effect") == "container_admission_check"
                and r["payload"].get("accepted") is True
                for r in rows
            ):
                top = docker(plan, "top", plan["name"], "-eo", "pid,ppid,args")
                if top["returncode"] == 0 and "HOST_BRIDGE_RESISTANT_CHILD=1" in top["stdout"]:
                    return plan
        time.sleep(0.03)
    raise AssertionError("The actual resistant descendant did not start within the frozen bound")


@DOCKER
def test_hold_stops_actual_resistant_container_without_releasing_unknown_ownership(
    command_fixture, journal
):
    r, dispatch, gateway, boundary, effect, create, _ = command_fixture
    runner = create({"resistant": resistant_plan(gateway)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(
            runner.execute_registered,
            "resistant",
            dispatch.resources,
            boundary,
            effect,
            "hold-case",
        )
        await_descendant(journal, runner.root)
        r.hold(dispatch)
        with pytest.raises((AccessDenied, RuntimeUncertain)):
            call.result(timeout=15)
    assert runner.reconcile_stopped(dispatch.attempt_id)
    outcome = journal._history("trace")[-1]["payload"]
    assert outcome["effect"] == "container_outcome" and outcome["confirmed_stop"] is True
    assert dispatch.resources.database.exists() and dispatch.resources.worktree.exists()


@DOCKER
def test_original_deadline_stops_actual_resistant_descendant(command_fixture, journal):
    _, dispatch, gateway, boundary, effect, create, _ = command_fixture
    runner = create({"resistant": resistant_plan(gateway, seconds=2)})
    original = runner.deadline
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(
            runner.execute_registered,
            "resistant",
            dispatch.resources,
            boundary,
            effect,
            "deadline-case",
        )
        await_descendant(journal, runner.root)
        with pytest.raises((AccessDenied, RuntimeUncertain)):
            call.result(timeout=15)
    assert runner.deadline == original and runner.reconcile_stopped(dispatch.attempt_id)
    job = next(iter(runner.jobs.values()))
    result = json.loads((job["directory"] / "cleanup.json").read_text())
    assert result["container"]["confirmed_stop"] is True
    frames = [json.loads(line) for line in (job["directory"] / "stdout").read_text().splitlines()]
    assert frames[-1]["outcome"] == "deadline"


@DOCKER
def test_controller_death_keeps_watchdog_until_exact_container_cleanup_and_fences_restart(
    command_fixture, journal
):
    r, dispatch, gateway, _, _, create, evidence = command_fixture
    plan = resistant_plan(gateway)
    runner = create({"registered": plan})
    current = r.rig.core.get_attempt(r.rig.handle, dispatch.attempt_id)
    packet = {
        "core": r.rig.db.path,
        "policy": asdict(r.rig.policy),
        "handle": r.rig.handle.token,
        "pair": {
            k: str(v) if isinstance(v, Path) else v for k, v in asdict(dispatch.resources).items()
        },
        "journal": str(journal.path),
        "key": str(journal.key_path),
        "root": str(runner.root),
        "docker": str(runner.docker),
        "endpoint": runner.environment["DOCKER_HOST"],
        "plan": asdict(plan),
        "deadline": runner.deadline,
        "version": current.version,
    }
    packet_path = evidence / "controller-private.json"
    write_private(packet_path, packet)
    controller = None
    group = None
    watch = None
    try:
        with (
            (evidence / "controller.stdout").open("wb") as out,
            (evidence / "controller.stderr").open("wb") as err,
        ):
            controller = subprocess.Popen(
                [sys.executable, "-m", "tests.hosts.container_controller", str(packet_path)],
                env={"PATH": os.defpath, "PYTHONPATH": str(Path("src").resolve())},
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        group = OwnedProcessGroup(controller)
        group.capture()
        watch = await_descendant(journal, runner.root)
        controller.kill()
        group.stop()
        group.reap_exited_leader()
        end = time.monotonic() + 15
        result = Path(watch["result"])
        while not result.exists() and time.monotonic() < end:
            time.sleep(0.03)
        assert result.exists(), "Owned watchdog must survive controller death through cleanup"
        stopped = json.loads(result.read_text())
        assert stopped["trigger"] == "controller_disconnected"
        assert stopped["container"]["confirmed_stop"] is True
        restarted = DockerCommands(
            journal,
            runner.root,
            runner.docker,
            runner.environment["DOCKER_HOST"],
            {"registered": plan},
            deadline=runner.deadline,
        )
        assert restarted.reconcile_stopped(dispatch.attempt_id) is False
        with pytest.raises(RuntimeUncertain, match="unresolved"):
            restarted.execute_registered(
                "registered", dispatch.resources, lambda: None, lambda: None, "never-relaunch"
            )
        assert dispatch.resources.database.exists() and dispatch.resources.worktree.exists()
    finally:
        if controller is not None and group is not None and controller.returncode is None:
            group.stop()
            group.reap_exited_leader()
        if watch is not None and not Path(watch["result"]).exists():
            write_private(evidence / "emergency-owned-cleanup.json", cleanup(watch))
