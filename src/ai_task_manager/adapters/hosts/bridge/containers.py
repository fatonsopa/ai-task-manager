"""Run exact reviewed fixture commands outside the credential-bearing controller."""

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, require_group_monitoring
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .container_admission import ContainerAdmissions
from .container_inspection import restrictions
from .container_plan import ContainerPlan, snapshot


@dataclass(frozen=True)
class VerifiedContainerResult:
    trace_ref: str
    attempt_id: str
    command_ref: str
    input_digest: str
    files: tuple[tuple[str, str], ...]
    result_digest: str
    expires_at: float


class DockerCommands:
    def __init__(self, journal, root, docker_executable, docker_endpoint, plans, *, deadline):
        self.journal, self.root = journal, Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise AccessDenied("Container command records require a private owned directory")
        if not docker_endpoint.startswith("unix://"):
            raise AccessDenied("Only an explicitly bound local Unix Docker endpoint is supported")
        endpoint = Path(docker_endpoint.removeprefix("unix://")).resolve()
        if not stat.S_ISSOCK(endpoint.stat().st_mode):
            raise AccessDenied("Docker endpoint is not a current local socket")
        self.docker = Path(docker_executable).resolve()
        if not self.docker.is_file():
            raise AccessDenied("Explicit installed Docker CLI is required")
        self.docker_hash = hashlib.sha256(self.docker.read_bytes()).hexdigest()
        config = self.root / "docker-config"
        config.mkdir(mode=0o700, exist_ok=True)
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(config),
            "DOCKER_CONFIG": str(config),
            "DOCKER_HOST": "unix://" + str(endpoint),
        }
        self.plans, self.deadline, self.jobs = dict(plans), deadline, {}
        self.maximum_jobs = 4
        self.admissions = ContainerAdmissions(journal, self.root)

    def _command(self, plan, pair, directory, name, owner):
        source, db, manifest = snapshot(plan, pair, directory)
        wrapper = Path(__file__).with_name("container_job.py").read_text()
        task = {
            "argv": plan.argv,
            "seconds": plan.seconds,
            "deadline": self.deadline,
            "output_bytes": plan.output_bytes,
            "file_bytes": plan.file_bytes,
            "nonce": owner,
        }
        argv = [
            str(self.docker),
            "run",
            "--pull=never",
            "--interactive",
            "--name",
            name,
            "--label",
            "aitm.owner=" + owner,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(plan.pids),
            "--memory",
            str(plan.memory_mib) + "m",
            "--memory-swap",
            str(plan.memory_mib) + "m",
            "--cpus",
            str(plan.cpus),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            f"/workspace:rw,nosuid,nodev,size={plan.workspace_mib}m,uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=2048",
            "--tmpfs",
            f"/database:rw,nosuid,nodev,size={plan.database_mib}m,uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=256",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={plan.temporary_mib}m,uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=512",
            "--mount",
            f"type=bind,src={source},dst=/input,readonly",
            "--mount",
            f"type=bind,src={db},dst=/input-db.sqlite,readonly",
            "--workdir",
            "/workspace",
            "--log-driver=json-file",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--entrypoint",
            "python",
            plan.image_id,
            "-I",
            "-u",
            "-c",
            wrapper,
            json.dumps(task, allow_nan=False),
        ]
        security = {
            "image_id": plan.image_id,
            "memory": plan.memory_mib * 1024 * 1024,
            "nano_cpus": int(plan.cpus * 1_000_000_000),
            "pids": plan.pids,
            "user": f"{os.getuid()}:{os.getgid()}",
            "tmpfs": {
                "/workspace": (
                    f"rw,nosuid,nodev,size={plan.workspace_mib}m,"
                    f"uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=2048"
                ),
                "/database": (
                    f"rw,nosuid,nodev,size={plan.database_mib}m,"
                    f"uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=256"
                ),
                "/tmp": (
                    f"rw,nosuid,nodev,size={plan.temporary_mib}m,"
                    f"uid={os.getuid()},gid={os.getgid()},mode=700,nr_inodes=512"
                ),
            },
            "mounts": {"/input": str(source), "/input-db.sqlite": str(db)},
        }
        return argv, manifest, security

    def execute_registered(self, reference, resources, boundary, effect_boundary, operation_key):
        with self.admissions.slot():
            return self._execute_registered(
                reference, resources, boundary, effect_boundary, operation_key
            )

    def _execute_registered(self, reference, resources, boundary, effect_boundary, operation_key):
        plan = self.plans.get(reference)
        if not isinstance(plan, ContainerPlan):
            raise AccessDenied("Unknown confined command reference")
        plan.check()
        boundary()
        require_group_monitoring()
        if (
            sum(path.name.startswith("aitm-fixture-") for path in self.root.iterdir())
            >= self.maximum_jobs
        ):
            raise AccessDenied("Retained command snapshots reached the finite job/disk budget")
        if hashlib.sha256(self.docker.read_bytes()).hexdigest() != self.docker_hash:
            raise AccessDenied("Installed Docker CLI changed")
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original command deadline expired")
        name = "aitm-fixture-" + secrets.token_hex(12)
        owner = secrets.token_hex(32)
        directory = self.root / name
        directory.mkdir(mode=0o700)
        argv, manifest, security = self._command(plan, resources, directory, name, owner)
        record = {
            "attempt_id": resources.attempt_id,
            "command_ref": reference,
            "name": name,
            "owner": owner,
            "deadline": self.deadline,
            "directory": str(directory),
            "input": manifest,
        }
        key = "container:" + operation_key
        if not self.journal.intend(key, record):
            raise RuntimeUncertain("Existing command must be reconciled")
        config = {
            "argv": argv,
            "docker": str(self.docker),
            "environment": self.environment,
            "name": name,
            "owner": owner,
            "deadline": min(self.deadline, self.journal.clock() + plan.seconds + 3),
            "result": str(directory / "cleanup.json"),
            "security": security,
        }
        write_private(directory / "watch.json", config)
        process = None
        group = None
        try:
            with (directory / "stdout").open("xb") as out, (directory / "stderr").open("xb") as err:
                os.chmod(out.name, 0o600)
                os.chmod(err.name, 0o600)
                # Only local process creation is inside the short Core transaction.
                with effect_boundary():
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            str(Path(__file__).with_name("container_watch.py")),
                            str(directory / "watch.json"),
                        ],
                        stdin=subprocess.PIPE,
                        stdout=out,
                        stderr=err,
                        env={"PATH": os.defpath},
                        start_new_session=True,
                    )
            group = OwnedProcessGroup(process)
            initial = group.capture()
            self.jobs[key] = {
                "group": group,
                "process": process,
                "record": record,
                "directory": directory,
            }
            self.journal._append(
                "trace",
                {
                    "effect": "container_watcher_created",
                    "intent": key,
                    "pid": process.pid,
                    "start_identity": group.start_identity,
                    "group": initial.record(),
                },
            )
            return self._wait(self.jobs[key], boundary, effect_boundary, config)
        except BaseException:
            if process is not None and process.stdin and not process.stdin.closed:
                process.stdin.close()
            if group is not None:
                job = self.jobs.get(key)
                if job is None or not getattr(job.get("stop"), "confirmed", False):
                    stopped = self._stop_watcher(group, 12)
                    if job is not None:
                        job["stop"] = stopped
            raise

    def _stop_watcher(self, group, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            status = group.leader()
            if status.known and status.exited:
                break
            time.sleep(0.02)
        stopped = group.stop()
        group.reap_exited_leader()
        return stopped

    def _wait(self, job, boundary, effect_boundary, config):
        deadline = config["deadline"]
        admitted = False
        process, group = job["process"], job["group"]
        trigger = None
        grace = None
        while True:
            status = group.leader()
            if not status.known:
                trigger = "unknown_process"
                break
            if status.exited:
                break
            if trigger is None:
                try:
                    boundary()
                    if not admitted:
                        path = job["directory"] / "stdout"
                        raw = path.read_bytes()
                        if b"\n" in raw:
                            first = json.loads(raw.split(b"\n", 1)[0])
                            if first != {"type": "sandbox_ready", "nonce": config["owner"]}:
                                raise AccessDenied("Container readiness source is invalid")
                            safe, inspection = restrictions(config)
                            self.journal._append(
                                "trace",
                                {
                                    "effect": "container_admission_check",
                                    "container": inspection,
                                    "accepted": safe,
                                    "intent": job["record"]["name"],
                                },
                            )
                            if not safe:
                                raise AccessDenied(
                                    "Actual container restrictions are unknown or different"
                                )
                            with effect_boundary():
                                process.stdin.write(
                                    (json.dumps({"nonce": config["owner"]}) + "\n").encode()
                                )
                                process.stdin.flush()
                            admitted = True
                    if self.journal.clock() >= deadline:
                        raise AccessDenied("Command deadline")
                except BaseException as error:
                    trigger = type(error).__name__
                    process.stdin.close()
                    grace = time.monotonic() + 12
            if grace is not None and time.monotonic() >= grace:
                break
            time.sleep(0.02)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        stopped = group.stop()
        group.reap_exited_leader()
        job["stop"] = stopped
        result_path = job["directory"] / "cleanup.json"
        cleanup = read_private(result_path) if result_path.exists() else None
        known = (
            stopped.confirmed
            and cleanup is not None
            and cleanup.get("owner") == job["record"]["owner"]
            and cleanup.get("watcher_pid") == process.pid
            and cleanup.get("container", {}).get("confirmed_stop") is True
        )
        stop_ref = self.journal._append(
            "trace",
            {
                "effect": "container_outcome",
                "intent": job["record"]["name"],
                "confirmed_stop": known,
                "process_stop": group.evidence(stopped, orphaned=False),
                "container": cleanup,
                "trigger": trigger,
            },
        )
        if not known:
            raise RuntimeUncertain("Complete owned container and process stop is not established")
        raw = (job["directory"] / "stdout").read_bytes()
        if len(raw) > 2097152:
            raise RuntimeUncertain("Container output exceeded the retained transport limit")
        try:
            frames = [json.loads(line) for line in raw.splitlines() if line]
            if len(frames) != 2 or frames[0] != {"type": "sandbox_ready", "nonce": config["owner"]}:
                raise ValueError("Expected actual readiness and terminal records")
            result = frames[1]
        except (ValueError, UnicodeError) as error:
            raise RuntimeUncertain("Container terminal output is missing or malformed") from error
        result.update(
            {"confirmed_stop": True, "container_removed": True, "input": job["record"]["input"]}
        )
        if cleanup.get("child_returncode") != 0 or result.get("outcome") != "succeeded" or trigger:
            raise AccessDenied(
                "Reviewed container command failed: " + str(result.get("outcome", trigger))
            )
        terminal = {
            "effect": "container_command_verified",
            "attempt_id": job["record"]["attempt_id"],
            "command_ref": job["record"]["command_ref"],
            "container_name": job["record"]["name"],
            "result": result,
        }
        job["verified_ref"] = self.journal._append(
            "trace",
            terminal,
            sources=(stop_ref,),
            expires_at=min(self.deadline, self.journal.clock() + 60),
        )
        job["verified_result"] = digest(result)
        return result

    def verified_result(self, attempt_id, command_ref, *, operation_key=None):
        """Read only one actual successful owned result; restart/ambiguity refuses."""
        matches = [
            job
            for key, job in self.jobs.items()
            if (operation_key is None or key == "container:" + operation_key)
            and job["record"]["attempt_id"] == attempt_id
            and job["record"]["command_ref"] == command_ref
            and job.get("verified_ref")
        ]
        if len(matches) != 1:
            raise AccessDenied("One actual successful registered command result is required")
        job = matches[0]
        if not job.get("stop") or not job["stop"].confirmed:
            raise AccessDenied("The owned command group is not confirmed stopped")
        stored = self.journal._read(job["verified_ref"], "trace")
        value = stored["payload"]
        result = value.get("result", {})
        if (
            value.get("effect") != "container_command_verified"
            or (value.get("attempt_id"), value.get("command_ref"), value.get("container_name"))
            != (attempt_id, command_ref, job["record"]["name"])
            or digest(result) != job["verified_result"]
            or result.get("outcome") != "succeeded"
            or result.get("confirmed_stop") is not True
            or result.get("container_removed") is not True
            or result.get("input") != job["record"]["input"]
        ):
            raise AccessDenied("The actual command result changed or is incomplete")
        return VerifiedContainerResult(
            stored["ref"],
            attempt_id,
            command_ref,
            result["input"]["digest"],
            tuple(sorted(result["input"]["files"].items())),
            digest(result),
            stored["expires_at"],
        )

    def reconcile_stopped(self, attempt_id):
        matching = [
            data
            for identity, data in self.journal._intents()
            if identity.startswith("container:") and data.get("attempt_id") == attempt_id
        ]
        for record in matching:
            job = next(
                (j for j in self.jobs.values() if j["record"]["name"] == record["name"]), None
            )
            if job is None or not getattr(job.get("stop"), "confirmed", False):
                return False
            path = job["directory"] / "cleanup.json"
            if not path.exists():
                return False
            cleanup = read_private(path)
            if (
                cleanup.get("owner") != record["owner"]
                or cleanup.get("container", {}).get("confirmed_stop") is not True
            ):
                return False
        return True
