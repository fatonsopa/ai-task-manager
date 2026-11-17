"""Coordinate observed resource ownership without a second task policy."""

from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
from ai_task_manager.supervisor.models import ResourcePair, RuntimeRefused, RuntimeUncertain

from .consumers import consumer_boundary
from .git import GitWorktrees
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
        existing_only=False,
        successor_sources=None,
    ):
        self.journal = journal
        self.successor_sources = dict(successor_sources or {})
        self.root = Path(root).absolute()
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise RuntimeRefused("Owned resource root must have no symlink components")
        if existing_only:
            if not self.root.is_dir() or self.root.stat().st_mode & 0o077:
                raise RuntimeRefused("Original owned resource directory is unavailable")
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git = GitWorktrees(repository)
        self.databases = SQLiteFixtures((*metadata_paths, journal.path))
        self.release_policy = release_policy or DeliveryReleasePolicy(self.git.repository)
        self.removal_verifier = removal_verifier or RejectRemoval()

    def prepare(self, attempt_id, *, base="HEAD"):
        owner = self.journal.attempt(attempt_id)
        source = self.successor_sources.get(owner["task_id"])
        if source is not None:
            return self._successor_resources(owner, source)
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

    def _successor_resources(self, owner, source):
        """Transfer use of an explicitly admitted, settled task's owned resources.

        Files, Git branch and database ownership markers remain unchanged. The
        new runtime records the original physical owner instead of adopting or
        recreating resources from paths supplied by a caller.
        """
        original = self.journal.attempt(source)
        outcomes = self.journal.observations(source, "runtime_outcome")
        if (
            original["task_id"] != owner["task_id"]
            or original["scope"] != owner["scope"]
            or not outcomes
            or outcomes[-1].get("quiescent") is not True
        ):
            raise RuntimeRefused("Successor resources require the same settled task owner")
        pair = self.reconcile(source)
        with self.use(pair):
            fact = self.verify(pair)
            resource_owner = fact.get("source_attempt", source)
            self.journal.intend(
                owner["attempt_id"],
                "resources",
                {
                    "worktree": str(pair.worktree),
                    "database": str(pair.database),
                    "branch": fact["worktree"]["branch"],
                    "base": fact["worktree"]["head"],
                    "source_attempt": resource_owner,
                    "predecessor_attempt": source,
                },
            )
            return self._ready(
                owner, fact["worktree"], fact["database"], source_attempt=resource_owner
            )

    def _ready(self, owner, tree, db, *, source_attempt=None):
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
        if source_attempt is not None:
            fact["source_attempt"] = source_attempt
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
        source = intent.get("source_attempt", attempt_id)
        worktree = confined(self.root, f"trees/{source}")
        database = confined(self.root, f"databases/{source}.sqlite")
        if str(worktree) != intent["worktree"] or str(database) != intent["database"]:
            raise RuntimeRefused("Resource intent path changed")
        if not worktree.exists() or not database.exists():
            raise RuntimeUncertain("Partial or missing resources require recovery, not recreation")
        return self._ready(
            self.journal.attempt(attempt_id),
            self.git.inspect(worktree, intent["branch"]),
            self.databases.inspect(database, source),
            source_attempt=source if source != attempt_id else None,
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
        self.databases.inspect(pair.database, fact.get("source_attempt", pair.attempt_id))
        return fact

    @contextmanager
    def use(self, pair, *, observe=None):
        """Pin physical resources; callers reverify after waits before consumption."""
        self.journal.attempt(pair.attempt_id)
        with self._consumer_boundary(pair.attempt_id, observe=observe):
            yield self.verify(pair)

    def cleanup(self, core, handle, attempt_id, *, check=lambda: None):
        # Scope denial must precede creating any physical or journal fence.
        core.get_attempt(handle, attempt_id)
        self.journal.attempt(attempt_id)
        with self._consumer_boundary(attempt_id, exclusive=True):
            check()
            return self._cleanup(core, handle, attempt_id, check=check)

    @contextmanager
    def _consumer_boundary(self, attempt_id, *, exclusive=False, observe=None):
        intent = self.journal.effect(attempt_id, "resources") or {}
        fence_owner = intent.get("source_attempt", attempt_id)
        records = self.journal.observations(attempt_id, "consumer_fence")
        source_records = (
            self.journal.observations(fence_owner, "consumer_fence")
            if fence_owner != attempt_id
            else []
        )
        if fence_owner != attempt_id and not source_records:
            raise RuntimeRefused("Original resource consumer fence history is unavailable")
        with consumer_boundary(
            self.root,
            fence_owner,
            exclusive=exclusive,
            existing_only=fence_owner != attempt_id or bool(records),
        ) as (physical, created):
            if source_records and any(
                {key: record.get(key) for key in physical} != physical for record in source_records
            ):
                raise RuntimeRefused("Original resource consumer fence identity changed")
            if records and any(
                {key: record.get(key) for key in physical} != physical for record in records
            ):
                raise RuntimeRefused("Resource consumer fence physical identity changed")
            if not records:
                if not created:
                    source = self.journal.attempt(fence_owner)
                    owner = self.journal.attempt(attempt_id)
                    prior = self.journal.observations(fence_owner, "consumer_fence")
                    if (
                        fence_owner == attempt_id
                        or not intent.get("predecessor_attempt")
                        or source["task_id"] != owner["task_id"]
                        or source["scope"] != owner["scope"]
                        or not prior
                        or any(
                            {key: record.get(key) for key in physical} != physical
                            for record in prior
                        )
                    ):
                        raise RuntimeRefused("Resource consumer fence ownership is unconfirmed")
                    if not callable(observe):
                        raise RuntimeRefused("Current successor consumer authority is required")
                    current = observe()
                    if (
                        current.get("task_id") != owner["task_id"]
                        or current.get("attempt_id") != attempt_id
                        or current.get("source_attempt") != fence_owner
                        or current.get("scope") != owner["scope"]
                        or current.get("purpose") != "worker_commit"
                        or not current.get("capability")
                        or current.get("deadline") != owner["deadline"]
                        or self.journal.clock() >= current["deadline"]
                    ):
                        raise RuntimeRefused("Successor consumer authority differs from its owner")
                    physical = {**physical, "current_use": current}
                self.journal.observe(attempt_id, "consumer_fence", physical)
            yield

    def _cleanup(self, core, handle, attempt_id, *, check=lambda: None):
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
        # A successor can share these physical resources. Its consumers must
        # have settled too; an old stopped attempt alone is insufficient.
        with self.journal.transaction() as conn:
            related = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM runtime_attempts WHERE task_id=?", (attempt.task_id,)
                )
            ]
        for related_id in related:
            if related_id == attempt_id:
                continue
            intent = self.journal.effect(related_id, "resources")
            if intent and intent.get("worktree") == str(tree):
                other = core.get_attempt(handle, related_id)
                stopped = self.journal.observations(related_id, "runtime_outcome")
                if (
                    other.state in ACTIVE_ATTEMPTS
                    or not stopped
                    or not stopped[-1].get("quiescent")
                ):
                    raise RuntimeRefused("Another resource consumer remains unresolved")
        # Observe the current commit; a worker may have committed since resource creation.
        if tree.exists():
            current = self.git.inspect(tree, fact["worktree"]["branch"], release=True)
            fact = {**fact, "worktree": {**fact["worktree"], "head": current["head"]}}
        elif self.journal.effect(attempt_id, "remove_worktree"):
            fact = {
                **fact,
                "worktree": self.journal.effect(attempt_id, "remove_worktree")["resource"],
            }
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
            removal = self.journal.effect(attempt_id, f"remove_{kind}")
            if removal is not None:
                if removal != {"resource": fact[kind]}:
                    raise RuntimeRefused("Original removal intent differs from resource ownership")
                if not path.exists() and not path.is_symlink():
                    if kind == "worktree" and f"worktree {path}\n" in (
                        self.git.registered_worktrees() + "\n"
                    ):
                        raise RuntimeUncertain(
                            "Worktree removal is not confirmed by its repository"
                        )
                    check()
                    self.journal.observe(
                        attempt_id,
                        f"{kind}_removed",
                        {
                            "path": str(path),
                            "reconciled_original_intent": True,
                        },
                    )
                    continue
            unchanged(path, fact[kind]["physical"])
            if kind == "worktree":
                self.git.inspect(path, fact[kind]["branch"], release=True)
                self.git.require_clean(path)
            else:
                self.databases.inspect(path, fact["database"]["owner"])
        for kind, path, provider in (
            ("worktree", tree, self.git),
            ("database", db, self.databases),
        ):
            if not self.journal.observations(attempt_id, f"{kind}_removed"):
                check()
                if self.journal.effect(attempt_id, f"remove_{kind}") is None:
                    self.journal.intend(attempt_id, f"remove_{kind}", {"resource": fact[kind]})
                provider.remove(path, fact[kind])
                check()
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
