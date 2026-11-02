"""Actual final local Git operations, with durable intent and conservative recovery.

This adapter is not a merge orchestrator or a task-state authority. Its configured
Operations boundary is mandatory; fixture boundaries do not prove Core ordering.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from ai_task_manager.domains.merging.contracts import Binding, Observation, Publication, encode
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict, StaleVersion

from .profile import MAX_OUTPUT, GitProfile, file_digest, oid, physical

# The child cannot execute Git until its PID/group/birth has been journaled.
# EOF before release exits without executing Git. No shell or arbitrary script is accepted.
_GATE = (
    "import os,sys; f=int(sys.argv[1]); b=os.read(f,1); os.close(f); "
    "sys.exit(97) if b!=b'G' else os.execve(sys.argv[2],sys.argv[2:],dict(os.environ))"
)


class _Drain:
    """Drain throughout execution; retain a bounded prefix and exact byte count."""

    def __init__(self, stream):
        self.stream, self.prefix, self.count = stream, bytearray(), 0
        self.lock, self.done = threading.Lock(), threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                chunk = os.read(self.stream.fileno(), 65536)
                if not chunk:
                    break
                with self.lock:
                    self.count += len(chunk)
                    self.prefix.extend(chunk[: max(0, MAX_OUTPUT - len(self.prefix))])
        except OSError as error:
            self.error = type(error).__name__
        finally:
            self.stream.close()
            self.done.set()

    def line(self, expected, seconds):
        deadline = time.monotonic() + seconds
        while True:
            with self.lock:
                found = expected in bytes(self.prefix).splitlines()
            if found:
                return True
            if self.done.is_set() or time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def excerpt(self):
        with self.lock:
            return bytes(self.prefix[:512]).decode("utf-8", "replace")

    def snapshot(self):
        with self.lock:
            return {
                "bytes": self.count,
                "retained_bytes": len(self.prefix),
                "complete": self.done.is_set(),
                "error": self.error,
            }


@dataclass
class _Child:
    process: object
    initial: object
    kind: str
    stdout: _Drain
    stderr: _Drain


class LocalGit:
    def __init__(
        self, journal, profile: GitProfile, operations=None, *, observation_seconds=5, faults=None
    ):
        if type(observation_seconds) not in {int, float} or not 0 < observation_seconds <= 30:
            raise ValueError("A finite observation window of at most30 seconds is required")
        self.journal, self.profile, self.operations = journal, profile, operations
        self.observation_seconds, self.faults = observation_seconds, faults
        self._live = {}
        self.python = str(Path(sys.executable).resolve())
        self.python_binding = (physical(self.python, directory=False), file_digest(self.python))

    def _fault(self, stage):
        if self.faults is not None:
            self.faults.hit(stage)

    def _write(self, ref, data, stage):
        op = self.journal.current(ref)
        facts = dict(op["facts"], local_git=data)
        return self.journal.commit(
            ref, op["version"], "git:" + stage + ":" + str(uuid4()), "git_" + stage, facts, data
        )

    @staticmethod
    def _inventory(pid):
        # Read identities, never command lines or another process's environment.
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        try:
            r = subprocess.run(
                ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="],
                env=env,
                capture_output=True,
                timeout=2,
            )
            if r.returncode or len(r.stdout) > MAX_OUTPUT:
                return None
            rows = []
            for line in r.stdout.decode("ascii", "strict").splitlines():
                p, g, state, born = line.split(None, 3)
                rows.append({"pid": int(p), "group": int(g), "state": state, "birth": born})
            anchor = next((x for x in rows if x["pid"] == pid), None)
            return {"anchor": anchor, "members": [x for x in rows if x["group"] == pid]}
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            return None

    @classmethod
    def _quiescence(cls, process, initial):
        try:
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            return {"known": False, "reason": "waitable_child_unavailable"}
        current = cls._inventory(process.pid)
        if (
            not current
            or not initial
            or not initial.get("anchor")
            or not current["anchor"]
            or current["anchor"]
            != {**current["anchor"], "birth": initial["anchor"]["birth"], "group": process.pid}
        ):
            return {"known": False, "reason": "process_identity_unavailable"}
        live = [x for x in current["members"] if not x["state"].startswith(("Z", "X"))]
        exited = status is not None and status.si_pid == process.pid
        quiet = exited and not live
        code = None
        if exited:
            code = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
        return {"known": True, "quiescent": quiet, "returncode": code, "inventory": current}

    def _validate(self, op, request):
        binding = Binding(**op["binding"])
        if (
            physical(self.python, directory=False),
            file_digest(self.python),
        ) != self.python_binding:
            raise AccessDenied("Selected gate interpreter changed")
        r = self.profile.validate(binding.worktree_id, binding.repository_id)
        if binding.configuration_digest != r["configuration_digest"]:
            raise AccessDenied("Operation belongs to a different Git configuration")
        repo = Path(r["path"])
        intent = op["facts"].get("publication_intent")
        expected = asdict(request)
        if intent != expected or any(k in op["facts"] for k in ("publication", "result")):
            raise AccessDenied("A current exact publication intent is required")
        if self.profile.reference(repo, request.source_ref) != request.new:
            raise AccessDenied("Source branch changed after checking")
        if self.profile.reference(repo, r["target_ref"]) != request.old:
            raise AccessDenied("Target reference moved")
        if self.profile.query(
            repo, "merge-base", "--is-ancestor", request.old, request.new, ok=(0, 1)
        ).returncode:
            raise AccessDenied("Only a fast-forward candidate is supported")
        for name in (
            "rebase-merge",
            "rebase-apply",
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "index.lock",
        ):
            if (Path(r["gitdir"]) / name).exists():
                raise AccessDenied("An active or unresolved Git operation prevents publication")
        # Reject attributes in the candidate even when they are not yet checked out.
        names = self.profile.query(
            repo, "ls-tree", "-r", "--name-only", "-z", request.new
        ).stdout.split(b"\0")
        if any(p.split(b"/")[-1] == b".gitattributes" for p in names if p):
            raise AccessDenied("Candidate checkout attributes are unsupported")
        worktrees = self.profile.query(repo, "worktree", "list", "--porcelain", "-z").stdout
        checked = b"branch " + r["target_ref"].encode() + b"\0" in worktrees
        if request.mode == "cas":
            if checked:
                raise AccessDenied("CAS cannot update a checked-out target")
            if self.profile.query(
                repo, "status", "--porcelain", "-z", "--untracked-files=all"
            ).stdout:
                raise AccessDenied("Dirty source worktree prevents publication")
        if request.mode == "fast_forward":
            head = self.profile.query(repo, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).stdout.strip()
            if head != r["target_ref"].encode() or not checked:
                raise AccessDenied("Fast-forward requires this owned target checkout")
            if any(
                line.startswith((b"S ", b"s "))
                for line in self.profile.query(repo, "ls-files", "-v").stdout.splitlines()
            ):
                raise AccessDenied("Sparse checkout is unsupported")
        return r

    def publish(self, ref, expected_version, key, request: Publication):
        if type(request) is not Publication:
            raise AccessDenied("An exact typed publication request is required")
        request.validate()
        if self.operations is None:
            raise AccessDenied("Configured final Git authority boundary is unavailable")
        op = self.journal.current(ref)
        payload = asdict(request)
        previous = op["facts"].get("local_git")
        if previous:
            if previous["request"] != payload:
                raise IdempotencyConflict("Publication has different durable input")
            return self.reconcile(ref)
        if op["version"] != expected_version:
            raise StaleVersion("Operation version changed before publication")
        r = self._validate(op, request)
        marker = "ai-task-manager:" + str(uuid4())
        data = {
            "request": payload,
            "marker": marker,
            "stage": "intent",
            "repository": r,
            "executable_digest": self.profile.executable.sha256,
            "evidence_ref": "git-publication:" + str(uuid4()),
        }
        # Expected version and key are checked by the same existing journal transaction.
        self.journal.commit(
            ref, expected_version, key, "git_intent", dict(op["facts"], local_git=data), data
        )
        self._fault("after_intent")
        try:
            self._fault("before_final_validation")
            self._validate(self.journal.current(ref), request)
            if request.mode == "cas":
                self._spawn(
                    ref,
                    "publication",
                    [
                        "update-ref",
                        "--no-deref",
                        "--create-reflog",
                        "-m",
                        marker,
                        r["target_ref"],
                        request.new,
                        request.old,
                    ],
                )
                self._fault("after_release")
            else:
                self._fast_forward(ref, request)
            return self.reconcile(ref)
        except Exception as error:
            # Exceptions can follow ref/index/worktree effects. Closing transaction
            # input requests abort but never proves checkout rollback or child stop.
            for child in self._live.get(ref, []):
                self._close_input(child)
            current = self.journal.current(ref)["facts"]["local_git"]
            checkout = current.get("checkout_state", "not_started")
            self._write(
                ref,
                dict(
                    current,
                    stage="uncertain",
                    exception=type(error).__name__,
                    repair_required=checkout != "not_started",
                ),
                "uncertain",
            )
            return self.reconcile(ref)

    @staticmethod
    def _close_input(child):
        if child.process.stdin and not child.process.stdin.closed:
            child.process.stdin.close()

    def _spawn(self, ref, kind, args, *, interactive=False):
        data = self.journal.current(ref)["facts"]["local_git"]
        repo = Path(data["repository"]["path"])
        readfd, writefd = os.pipe()
        try:
            with self.operations.authority_boundary(ref):
                env = dict(self.profile.environment_for(repo), GIT_REFLOG_ACTION=data["marker"])
                argv = self.profile.argv(repo, *args)
                process = subprocess.Popen(
                    [self.python, "-I", "-S", "-c", _GATE, str(readfd), *argv],
                    env=env,
                    stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    pass_fds=(readfd,),
                    start_new_session=True,
                )
                os.close(readfd)
                readfd = -1
                initial = self._inventory(process.pid)
                child = _Child(
                    process, initial, kind, _Drain(process.stdout), _Drain(process.stderr)
                )
                self._live.setdefault(ref, []).append(child)
                owned = {"pid": process.pid, "group": process.pid, "initial": initial, "kind": kind}
                children = list(data.get("children", [])) + [owned]
                data = dict(
                    data,
                    stage="gated",
                    children=children,
                    process=data.get("process", owned),
                    started_monotonic=data.get("started_monotonic", time.monotonic()),
                )
                self._write(ref, data, "process")
                if (
                    not initial
                    or not initial["anchor"]
                    or initial["anchor"]["group"] != process.pid
                ):
                    raise AccessDenied("Initial process identity is unavailable")
                self._fault("before_release" if kind != "checkout" else "before_checkout_release")
                if kind == "checkout":
                    data = dict(data, checkout_state="begun", repair_required=True)
                    self._write(ref, data, "checkout_intent")
                os.write(writefd, b"G")
                os.close(writefd)
                writefd = -1
                self._write(ref, dict(data, stage="released"), "released")
            return child
        finally:
            if readfd >= 0:
                os.close(readfd)
            if writefd >= 0:
                os.close(writefd)  # EOF cancels only a child still behind its gate.

    @staticmethod
    def _send(child, command):
        child.process.stdin.write(command.encode("ascii"))
        child.process.stdin.flush()

    def _settle(self, ref, child, deadline):
        while True:
            observation = self._quiescence(child.process, child.initial)
            if (
                observation.get("quiescent")
                or not observation["known"]
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        if observation.get("quiescent"):
            data = self.journal.current(ref)["facts"]["local_git"]
            # Persist the original waitable identity before reap. Output is drained
            # concurrently, so large Git errors cannot block completion on a pipe.
            rows = [
                dict(row, quiescence=observation) if row["pid"] == child.process.pid else row
                for row in data["children"]
            ]
            self._write(ref, dict(data, children=rows), "child_quiescent")
            child.process.wait(timeout=0.5)
            for drain in (child.stdout, child.stderr):
                drain.thread.join(timeout=0.5)
            self._close_input(child)
            output = {
                "stdout_bytes": child.stdout.snapshot()["bytes"],
                "stderr_bytes": child.stderr.snapshot()["bytes"],
                "stdout": child.stdout.snapshot(),
                "stderr": child.stderr.snapshot(),
                "stderr_excerpt": child.stderr.excerpt(),
                "bounded": all(
                    d.snapshot()["retained_bytes"] <= MAX_OUTPUT
                    for d in (child.stdout, child.stderr)
                ),
            }
            data = self.journal.current(ref)["facts"]["local_git"]
            rows = [
                dict(row, output=output) if row["pid"] == child.process.pid else row
                for row in data["children"]
            ]
            self._write(ref, dict(data, children=rows), "child_output")
            self._live[ref].remove(child)
            if not self._live[ref]:
                self._live.pop(ref)
        return observation

    def _fast_forward(self, ref, request):
        data = self.journal.current(ref)["facts"]["local_git"]
        r = data["repository"]
        orig_head = Path(r["gitdir"]) / "ORIG_HEAD"
        previous_orig = "0" * 40
        if orig_head.exists() or orig_head.is_symlink():
            physical(orig_head, directory=False)
            if orig_head.stat().st_size > 128:
                raise AccessDenied("Unsupported ORIG_HEAD metadata")
            previous_orig = oid(orig_head.read_text().strip())
        transaction = self._spawn(
            ref,
            "ref_transaction",
            ["update-ref", "--create-reflog", "-m", data["marker"], "--stdin"],
            interactive=True,
        )
        self._fault("after_release")
        self._send(
            transaction,
            "start\nupdate HEAD "
            + request.new
            + " "
            + request.old
            + "\nupdate ORIG_HEAD "
            + request.old
            + " "
            + previous_orig
            + "\nprepare\n",
        )
        if not transaction.stdout.line(b"prepare: ok", self.observation_seconds):
            self._close_input(transaction)
            return  # Failed/unknown prepare never admits a checkout.
        data = self.journal.current(ref)["facts"]["local_git"]
        self._write(
            ref, dict(data, ref_prepared=True, checkout_state="not_started"), "ref_prepared"
        )
        self._fault("after_prepare")
        head = self.profile.query(
            data["repository"]["path"], "symbolic-ref", "-q", "HEAD", ok=(0, 1)
        ).stdout.strip()
        if (
            head != r["target_ref"].encode()
            or self.profile.reference(data["repository"]["path"], r["target_ref"]) != request.old
        ):
            self._send(transaction, "abort\n")
            transaction.stdout.line(b"abort: ok", self.observation_seconds)
            self._close_input(transaction)
            return
        # Prepared expected-old ref locks remain held through checkout and commit.
        # This is a Git transaction, never a Core/database transaction across I/O.
        checkout = self._spawn(ref, "checkout", ["read-tree", "-m", "-u", request.old, request.new])
        self._fault("after_checkout_release")
        observation = self._settle(ref, checkout, time.monotonic() + self.observation_seconds)
        if not observation.get("quiescent"):
            raise AccessDenied("Checkout completion is unknown")
        if observation.get("returncode") != 0:
            self._send(transaction, "abort\n")
            transaction.stdout.line(b"abort: ok", self.observation_seconds)
            self._close_input(transaction)
            data = self.journal.current(ref)["facts"]["local_git"]
            self._write(
                ref,
                dict(data, checkout_state="failed_or_partial", repair_required=True),
                "checkout_failed",
            )
            return
        data = self.journal.current(ref)["facts"]["local_git"]
        self._write(
            ref, dict(data, checkout_state="updated", repair_required=True), "checkout_updated"
        )
        self._fault("after_checkout_before_commit")
        with self.operations.authority_boundary(ref):
            self._send(transaction, "commit\n")
        acknowledged = transaction.stdout.line(b"commit: ok", self.observation_seconds)
        self._close_input(transaction)
        data = self.journal.current(ref)["facts"]["local_git"]
        self._write(ref, dict(data, ref_commit_acknowledged=acknowledged), "ref_commit_reply")

    def _effect(self, data):
        r, req = data["repository"], data["request"]
        log = Path(r["common"]) / "logs" / r["target_ref"]
        try:
            if log.is_symlink() or log.stat().st_size > MAX_OUTPUT:
                return False
            lines = log.read_bytes().splitlines()
        except OSError:
            return False
        pair = (req["old"] + " " + req["new"] + " ").encode()
        marker = data["marker"].encode()
        return any(
            line.startswith(pair) and b"\t" in line and marker in line.split(b"\t", 1)[1]
            for line in lines
        )

    def reconcile(self, ref):
        op = self.journal.current(ref)
        data = op["facts"].get("local_git")
        if data is None:
            raise AccessDenied("No durable Git operation exists")
        old_result = data.get("result")
        if old_result and old_result["publication"] == "committed" and old_result["quiescent"]:
            return old_result.copy()
        # Child lifetime observations do not depend on a still-valid Git profile.
        # Drift may block ref proof, but must not erase actual normal completion.
        deadline = time.monotonic() + self.observation_seconds
        for child in list(self._live.get(ref, [])):
            self._settle(ref, child, deadline)
        data = self.journal.current(ref)["facts"]["local_git"]
        rows = data.get("children", [])
        quiet = bool(rows) and all(row.get("quiescence", {}).get("quiescent") for row in rows)
        observations = [row.get("quiescence", {"known": False}) for row in rows]
        observation = (
            observations[0]
            if len(observations) == 1
            else {
                "known": bool(rows) and all(x.get("known") for x in observations),
                "quiescent": quiet,
                "children": observations,
                "returncode": next(
                    (x.get("returncode") for x in observations if x.get("returncode")), 0
                )
                if quiet
                else None,
            }
        )
        if not rows:
            observation = {"known": False, "reason": "original_waitable_process_unavailable"}
        output = (
            {
                "stdout_bytes": sum(row.get("output", {}).get("stdout_bytes", 0) for row in rows),
                "stderr_bytes": sum(row.get("output", {}).get("stderr_bytes", 0) for row in rows),
                "bounded": all(row.get("output", {}).get("bounded", False) for row in rows),
            }
            if quiet
            else None
        )
        binding = Binding(**op["binding"])
        current_target, confirmed, profile_error = None, False, None
        try:
            self.profile.validate(binding.worktree_id, binding.repository_id)
            confirmed = self._effect(data)
            current_target = self.profile.reference(
                data["repository"]["path"], data["repository"]["target_ref"]
            )
        except (AccessDenied, OSError) as error:
            profile_error = type(error).__name__
        publication = "committed" if confirmed else "uncertain"
        if (
            not confirmed
            and quiet
            and observation.get("returncode") not in (None, 0)
            and current_target == data["request"]["old"]
        ):
            publication = "refused"
        checkout = data.get("checkout_state", "not_started")
        repair = data.get("request", {}).get("mode") == "fast_forward" and checkout != "not_started"
        if confirmed and quiet and checkout == "updated" and data.get("ref_commit_acknowledged"):
            repair = False
        result = {
            "publication": publication,
            "quiescent": quiet,
            "evidence_ref": data["evidence_ref"],
            "process_observation": observation,
            "output": output,
            "elapsed_seconds": time.monotonic() - data.get("started_monotonic", time.monotonic()),
            "old": data["request"]["old"],
            "new": data["request"]["new"],
            "current_target": current_target,
            "checkout_state": checkout,
            "repair_required": repair,
            "profile_error": profile_error,
            "target_ref": data["repository"]["target_ref"],
            "repository_id": data["repository"]["repository_id"],
            "configuration_digest": data["repository"]["configuration_digest"],
        }
        self._fault("after_effect_observation")
        self._write(ref, dict(data, result=result, stage="observed"), "observed")
        return result

    def read(self, evidence_ref, binding):
        """Only actual scoped publication observations; no generic evidence factory."""
        for op in self.journal.for_attempt(binding.attempt_id):
            data = op["facts"].get("local_git", {})
            if data.get("evidence_ref") != evidence_ref or op["fingerprint"] != binding.fingerprint:
                continue
            result = data.get("result")
            if not result:
                raise AccessDenied("Publication has no observed result")
            payload = dict(
                op["facts"]["publication_intent"],
                confirmed=result["publication"] == "committed",
                current_target=result["current_target"],
            )
            return Observation(
                op["ref"], op["fingerprint"], evidence_ref, "publication", encode(payload)
            )
        raise AccessDenied("Publication evidence is unavailable for this binding")
