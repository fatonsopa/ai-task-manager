"""Durable gated named Git children; no arbitrary public command execution."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.git.publication import _GATE, _Drain
from ai_task_manager.adapters.process.groups import OwnedProcessGroup, supervise
from ai_task_manager.domains.work.errors import AccessDenied


class CandidateProcesses:
    def __init__(self, store, profile, boundary, *, faults=None):
        self.store, self.profile, self.boundary, self.faults = store, profile, boundary, faults
        self.live = {}

    def fault(self, stage):
        if self.faults:
            self.faults.hit(stage)

    def run(self, ref, name, path, args):
        generation = str(uuid4())
        argv = self.profile.argv(path, *args)
        marker = "candidate:" + self.store.get(ref)["generation"]
        env = self.profile.environment(path, marker)
        self.store.effect(ref, name, {"argv": argv, "generation": generation, "path": str(path)})
        self.fault("before:" + name)
        readfd, writefd = os.pipe()
        child = None
        observed = False
        try:
            process = subprocess.Popen(
                [str(Path(sys.executable).resolve()), "-I", "-S", "-c", _GATE, str(readfd), *argv],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(readfd,),
                start_new_session=True,
            )
            group = OwnedProcessGroup(process)
            child = {
                "process": process,
                "group": group,
                "stdout": None,
                "stderr": None,
                "ref": ref,
                "name": name,
            }
            self.live[(ref, name)] = child
            os.close(readfd)
            readfd = -1
            initial = group.capture()
            child["stdout"] = _Drain(process.stdout)
            child["stderr"] = _Drain(process.stderr)
            self.store.observe(
                ref,
                name,
                "process",
                {
                    "pid": process.pid,
                    "group": group.group_id,
                    "birth": group.start_identity,
                    "initial": initial.record(),
                    "generation": generation,
                },
            )
            if not initial.known:
                raise AccessDenied("Git child identity is unavailable")
            self.fault("gated:" + name)
            self.store.owner(ref)
            with self.boundary(ref):
                os.write(writefd, b"G")
            os.close(writefd)
            writefd = -1
            self.fault("released:" + name)
            result = self.observe(child)
            observed = True
            return result
        finally:
            if readfd >= 0:
                os.close(readfd)
            if writefd >= 0:
                os.close(writefd)
            if child is not None and (ref, name) in self.live and not observed:
                try:
                    self.observe(child)
                except Exception as error:
                    child["cleanup_error"] = type(error).__name__

    def observe(self, child):
        ref, name = child["ref"], child["name"]
        result = supervise(child["group"], timeout_seconds=10)
        for stream in (child["stdout"], child["stderr"]):
            if stream:
                stream.thread.join(0.5)
        output = {key: child[key].excerpt() if child[key] else None for key in ("stdout", "stderr")}
        result["output"] = output
        result["capture"] = {
            key: child[key].snapshot() if child[key] else None for key in ("stdout", "stderr")
        }
        self.store.observe(ref, name, "result", result)
        if result["group"]["confirmed_stop"]:
            self.live.pop((ref, name), None)
        self.fault("observed:" + name)
        return result

    def reconcile(self, ref):
        for (operation, _), child in list(self.live.items()):
            if operation == ref:
                self.observe(child)
