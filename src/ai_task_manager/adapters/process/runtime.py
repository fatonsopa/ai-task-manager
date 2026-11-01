"""Actual waiting-worker launch and complete owned-group supervision."""

import json
import math
import os
import secrets
import selectors
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeResult, RuntimeUncertain

from .groups import OwnedProcessGroup, require_group_monitoring, supervise


@dataclass
class WaitingWorker:
    attempt_id: str
    process: subprocess.Popen
    group: OwnedProcessGroup
    nonce: str
    observation_id: str
    stdout: Path
    stderr: Path
    granted: bool = False


class ProcessRuntime:
    def __init__(self, journal, directory):
        self.journal = journal
        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workers = {}

    def launch_waiting(self, attempt_id, *, startup_timeout=2):
        require_group_monitoring()
        owner = self.journal.attempt(attempt_id)
        if self.journal.clock() >= owner["deadline"]:
            raise RuntimeRefused("Runtime deadline expired before startup")
        self.journal.intend(
            attempt_id, "launch", {"kind": "waiting_harness", "worker_id": owner["worker_id"]}
        )
        nonce = secrets.token_urlsafe(32)
        stdout, stderr = (
            self.directory / f"{attempt_id}.stdout",
            self.directory / f"{attempt_id}.stderr",
        )
        read_fd, write_fd = os.pipe()
        process = group = None
        try:
            with stdout.open("xb") as out, stderr.open("xb") as err:
                os.chmod(stdout, 0o600)
                os.chmod(stderr, 0o600)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("worker.py")),
                        str(write_fd),
                        nonce,
                        str(owner["deadline"] - self.journal.clock()),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    cwd=self.directory,
                    env={"PATH": os.defpath},
                    pass_fds=(write_fd,),
                    start_new_session=True,
                )
            os.close(write_fd)
            write_fd = -1
            group = OwnedProcessGroup(process)
            initial = group.capture()
            self.journal.observe(
                attempt_id,
                "process_created",
                {
                    "pid": process.pid,
                    "start_identity": group.start_identity,
                    "group": process.pid,
                    "worker_id": owner["worker_id"],
                    "observation": initial.record(),
                    "model_execution": "not_started",
                },
            )
            with selectors.DefaultSelector() as selected:
                selected.register(read_fd, selectors.EVENT_READ)
                if not selected.select(startup_timeout):
                    raise RuntimeUncertain("Waiting-harness startup reply timed out")
                reply = os.read(read_fd, 4097)
            data = json.loads(reply)
            if (
                len(reply) > 4096
                or data.get("type") != "harness_ready"
                or data.get("nonce") != nonce
                or data.get("pid") != process.pid
                or not initial.known
                or not group.start_identity
            ):
                raise RuntimeUncertain("Waiting-harness identity was not confirmed")
            observation = self.journal.observe(
                attempt_id,
                "harness_ready",
                {
                    "pid": process.pid,
                    "start_identity": group.start_identity,
                    "worker_id": owner["worker_id"],
                    "nonce_matched": True,
                    "runtime_stage": "waiting_harness_initialized",
                    "model_execution": "not_started",
                },
            )
            worker = WaitingWorker(attempt_id, process, group, nonce, observation, stdout, stderr)
            self.workers[attempt_id] = worker
            return worker
        except BaseException as error:
            if group is not None:
                stop = group.stop()
                group.reap_exited_leader()
                self.journal.observe(
                    attempt_id,
                    "startup_failure",
                    {
                        "reason": type(error).__name__,
                        "confirmed_stop": stop.confirmed,
                        "group": group.evidence(stop, orphaned=False),
                    },
                )
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def execute(self, worker, command, boundary_check):
        if self.workers.get(worker.attempt_id) is not worker or worker.granted:
            raise RuntimeRefused("This runtime can grant its waiting worker only once")
        if (
            not command.argv
            or not command.argv[0]
            or any(not isinstance(a, str) or "\0" in a for a in command.argv)
            or isinstance(command.timeout_seconds, bool)
            or not isinstance(command.timeout_seconds, (int, float))
            or not math.isfinite(command.timeout_seconds)
            or command.timeout_seconds <= 0
        ):
            raise RuntimeRefused("A valid structured command and finite timeout are required")
        boundary_check()
        owner = self.journal.attempt(worker.attempt_id)
        remaining = owner["deadline"] - self.journal.clock()
        if remaining <= 0:
            raise RuntimeRefused("Runtime deadline expired")
        self.journal.intend(
            worker.attempt_id,
            "work_grant",
            {
                "argv": list(command.argv),
                "cwd": str(command.cwd),
                "host": command.host,
                "runtime_stage": "command_authorized",
                "model_execution": "awaiting_host_trace"
                if command.host != "fixture"
                else "not_applicable",
            },
        )
        worker.granted = True
        assert worker.process.stdin is not None
        worker.process.stdin.write(
            (
                json.dumps(
                    {
                        "nonce": worker.nonce,
                        "argv": list(command.argv),
                        "cwd": str(command.cwd),
                        "environment": command.environment,
                        "timeout_seconds": command.timeout_seconds,
                    }
                )
                + "\n"
            ).encode()
        )
        worker.process.stdin.flush()
        try:
            completion = supervise(
                worker.group,
                timeout_seconds=min(command.timeout_seconds, remaining),
                boundary_check=boundary_check,
            )
        finally:
            worker.process.stdin.close()
        state = "done" if completion["outcome"] == "succeeded" else "failed"
        if not completion["group"]["confirmed_stop"]:
            state = "unknown"
        return self._result(worker, state, completion)

    def stop(self, worker, *, reason="stopped", state="paused"):
        stop = worker.group.stop()
        completion = {
            "group": worker.group.evidence(stop, orphaned=False),
            "returncode": worker.group.reap_exited_leader(),
            "reason": reason,
        }
        if worker.process.stdin is not None and not worker.process.stdin.closed:
            worker.process.stdin.close()
        return self._result(worker, state if stop.confirmed else "unknown", completion)

    def _result(self, worker, state, completion):
        observation = self.journal.observe(
            worker.attempt_id,
            "runtime_outcome",
            {
                "state": state,
                "quiescent": completion["group"]["confirmed_stop"],
                "worker_id": self.journal.attempt(worker.attempt_id)["worker_id"],
                "completion": completion,
                "model_success": "not_established",
            },
        )
        return RuntimeResult(
            worker.attempt_id,
            observation,
            state,
            completion["group"]["confirmed_stop"],
            completion.get("reason"),
        )

    def reconcile(self, attempt_id):
        self.journal.attempt(attempt_id)
        if attempt_id in self.workers:
            worker = self.workers[attempt_id]
            return {"state": "owned", "observation": worker.group.observe().record()}
        outcomes = self.journal.observations(attempt_id, "runtime_outcome")
        if outcomes and outcomes[-1].get("quiescent"):
            return {"state": "quiescent", "observation_id": outcomes[-1]["id"]}
        # A restarted controller has lost its waitable-child anchor. It must not signal
        # a potentially reused group or launch a second worker from a missing reply.
        self.journal.observe(
            attempt_id, "recovery_uncertain", {"reason": "owned_child_anchor_unavailable"}
        )
        return {"state": "unknown", "reason": "owned_child_anchor_unavailable"}
