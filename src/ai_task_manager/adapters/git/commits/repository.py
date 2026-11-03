"""Fixed local Git operations; worker requests cannot select argv or disable hooks."""

import hashlib
import os
import subprocess
import time
from pathlib import Path

from ai_task_manager.adapters.git.publication import _GATE
from ai_task_manager.adapters.process.groups import OwnedProcessGroup, _inventory, supervise
from ai_task_manager.adapters.resources.paths import confined
from ai_task_manager.domains.checks.models import fingerprint, repo_path
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.supervisor.models import RuntimeUncertain

from .output import CommitOutput


class WorkerGit:
    def __init__(self, journal, *, executable="/usr/bin/git", faults=None):
        self.journal, self.executable, self.faults = journal, executable, faults
        self.environment = {"PATH": os.defpath, "HOME": str(Path.home()), "LC_ALL": "C"}
        self.live = {}

    def query(self, path, *args):
        result = subprocess.run(
            [self.executable, "-C", str(path), *args],
            env=self.environment,
            capture_output=True,
            timeout=10,
        )
        if result.returncode or len(result.stdout) + len(result.stderr) > 1048576:
            raise EvidenceRequired("Worker Git observation is unavailable")
        return result.stdout

    def source(self, path):
        config = self.query(path, "config", "--null", "--list", "--show-origin")
        hooks = self.query(path, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
        directory = Path(hooks.decode().strip())
        entries = []
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and not entry.name.endswith(".sample"):
                    entries.append(
                        (
                            entry.name,
                            entry.stat().st_mode,
                            hashlib.sha256(entry.read_bytes()).hexdigest(),
                        )
                    )
        return fingerprint(
            {
                "config": config.hex(),
                "hooks": str(directory),
                "files": entries,
                "git": hashlib.sha256(Path(self.executable).read_bytes()).hexdigest(),
            }
        )

    def snapshot(self, path, allowed):
        raw = self.query(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        files = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            value = entry.decode("utf-8", "strict")
            if len(value) < 4 or value[2] != " " or any(c in value[:2] for c in "RCU"):
                raise AccessDenied("Ambiguous, renamed or conflicted worker changes")
            if value[0] not in {" ", "?"}:
                raise AccessDenied("Preexisting staged files require their owner's recovery")
            name = value[3:]
            repo_path(name)
            if name not in allowed:
                raise AccessDenied("Worker change exceeds its installed file scope")
            files.append(name)
        if not files:
            raise EvidenceRequired("A worker commit requires an actual scoped change")
        return {
            "files": sorted(files),
            "content": self.content(path, files),
            "git_source": self.source(path),
        }

    def content(self, path, files):
        result = {}
        for name in files:
            target = confined(Path(path), name)
            if target.exists():
                if not target.is_file() or target.stat().st_size > 1048576:
                    raise AccessDenied("Unsupported worker file type or size")
                result[name] = {
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "executable": bool(target.stat().st_mode & 0o111),
                }
            else:
                result[name] = None
        return result

    def stage(self, attempt_id, path, files, boundary, seconds, effect_boundary):
        return self._effect(
            attempt_id, "stage", path, ("add", "--", *files), boundary, seconds, effect_boundary
        )

    def whitespace(self, attempt_id, path, boundary, seconds, effect_boundary):
        return self._effect(
            attempt_id,
            "diff-check",
            path,
            ("diff", "--cached", "--check"),
            boundary,
            seconds,
            effect_boundary,
        )

    def tree(self, path):
        return self.query(path, "write-tree").decode().strip()

    def commit(self, attempt_id, path, message, boundary, seconds, effect_boundary):
        return self._effect(
            attempt_id,
            "commit",
            path,
            ("commit", "--cleanup=verbatim", "-m", message),
            boundary,
            seconds,
            effect_boundary,
        )

    def _effect(self, attempt_id, stage, path, args, boundary, seconds, effect_boundary):
        import sys

        boundary()
        self.journal.intend(attempt_id, "worker-" + stage, {"path": str(path), "args": args})
        started = time.monotonic()
        readfd, writefd = os.pipe()
        child = None
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _GATE,
                    str(readfd),
                    self.executable,
                    "-C",
                    str(path),
                    *args,
                ],
                env={
                    **self.environment,
                    "AITM_WORKER_DATABASE": self.journal.observations(
                        attempt_id, "resources_ready"
                    )[-1]["database"]["path"],
                    "AITM_WORKER_ATTEMPT": attempt_id,
                    "AITM_WORKER_TASK": self.journal.attempt(attempt_id)["task_id"],
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(readfd,),
                start_new_session=True,
            )
            os.close(readfd)
            readfd = -1
            group = OwnedProcessGroup(child)
            self.live[(attempt_id, stage)] = group
            initial = group.capture()
            directory = self.journal.path.parent / "worker-commit-output" / attempt_id
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            stdout = CommitOutput(child.stdout, directory / (stage + ".stdout"))
            stderr = CommitOutput(child.stderr, directory / (stage + ".stderr"))
            self.journal.observe(
                attempt_id,
                "worker-git-process",
                {
                    "stage": stage,
                    "pid": child.pid,
                    "birth": group.start_identity,
                    "initial": initial.record(),
                    "output": {"stdout": stdout.record(), "stderr": stderr.record()},
                },
            )
            if not initial.known:
                raise RuntimeUncertain("Worker Git process identity is uncertain")
            boundary()
            with effect_boundary():
                os.write(writefd, b"G")
            os.close(writefd)
            writefd = -1

            def watch():
                with effect_boundary():
                    pass

            result = supervise(
                group, timeout_seconds=max(0.01, min(30, seconds)), boundary_check=watch
            )
            stdout.thread.join(0.5)
            stderr.thread.join(0.5)
            result.update(
                stage=stage,
                seconds=time.monotonic() - started,
                stdout=stdout.excerpt(),
                stderr=stderr.excerpt(),
                output={"stdout": stdout.record(), "stderr": stderr.record()},
            )
            self.journal.observe(attempt_id, "worker-git-result", result)
            if not result["group"]["confirmed_stop"]:
                raise RuntimeUncertain("Worker Git descendants retain ownership")
            self.live.pop((attempt_id, stage), None)
            if self.faults:
                self.faults.hit("after:" + stage)
            if result["returncode"] != 0 or result["outcome"] != "succeeded":
                raise EvidenceRequired("Worker Git stage failed: " + stage)
            return result
        finally:
            if readfd >= 0:
                os.close(readfd)
            if writefd >= 0:
                os.close(writefd)
            # A lost reply never launches another child. Keep an unknown live owner.
            if child is not None and (attempt_id, stage) in self.live:
                group = self.live[(attempt_id, stage)]
                if group.process.returncode is None:
                    stopped = group.stop()
                    self.journal.observe(
                        attempt_id,
                        "worker-git-stop",
                        {
                            "stage": stage,
                            "quiescent": stopped.confirmed,
                            "group": stopped.observation.record(),
                        },
                    )
                    if stopped.confirmed:
                        group.reap_exited_leader()
                        self.live.pop((attempt_id, stage), None)

    def require_quiescent_commit(self, attempt_id):
        results = [
            r
            for r in self.journal.observations(attempt_id, "worker-git-result")
            if r["stage"] == "commit"
        ]
        if results:
            if results[-1]["group"]["confirmed_stop"] is True:
                return
            raise RuntimeUncertain("Worker commit descendants remain uncertain")
        processes = [
            r
            for r in self.journal.observations(attempt_id, "worker-git-process")
            if r["stage"] == "commit"
        ]
        if len(processes) != 1:
            raise RuntimeUncertain("Original worker commit child identity is unavailable")
        original = processes[0]
        inventory = _inventory()
        if (
            not inventory.known
            or not original["birth"]
            or any(
                member.live and (member.pid == original["pid"] or member.group == original["pid"])
                for member in inventory.members
            )
        ):
            raise RuntimeUncertain("Worker commit child stop cannot be reconciled")
        if not self.journal.observations(attempt_id, "worker-commit-reconciled-stop"):
            self.journal.observe(
                attempt_id,
                "worker-commit-reconciled-stop",
                {
                    "pid": original["pid"],
                    "birth": original["birth"],
                    "quiescent": True,
                },
            )

    def committed(self, path, intent):
        head = self.query(path, "rev-parse", "HEAD").decode().strip()
        if head == intent["base"]:
            return None
        parents = self.query(path, "rev-list", "--parents", "-n", "1", head).decode().split()
        tree = self.query(path, "rev-parse", head + "^{tree}").decode().strip()
        message = self.query(path, "show", "-s", "--format=%B", head).decode().rstrip("\n")
        if (
            parents != [head, intent["base"]]
            or tree != intent["tree"]
            or message != intent["message"]
        ):
            raise RuntimeUncertain("Actual worker commit differs from its saved intent")
        if self.query(path, "status", "--porcelain=v1", "--untracked-files=all"):
            raise RuntimeUncertain("Worker worktree changed after its commit")
        return head
