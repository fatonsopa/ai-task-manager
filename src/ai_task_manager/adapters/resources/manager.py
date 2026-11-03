"""Coordinate observed resource ownership without a second task policy."""

from pathlib import Path

from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
from ai_task_manager.supervisor.models import ResourcePair, RuntimeRefused, RuntimeUncertain

from .git import GitWorktrees, git
from .paths import confined, unchanged
from .release import DeliveryReleasePolicy, RejectRemoval, RemovalReceipt
from .sqlite import SQLiteFixtures


class ResourceManager:
    def __init__(
        self,
        journal,
        root,
        repository,
        *,
        metadata_paths=(),
        release_policy=None,
        removal_verifier=None,
    ):
        self.journal = journal
        self.root = Path(root).absolute()
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise RuntimeRefused("Owned resource root must have no symlink components")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git = GitWorktrees(repository)
        self.databases = SQLiteFixtures((*metadata_paths, journal.path))
        self.release_policy = release_policy or DeliveryReleasePolicy(self.git.repository)
        self.removal_verifier = removal_verifier or RejectRemoval()

    def prepare(self, attempt_id, *, base="HEAD"):
        owner = self.journal.attempt(attempt_id)
        worktree = confined(self.root, f"trees/{attempt_id}")
        database = confined(self.root, f"databases/{attempt_id}.sqlite")
        branch = f"managed/{attempt_id}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.journal.intend(
            attempt_id,
            "resources",
            {
                "worktree": str(worktree),
                "database": str(database),
                "branch": branch,
                "base": base,
            },
        )
        tree = self.git.create(worktree, branch, base)
        self.journal.observe(attempt_id, "worktree_created", tree)
        db = self.databases.create(database, attempt_id)
        self.journal.observe(attempt_id, "database_created", db)
        return self._ready(owner, tree, db)

    def _ready(self, owner, tree, db):
        attempt_id = owner["attempt_id"]
        fact = {
            "worktree": tree,
            "database": db,
            "worker_id": owner["worker_id"],
            "worktree_id": f"git:{tree['common']}:{tree['branch']}",
            "database_id": (
                f"sqlite:{db['path']}:{db['physical']['device']}:{db['physical']['inode']}"
            ),
        }
        observation = self.journal.observe(attempt_id, "resources_ready", fact)
        return ResourcePair(
            attempt_id,
            Path(tree["path"]),
            Path(db["path"]),
            fact["worktree_id"],
            fact["database_id"],
            observation,
        )

    def reconcile(self, attempt_id):
        intent = self.journal.effect(attempt_id, "resources")
        if intent is None:
            raise RuntimeUncertain("No resource intent exists")
        worktree = confined(self.root, f"trees/{attempt_id}")
        database = confined(self.root, f"databases/{attempt_id}.sqlite")
        if str(worktree) != intent["worktree"] or str(database) != intent["database"]:
            raise RuntimeRefused("Resource intent path changed")
        if not worktree.exists() or not database.exists():
            raise RuntimeUncertain("Partial or missing resources require recovery, not recreation")
        return self._ready(
            self.journal.attempt(attempt_id),
            self.git.inspect(worktree, intent["branch"]),
            self.databases.inspect(database, attempt_id),
        )

    def verify(self, pair):
        fact = self.journal.observation(
            pair.observation_id, attempt_id=pair.attempt_id, kind="resources_ready"
        )
        for kind, path in (("worktree", pair.worktree), ("database", pair.database)):
            if str(path) != fact[kind]["path"]:
                raise RuntimeRefused("Resource path does not match its observation")
            unchanged(path, fact[kind]["physical"])
        self.git.inspect(pair.worktree, fact["worktree"]["branch"])
        self.databases.inspect(pair.database, pair.attempt_id)
        return fact

    def cleanup(self, core, handle, attempt_id):
        attempt = core.get_attempt(handle, attempt_id)
        task = core.get_task(handle, attempt.task_id)
        if attempt.state in ACTIVE_ATTEMPTS or task.execution_state in {"merging", "unknown"}:
            raise RuntimeRefused("Unresolved attempt or merge retains its resources")
        if self.journal.effect(
            attempt_id, "worker-commit-intent"
        ) is not None and not self.journal.observations(attempt_id, "worker-commit-complete"):
            raise RuntimeRefused("Unresolved worker commit retains its worktree and database")
        outcomes = self.journal.observations(attempt_id, "runtime_outcome")
        if not outcomes or outcomes[-1].get("quiescent") is not True:
            raise RuntimeRefused("Actual runtime stop must be confirmed before cleanup")
        records = self.journal.observations(attempt_id, "resources_ready")
        if not records:
            raise RuntimeUncertain("Resource ownership is not confirmed")
        fact = records[-1]
        tree, db = Path(fact["worktree"]["path"]), Path(fact["database"]["path"])
        # Observe the current commit; a worker may have committed since resource creation.
        if tree.exists():
            current = self.git.inspect(tree, fact["worktree"]["branch"])
            fact = {**fact, "worktree": {**fact["worktree"], "head": current["head"]}}
        self.release_policy.authorize(core, handle, attempt, fact)
        # Revalidate confinement and both resources before the first deletion.
        for kind, path in (("worktree", tree), ("database", db)):
            expected = confined(self.root, str(path.relative_to(self.root)))
            if expected != path:
                raise RuntimeRefused("Resource escaped the owned root")
            removed = self.journal.observations(attempt_id, f"{kind}_removed")
            if removed:
                if path.exists() or path.is_symlink():
                    raise RuntimeRefused("A new object occupies a removed resource path")
                continue
            unchanged(path, fact[kind]["physical"])
            if kind == "worktree":
                self.git.inspect(path, fact[kind]["branch"])
                if git(path, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeRefused("Uncommitted work prevents resource removal")
            else:
                self.databases.inspect(path, attempt_id)
        for kind, path, provider in (
            ("worktree", tree, self.git),
            ("database", db, self.databases),
        ):
            if not self.journal.observations(attempt_id, f"{kind}_removed"):
                self.journal.intend(attempt_id, f"remove_{kind}", {"resource": fact[kind]})
                provider.remove(path, fact[kind])
                self.journal.observe(attempt_id, f"{kind}_removed", {"path": str(path)})
        return {"worktree": "removed", "database": "removed"}

    def reconcile_removed(self, core, handle, attempt_id, kind, opaque_ref):
        if kind not in {"worktree", "database"}:
            raise RuntimeRefused("Unknown resource removal kind")
        attempt = core.get_attempt(handle, attempt_id)
        task = core.get_task(handle, attempt.task_id)
        if self.journal.effect(
            attempt_id, "worker-commit-intent"
        ) is not None and not self.journal.observations(attempt_id, "worker-commit-complete"):
            raise RuntimeRefused("Unresolved worker commit retains its worktree and database")
        outcomes = self.journal.observations(attempt_id, "runtime_outcome")
        if (
            attempt.state in ACTIVE_ATTEMPTS
            or task.execution_state in {"merging", "unknown"}
            or not outcomes
            or outcomes[-1].get("quiescent") is not True
        ):
            raise RuntimeRefused("Active attempts retain resource ownership")
        facts = self.journal.observations(attempt_id, "resources_ready")
        if not facts:
            raise RuntimeUncertain("Original resource ownership is missing")
        fact = facts[-1]
        receipt = self.removal_verifier.verify_removal(opaque_ref)
        if (
            not isinstance(receipt, RemovalReceipt)
            or receipt.attempt_id != attempt_id
            or receipt.kind != kind
            or receipt.resource_id != fact[f"{kind}_id"]
            or not receipt.source_ref
        ):
            raise RuntimeRefused("Removal receipt does not match the exact resource owner")
        path = Path(fact[kind]["path"])
        if path.exists() or path.is_symlink():
            raise RuntimeRefused("A removed resource path is still occupied")
        return self.journal.observe(
            attempt_id,
            f"{kind}_removed",
            {
                "path": str(path),
                "verified_removal_ref": opaque_ref,
                "source_ref": receipt.source_ref,
            },
        )
