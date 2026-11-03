"""Working contained worker commits with private Git state and trusted publication."""

import hashlib
import os
import time
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .container import WorkerGitContainer
from .private import file_state, prepare, query, verify
from .publication import Publication
from .repository import WorkerGit


def workspace(path):
    result, total = {}, 0
    for root, directories, files in os.walk(path, followlinks=False):
        if Path(root) == path and ".git" in directories:
            directories.remove(".git")
        for name in [*directories, *files]:
            target = Path(root) / name
            if target.is_symlink():
                raise AccessDenied("Confined Git worktree symlinks require separate admission")
        for name in files:
            target = Path(root) / name
            if not target.is_file():
                raise AccessDenied("Unsupported confined Git worktree entry")
            total += target.stat().st_size
            if total > 16777216 or target.stat().st_size > 1048576 or len(result) >= 4096:
                raise AccessDenied("Confined Git worktree exceeds its finite byte/file budget")
            result[str(target.relative_to(path))] = {
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "executable": bool(target.stat().st_mode & 0o111),
            }
    return result


class PrivateView:
    def __init__(self, record):
        self.record = record

    def query(self, path, *args, index=None):
        return query(self.record, *args, index=index)


class ConfinedWorkerGit(WorkerGit):
    def __init__(self, journal, container, *, executable="/usr/bin/git", faults=None):
        if type(container) is not WorkerGitContainer or container.journal is not journal:
            raise AccessDenied("Explicit actual-resource worker Git capability is required")
        super().__init__(journal, executable=executable, faults=faults)
        self.container = container
        self.publication = Publication(journal, faults=faults)

    def _record(self, attempt_id):
        record = self.journal.effect(attempt_id, "worker-private-git")
        if record is None:
            raise RuntimeUncertain("Private Git preparation is missing")
        verify(record)
        return record

    def _for_path(self, path):
        branch = self.query(path, "symbolic-ref", "--short", "HEAD").decode().strip()
        if not branch.startswith("managed/"):
            raise AccessDenied("Owned worker branch required")
        return self._record(branch.removeprefix("managed/"))

    def snapshot(self, path, allowed):
        """Read bytes and built-in Git metadata without running checkout filters."""
        path = Path(path)
        base = self.query(path, "rev-parse", "HEAD").decode().strip()
        indexed, committed = {}, {}
        for entry in self.query(path, "ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                mode, oid, stage = metadata.decode().split()
                if stage != "0":
                    raise AccessDenied("Conflicted worker index requires its owner's recovery")
                indexed[name.decode()] = (mode, oid)
        for entry in self.query(path, "ls-tree", "-r", "-z", base).split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                if kind != "blob" or mode not in {"100644", "100755"}:
                    raise AccessDenied("Unsupported base Git entry")
                committed[name.decode()] = (mode, oid)
        if indexed != committed:
            raise AccessDenied("Preexisting staged files require their owner's recovery")
        materialized = workspace(path)
        changed = set()
        total = 0
        for name, (mode, oid) in committed.items():
            size = int(self.query(path, "cat-file", "-s", oid))
            total += size
            if size > 1048576 or total > 16777216 or len(committed) > 4096:
                raise AccessDenied("Base Git files exceed the finite comparison budget")
            expected = {
                "sha256": hashlib.sha256(self.query(path, "cat-file", "blob", oid)).hexdigest(),
                "executable": mode == "100755",
            }
            if materialized.get(name) != expected:
                changed.add(name)
        changed.update(
            p.decode()
            for p in self.query(path, "ls-files", "--others", "--exclude-standard", "-z").split(
                b"\0"
            )
            if p
        )
        if not changed or not changed <= set(allowed):
            raise AccessDenied("Worker change is empty or exceeds its installed file scope")
        return {
            "files": sorted(changed),
            "content": self.content(path, sorted(changed)),
            "git_source": self.source(path),
            "workspace_state": materialized,
        }

    def stage(self, attempt_id, path, files, boundary, seconds, effect_boundary):
        boundary()
        intent = self.journal.effect(attempt_id, "worker-commit-intent")
        root = self.journal.path.parent / "worker-private-git" / attempt_id
        root.mkdir(mode=0o700, parents=True)
        record = prepare(self, attempt_id, path, root / "git", intent)
        self.journal.intend(attempt_id, "worker-private-git", record)
        started = time.monotonic()
        result = self.container.execute(
            attempt_id,
            "stage",
            record,
            {"files": files, "index": str(root / "git/index")},
            boundary,
            seconds,
            effect_boundary,
        )
        self.journal.observe(
            attempt_id, "worker-git-result", {**result, "seconds": time.monotonic() - started}
        )
        if self.faults:
            self.faults.hit("after:stage")
        return result

    def whitespace(self, attempt_id, path, boundary, seconds, effect_boundary):
        boundary()
        result = self.container.result(attempt_id, "stage")
        self.journal.observe(attempt_id, "worker-git-result", {**result, "stage": "diff-check"})
        return result

    def tree(self, path):
        return query(self._for_path(path), "write-tree").decode().strip()

    def checked_tree(self, path, intent):
        record = self._record(intent["attempt_id"])
        if file_state(Path(record["gitdir"]) / "index") != record["original_index"]:
            raise AccessDenied("Original index changed; foreign staged work is preserved")
        if workspace(Path(path)) != intent["workspace_state"]:
            raise AccessDenied("Checked worktree bytes changed during contained staging or checks")
        tree = self.tree(path)
        WorkerGit.validate_tree(PrivateView(record), path, tree, intent)
        return tree

    def prepare_index(self, attempt_id, path, tree, boundary, seconds, effect_boundary):
        boundary()
        record = self._record(attempt_id)
        index = Path(record["metadata"]) / "candidate.index"
        if index.exists():
            raise RuntimeUncertain("Existing private commit index requires recovery")
        query(record, "read-tree", tree, index=index)
        index.chmod(0o600)
        return str(index)

    def commit(self, attempt_id, path, message, boundary, seconds, effect_boundary, *, index):
        record = self._record(attempt_id)
        candidate = self.journal.effect(attempt_id, "worker-commit-candidate")
        if index != str(Path(record["metadata"]) / "candidate.index"):
            raise AccessDenied("Commit index differs from the prepared private candidate")
        started = time.monotonic()
        result = self.container.execute(
            attempt_id,
            "commit",
            record,
            {"index": index, "tree": candidate["tree"], "message": message},
            boundary,
            seconds,
            effect_boundary,
        )
        self.journal.observe(
            attempt_id, "worker-git-result", {**result, "seconds": time.monotonic() - started}
        )
        if self.faults:
            self.faults.hit("after:commit")
        return result

    def require_quiescent_commit(self, attempt_id):
        return self.container.result(attempt_id, "commit")

    def _private_commit(self, record, candidate):
        head = query(record, "rev-parse", "HEAD").decode().strip()
        parents = query(record, "rev-list", "--parents", "-n", "1", head).decode().split()
        tree = query(record, "rev-parse", head + "^{tree}").decode().strip()
        message = query(record, "show", "-s", "--format=%B", head).decode().rstrip("\n")
        if (
            parents != [head, candidate["base"]]
            or tree != candidate["tree"]
            or message != candidate["message"]
        ):
            raise RuntimeUncertain("Actual private worker commit differs from its saved intent")
        WorkerGit.validate_tree(PrivateView(record), Path(record["worktree"]), tree, candidate)
        return head

    def reconcile_publication(self, attempt_id, path, candidate, effect_boundary):
        record = self._record(attempt_id)
        self.require_quiescent_commit(attempt_id)
        head = self._private_commit(record, candidate)
        if self.source(path) != candidate["git_source"]:
            raise RuntimeUncertain("Original Git source changed before publication")
        if workspace(Path(path)) != candidate["workspace_state"]:
            raise RuntimeUncertain("Post-hook worktree differs; existing work is preserved")
        return self.publication.publish(
            attempt_id, record, head, candidate["tree"], effect_boundary
        )

    def committed(self, path, intent):
        record = self._record(intent["attempt_id"])
        head = self._private_commit(record, intent)
        published = self.journal.observations(intent["attempt_id"], "worker-published")
        if (
            not published
            or published[-1]["head"] != head
            or self.query(path, "rev-parse", "HEAD").decode().strip() != head
            or file_state(Path(record["gitdir"]) / "index") != published[-1]["index"]
            or workspace(Path(path)) != intent["workspace_state"]
        ):
            raise RuntimeUncertain("Actual owned worker publication differs from its saved receipt")
        return head
