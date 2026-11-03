"""Trusted object import and recoverable CAS of one owned ref and worktree index."""

import fcntl
import hashlib
import os
import select
import subprocess
from pathlib import Path

from ai_task_manager.config.files import private_path
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .private import environment, file_state, query, sha, verify


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Publication:
    def __init__(self, journal, *, faults=None):
        self.journal, self.faults = journal, faults

    def _hit(self, name):
        if self.faults:
            self.faults.hit(name)

    def import_objects(self, record, head, effect_boundary):
        """Import only verified reachable objects; no hooks or arbitrary object paths."""
        verify(record)
        objects = (
            query(record, "rev-list", "--objects", "--no-object-names", head, "^" + record["base"])
            .decode()
            .splitlines()
        )
        if len(objects) > 4096:
            raise AccessDenied("Private commit exceeds the finite object budget")
        total = 0
        for oid in objects:
            if len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid):
                raise AccessDenied("Malformed private object identity")
            kind = query(record, "cat-file", "-t", oid).decode().strip()
            size = int(query(record, "cat-file", "-s", oid))
            total += size
            if kind not in {"commit", "tree", "blob"} or size > 1048576 or total > 16777216:
                raise AccessDenied("Private commit exceeds its object type or byte budget")
            raw = query(record, "cat-file", kind, oid)
            header = (kind + " " + str(len(raw))).encode() + b"\0"
            if len(raw) != size or hashlib.sha1(header + raw).hexdigest() != oid:
                raise AccessDenied("Private object bytes do not match their Git identity")
            host = {**record, "metadata": record["gitdir"]}
            with effect_boundary():
                process = subprocess.Popen(
                    [
                        record["git"],
                        "-c",
                        "core.hooksPath=/dev/null",
                        "hash-object",
                        "-w",
                        "--stdin",
                        "-t",
                        kind,
                    ],
                    env=environment(Path(host["metadata"]), Path(record["worktree"])),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            try:
                output, _ = process.communicate(raw, timeout=10)
            except BaseException:
                process.kill()
                process.communicate(timeout=5)
                raise
            with effect_boundary():
                pass
            if process.returncode or output.decode().strip() != oid:
                raise RuntimeUncertain("Verified object import did not settle")
        return objects

    def publish(self, attempt_id, record, head, tree, effect_boundary):
        root = Path(record["metadata"]).parent
        guard = os.open(root / "publication.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeUncertain("Another publisher retains this operation") from error
            return self._publish(attempt_id, record, head, tree, effect_boundary)
        finally:
            os.close(guard)

    def _publish(self, attempt_id, record, head, tree, effect_boundary):
        root, gitdir = Path(record["metadata"]).parent, Path(record["gitdir"])
        index, lock = gitdir / "index", gitdir / "index.lock"
        host = {**record, "metadata": str(gitdir)}
        intended = self.journal.effect(attempt_id, "worker-publish")
        if intended is None:
            if self.journal.effect(attempt_id, "worker-object-import") is None:
                self.journal.intend(
                    attempt_id, "worker-object-import", {"head": head, "tree": tree}
                )
            imported = self.import_objects(record, head, effect_boundary)
            self.journal.observe(
                attempt_id, "worker-objects-imported", {"head": head, "objects": imported}
            )
            replacement = root / "publication.index"
            if replacement.exists():
                raise RuntimeUncertain("Unjournaled replacement index requires recovery")
            query(host, "read-tree", tree, index=replacement)
            replacement.chmod(0o600)
            intended = {
                "head": head,
                "tree": tree,
                "branch": record["branch"],
                "base": record["base"],
                "gitdir": str(gitdir),
                "index": record["original_index"],
                "replacement": str(replacement),
                "replacement_sha256": sha(replacement),
                "imported": imported,
            }
            self.journal.intend(attempt_id, "worker-publish", intended)
        if (intended["head"], intended["tree"], intended["gitdir"], intended["branch"]) != (
            head,
            tree,
            str(gitdir),
            record["branch"],
        ):
            raise RuntimeUncertain("Publication differs from its original intent")
        private_path(intended["replacement"])
        replacement = Path(intended["replacement"])
        if sha(replacement) != intended["replacement_sha256"]:
            raise RuntimeUncertain("Trusted replacement index changed")
        current = query(host, "rev-parse", record["branch"]).decode().strip()
        if current not in {record["base"], head}:
            raise RuntimeUncertain("Owned branch moved outside the publication CAS")
        actual = file_state(index)
        completed = self.journal.observations(attempt_id, "worker-published")
        if completed:
            if current != head or actual != completed[-1]["index"]:
                raise RuntimeUncertain("Published branch or original index changed afterward")
            return head
        lock_receipts = self.journal.observations(attempt_id, "worker-index-lock")
        if current == head and actual and actual["sha256"] == intended["replacement_sha256"]:
            if not lock_receipts or actual["inode"] != lock_receipts[-1]["physical"]["inode"]:
                raise RuntimeUncertain("A foreign replacement index cannot settle publication")
            self.journal.observe(attempt_id, "worker-published", {"head": head, "index": actual})
            return head
        if actual != intended["index"]:
            raise RuntimeUncertain("Original index changed; foreign work is preserved")
        if sha(gitdir / "HEAD") != record["original_head"]:
            raise RuntimeUncertain("Original worktree HEAD changed")
        fd = None
        transaction = None
        try:
            if lock.exists():
                if not lock_receipts or file_state(lock) != lock_receipts[-1]["physical"]:
                    raise RuntimeUncertain("Foreign or unjournaled index lock is retained")
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
            else:
                if lock_receipts:
                    raise RuntimeUncertain("Owned publication lock disappeared")
                with effect_boundary():
                    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(os.dup(fd), "wb") as output:
                    output.write(replacement.read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                with effect_boundary():
                    pass
                self.journal.observe(
                    attempt_id, "worker-index-lock", {"physical": file_state(lock)}
                )
                sync_directory(gitdir)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeUncertain("Owned index lock still has a live writer") from error
            if current == record["base"]:
                with effect_boundary():
                    transaction = subprocess.Popen(
                        [record["git"], "-c", "core.hooksPath=/dev/null", "update-ref", "--stdin"],
                        env=environment(gitdir, Path(record["worktree"])),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                commands = "start\nupdate " + record["branch"] + " " + head + " " + current
                with effect_boundary():
                    transaction.stdin.write((commands + "\nprepare\n").encode())
                    transaction.stdin.flush()
                self._reply(transaction, b"start: ok\nprepare: ok\n")
                with effect_boundary():
                    pass
            if (
                file_state(index) != intended["index"]
                or sha(gitdir / "HEAD") != record["original_head"]
            ):
                raise RuntimeUncertain("Worktree index or HEAD changed while acquiring CAS locks")
            self.journal.observe(attempt_id, "worker-publish-prepared", {"head": head})
            if transaction is not None:
                with effect_boundary():
                    transaction.stdin.write(b"commit\n")
                    transaction.stdin.flush()
                self._reply(transaction, b"commit: ok\n")
                transaction.stdin.close()
                transaction.wait(timeout=5)
                if transaction.returncode:
                    raise RuntimeUncertain("Owned branch CAS has an unknown result")
            self._hit("after:publish-ref")
            if query(host, "rev-parse", record["branch"]).decode().strip() != head:
                raise RuntimeUncertain("Owned branch moved after its publication CAS")
            if file_state(index) != intended["index"]:
                raise RuntimeUncertain("Foreign index work appeared before publication rename")
            with effect_boundary():
                os.replace(lock, index)
            sync_directory(gitdir)
            self._hit("after:publish-index")
            self.journal.observe(
                attempt_id, "worker-published", {"head": head, "index": file_state(index)}
            )
            return head
        finally:
            if transaction is not None and transaction.poll() is None:
                transaction.stdin.close()
                try:
                    transaction.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    transaction.kill()
                    transaction.wait(timeout=5)
            if fd is not None:
                os.close(fd)

    def _reply(self, process, expected):
        output = b""
        while len(output) < len(expected):
            if not select.select([process.stdout], [], [], 5)[0]:
                raise RuntimeUncertain("Owned branch transaction did not acknowledge its phase")
            chunk = os.read(process.stdout.fileno(), len(expected) - len(output))
            if not chunk:
                raise RuntimeUncertain("Owned branch transaction ended before acknowledgement")
            output += chunk
        if output != expected:
            raise RuntimeUncertain("Owned branch transaction returned an unexpected phase")
