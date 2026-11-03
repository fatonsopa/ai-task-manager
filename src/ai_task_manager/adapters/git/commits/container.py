"""Finite actual-resource Git jobs using the existing host watchdog and inspection."""

import json
import os
import secrets
import stat
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge import container_watch
from ai_task_manager.adapters.hosts.bridge.container_inspection import restrictions
from ai_task_manager.adapters.process.groups import (
    OwnedProcessGroup,
    _inventory,
    require_group_monitoring,
)
from ai_task_manager.adapters.resources.paths import identity
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .private import sha


class WorkerGitContainer:
    """Explicit code-held authority; snapshot check runners cannot supply this port."""

    def __init__(
        self,
        journal,
        root,
        *,
        docker,
        docker_sha256,
        endpoint,
        image,
        resource_root,
        deadline,
        run_id,
        lifecycle=None,
    ):
        import re

        self.journal, self.root = journal, Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.resource_root = Path(resource_root).resolve()
        self.docker = Path(docker).resolve()
        socket = Path(endpoint.removeprefix("unix://")).resolve()
        if (
            self.root.resolve() != self.root
            or self.root.stat().st_mode & 0o077
            or not endpoint.startswith("unix://")
            or not stat.S_ISSOCK(socket.stat().st_mode)
            or sha(self.docker) != docker_sha256
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
            or not run_id
            or deadline <= time.time()
        ):
            raise AccessDenied("Exact private container source and finite authority required")
        self.image, self.deadline, self.run_id = image, deadline, run_id
        self.docker_sha256, self.lifecycle = docker_sha256, lifecycle
        self.resource_identity, self.socket_identity = (
            identity(self.resource_root),
            identity(socket),
        )
        self.socket = socket
        config = self.root / "docker-config"
        config.mkdir(mode=0o700, exist_ok=True)
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(config),
            "DOCKER_CONFIG": str(config),
            "DOCKER_HOST": "unix://" + str(socket),
        }
        self.wrapper = Path(__file__).with_name("container_job.py")
        self.watch = Path(container_watch.__file__)
        self.inspection = self.watch.with_name("container_inspection.py")
        self.pins = {
            str(p): sha(p) for p in (self.docker, self.wrapper, self.watch, self.inspection)
        }

    def execute(self, attempt_id, operation, record, args, boundary, seconds, effect_boundary):
        if operation not in {"stage", "commit"}:
            raise AccessDenied("Unknown fixed worker Git operation")
        if self.journal.effect(attempt_id, "worker-container:" + operation):
            raise RuntimeUncertain("Existing contained Git operation must never be replayed")
        boundary()
        require_group_monitoring()
        if time.time() >= self.deadline or seconds <= 0:
            raise AccessDenied("Original confined Git deadline expired")
        if (
            identity(self.resource_root) != self.resource_identity
            or identity(self.socket) != self.socket_identity
            or any(sha(Path(p)) != value for p, value in self.pins.items())
        ):
            raise AccessDenied("Confined Git runtime source or resource root changed")
        if len(list(self.root.glob("aitm-worker-*"))) >= 4:
            raise AccessDenied("Finite contained Git job budget exhausted")
        ready = self.journal.observations(attempt_id, "resources_ready")[-1]
        worktree, database = Path(ready["worktree"]["path"]), Path(ready["database"]["path"])
        if (
            str(worktree) != record["worktree"]
            or ready["worktree"]["common"] != record["common"]
            or not all(
                p.resolve() == p and p.is_relative_to(self.resource_root)
                for p in (worktree, database)
            )
            or identity(worktree) != ready["worktree"]["physical"]
            or identity(database) != ready["database"]["physical"]
        ):
            raise AccessDenied("Actual Git resources differ from the original owned pair")
        name, owner = "aitm-worker-" + secrets.token_hex(12), secrets.token_hex(32)
        directory = self.root / name
        directory.mkdir(mode=0o700)
        deadline = min(self.deadline, time.time() + min(30, seconds) + 3)
        labels = {
            "aitm.owner": owner,
            "aitm.task": self.journal.attempt(attempt_id)["task_id"],
            "aitm.run": self.run_id,
            "aitm.attempt": attempt_id,
        }
        task = {
            **args,
            "operation": operation,
            "worktree": str(worktree),
            "database": str(database),
            "metadata": record["metadata"],
            "attempt_id": attempt_id,
            "task_id": labels["aitm.task"],
            "nonce": owner,
            "deadline": deadline,
            "seconds": min(30, seconds),
        }
        argv, security = self._command(task, directory, name, labels, record)
        config = {
            "argv": argv,
            "docker": str(self.docker),
            "environment": self.environment,
            "name": name,
            "owner": owner,
            "labels": labels,
            "cidfile": str(directory / "container.cid"),
            "deadline": deadline,
            "result": str(directory / "cleanup.json"),
            "security": security,
        }
        intent = {
            "operation": operation,
            "attempt_id": attempt_id,
            "name": name,
            "owner": owner,
            "directory": str(directory),
            "pins": self.pins,
            "config": config,
            "database": ready["database"],
            "worktree": ready["worktree"],
        }
        self.journal.intend(attempt_id, "worker-container:" + operation, intent)
        write_private(directory / "watch.json", config)
        process, group, trigger = None, None, None
        known = False
        try:
            with (directory / "stdout").open("xb") as out, (directory / "stderr").open("xb") as err:
                os.chmod(out.name, 0o600)
                os.chmod(err.name, 0o600)
                launch = (
                    self.lifecycle.launch_boundary(operation, intent)
                    if self.lifecycle
                    else nullcontext(None)
                )
                with effect_boundary(), launch as report:
                    process = subprocess.Popen(
                        [sys.executable, "-I", str(self.watch), str(directory / "watch.json")],
                        stdin=subprocess.PIPE,
                        stdout=out,
                        stderr=err,
                        env={"PATH": os.defpath},
                        start_new_session=True,
                    )
                    if report:
                        report(process)
            group = OwnedProcessGroup(process)
            initial = group.capture()
            self.journal.observe(
                attempt_id,
                "worker-container-process",
                {
                    "operation": operation,
                    "pid": process.pid,
                    "birth": group.start_identity,
                    "initial": initial.record(),
                },
            )
            if not initial.known:
                raise RuntimeUncertain("Container watchdog identity is uncertain")
            admitted = False
            while True:
                status = group.leader()
                if not status.known:
                    raise RuntimeUncertain("Container watchdog observation became uncertain")
                if status.exited:
                    break
                boundary()
                if not admitted and b"\n" in (directory / "stdout").read_bytes():
                    first = json.loads((directory / "stdout").read_bytes().split(b"\n", 1)[0])
                    safe, observed = restrictions(config)
                    if first != {"type": "sandbox_ready", "nonce": owner} or not safe:
                        raise AccessDenied("Actual Git container admission differs")
                    self.journal.observe(
                        attempt_id,
                        "worker-container-admitted",
                        {
                            "operation": operation,
                            "inspection": observed,
                            "cid": Path(config["cidfile"]).read_text().strip(),
                        },
                    )
                    with effect_boundary():
                        process.stdin.write((json.dumps({"nonce": owner}) + "\n").encode())
                        process.stdin.flush()
                    admitted = True
                if time.time() >= deadline + 12:
                    raise RuntimeUncertain("Container watchdog exceeded its removal bound")
                time.sleep(0.02)
        except BaseException as error:
            trigger = type(error).__name__
            raise
        finally:
            if process is not None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                until = time.monotonic() + 12
                while group is not None and time.monotonic() < until:
                    status = group.leader()
                    if not status.known or status.exited:
                        break
                    time.sleep(0.02)
                if group is not None:
                    stopped = group.stop()
                    group.reap_exited_leader()
                    known = stopped.confirmed
                    self.journal.observe(
                        attempt_id,
                        "worker-container-host-stop",
                        {
                            "operation": operation,
                            "pid": process.pid,
                            "confirmed_stop": known,
                            "group": stopped.observation.record(),
                        },
                    )
                receipt = None
                try:
                    receipt = self.result(attempt_id, operation, require_success=False)
                    known = known and receipt["lifetime"]["confirmed_stop"]
                except Exception:
                    known = False
                if self.lifecycle:
                    self.lifecycle.settle(operation, intent, known, receipt, trigger)
        return self.result(attempt_id, operation)

    def _command(self, task, directory, name, labels, record):
        uid = f"{os.getuid()}:{os.getgid()}"
        tmpfs = {
            "/tmp": f"rw,nosuid,nodev,size=4m,uid={os.getuid()},gid={os.getgid()},mode=700",
            str(
                Path(task["database"]).parent
            ): f"rw,nosuid,nodev,size=8m,uid={os.getuid()},gid={os.getgid()},mode=700",
        }
        mounts = {
            record["worktree"]: [record["worktree"], "bind", True],
            record["metadata"]: [record["metadata"], "bind", True],
            task["database"]: [task["database"], "bind", True],
            record["objects"]: [record["objects"], "bind", False],
        }
        paths = [Path(p) for p in mounts]
        if len(paths) != 4 or any(
            a != b and (a.is_relative_to(b) or b.is_relative_to(a)) for a in paths for b in paths
        ):
            raise AccessDenied("Git resource mounts must have distinct physical boundaries")
        argv = [
            str(self.docker),
            "run",
            "--pull=never",
            "--interactive",
            "--name",
            name,
            "--cidfile",
            str(directory / "container.cid"),
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "0.5",
            "--user",
            uid,
        ]
        for key, value in labels.items():
            argv.extend(("--label", key + "=" + value))
        for target, options in tmpfs.items():
            argv.extend(("--tmpfs", target + ":" + options))
        for target, (source, _, writable) in mounts.items():
            argv.extend(
                (
                    "--mount",
                    f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly"),
                )
            )
        argv.extend(
            (
                "--workdir",
                task["worktree"],
                "--log-driver=json-file",
                "--log-opt",
                "max-size=1m",
                "--log-opt",
                "max-file=1",
                "--entrypoint",
                "python",
                self.image,
                "-I",
                "-u",
                "-c",
                self.wrapper.read_text(),
                json.dumps(task),
            )
        )
        security = {
            "image_id": self.image,
            "memory": 134217728,
            "nano_cpus": 500000000,
            "pids": 32,
            "user": uid,
            "tmpfs": tmpfs,
            "mounts": {},
            "mount_records": mounts,
        }
        return argv, security

    def result(self, attempt_id, operation, *, require_success=True):
        intent = self.journal.effect(attempt_id, "worker-container:" + operation)
        if intent is None:
            raise RuntimeUncertain("Contained Git intent is missing")
        directory = Path(intent["directory"])
        cleanup = read_private(directory / "cleanup.json")
        processes = [
            r
            for r in self.journal.observations(attempt_id, "worker-container-process")
            if r["operation"] == operation
        ]
        admissions = [
            r
            for r in self.journal.observations(attempt_id, "worker-container-admitted")
            if r["operation"] == operation
        ]
        stops = [
            r
            for r in self.journal.observations(attempt_id, "worker-container-host-stop")
            if r["operation"] == operation
        ]
        # Exact daemon removal is mandatory before a missing host-stop append
        # may be recovered from absence of this trusted watchdog and CLI group.
        if (
            not stops
            and processes
            and admissions
            and cleanup.get("owner") == intent["owner"]
            and cleanup.get("watcher_pid") == processes[-1]["pid"]
            and cleanup.get("container", {}).get("container_id") == admissions[-1]["cid"]
            and cleanup.get("container", {}).get("confirmed_stop") is True
        ):
            observed = _inventory()
            pid = processes[-1]["pid"]
            if observed.known and not any(m.pid == pid or m.group == pid for m in observed.members):
                self.journal.observe(
                    attempt_id,
                    "worker-container-host-stop",
                    {
                        "operation": operation,
                        "pid": pid,
                        "confirmed_stop": True,
                        "recovered": True,
                        "reason": "Exact CID removed; trusted watchdog group absent",
                    },
                )
                stops = [
                    r
                    for r in self.journal.observations(attempt_id, "worker-container-host-stop")
                    if r["operation"] == operation
                ]
        if (
            not processes
            or not admissions
            or not stops
            or not stops[-1]["confirmed_stop"]
            or cleanup.get("owner") != intent["owner"]
            or cleanup.get("name") != intent["name"]
            or cleanup.get("watcher_pid") != processes[-1]["pid"]
            or cleanup.get("container", {}).get("container_id") != admissions[-1]["cid"]
            or cleanup.get("container", {}).get("confirmed_stop") is not True
        ):
            raise RuntimeUncertain("Exact contained Git lifetime has no complete removal proof")
        frames = [json.loads(line) for line in (directory / "stdout").read_bytes().splitlines()]
        if len(frames) != 2 or frames[0] != {"type": "sandbox_ready", "nonce": intent["owner"]}:
            raise RuntimeUncertain("Contained Git terminal output is missing or ambiguous")
        result = {
            **frames[1],
            "stage": operation,
            "lifetime": {
                "kind": "docker",
                "confirmed_stop": True,
                "container_id": admissions[-1]["cid"],
                "cleanup": str(directory / "cleanup.json"),
            },
        }
        if require_success and (
            result.get("outcome") != "succeeded"
            or result.get("returncode") != 0
            or cleanup.get("child_returncode") != 0
        ):
            raise AccessDenied("Contained Git operation failed: " + operation)
        return result
