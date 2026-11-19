"""Trusted refresh composition; provider work ends before Core consumes a receipt."""

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock, local
from time import monotonic, sleep
from uuid import uuid4

from ai_task_manager.domains.scheduling.models import (
    Dependency,
    DependencySnapshot,
    SchedulingBlocked,
)
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    EvidenceRequired,
    StaleVersion,
    ValidationError,
)

from .reader import DependencyReader, digest


@dataclass(frozen=True)
class Preparation:
    task_id: str
    task_version: int
    decision_revision: int
    snapshot_ref: str
    operation_key: str


class DependencyPublicationCancelled(AccessDenied):
    """An identified caller cancelled its pending publication observation."""

    def __init__(self, cancelled):
        super().__init__("Dependency publication cancelled")
        self.cancelled = cancelled


def milestone_declarations(value):
    if not isinstance(value, list) or len(value) > 128:
        raise ValidationError("A bounded dependency milestone list is required")
    declarations = []
    for item in value:
        keys = {"task_prose_digest", "predecessor_prose_digest", "condition"}
        if not isinstance(item, dict) or set(item) != keys:
            raise ValidationError("Dependency milestone fields are invalid")
        task, predecessor = item["task_prose_digest"], item["predecessor_prose_digest"]
        if (
            any(
                not isinstance(v, str)
                or len(v) != 64
                or any(c not in "0123456789abcdef" for c in v)
                for v in (task, predecessor)
            )
            or task == predecessor
            or item["condition"] != "code_merged"
            or (task, predecessor) in declarations
        ):
            raise ValidationError("A unique explicit code-merge milestone is required")
        declarations.append((task, predecessor))
    return tuple(declarations)


class DependencySource:
    def __init__(self, core, handle, client, receipts, *, milestones=()):
        self.core, self.handle, self.client, self.receipts = core, handle, client, receipts
        self.binding = core.session_binding(handle)
        self.expected = client.expected
        self.milestones = tuple(milestones)
        self._milestones = self.milestones
        self._publications = {}
        self._publication_local = local()
        self.source_id = "linear-dependencies-v1:" + digest(
            {
                "scope": vars(self.binding[1]),
                "team": client.expected.team_id,
                **({"milestones": self.milestones} if self.milestones else {}),
            }
        )

    def _scope(self):
        if (
            self.core.session_binding(self.handle) != self.binding
            or self.client.expected != self.expected
            or self.milestones != self._milestones
        ):
            raise AccessDenied("Dependency source session binding changed")
        return self.binding[1]

    def prepare(self, task_id, operation_key, *, expected_version=None, decision_revision=None):
        with self.publication_boundary(task_id):
            return self._prepare(
                task_id, operation_key, expected_version=expected_version,
                decision_revision=decision_revision,
            )

    @contextmanager
    def publication_boundary(self, task_id, *, not_after=None, cancelled=None):
        """Serialize one task's collection through publication, outside Core."""
        self._scope()
        self.core.get_task(self.handle, task_id)
        deadline = self.core.context.clock() + 30 if not_after is None else not_after
        end = monotonic() + max(0, deadline - self.core.context.clock())
        lock = self._publications.setdefault(task_id, RLock())

        def current():
            self._scope()
            if self.core.context.clock() >= deadline or monotonic() >= end:
                raise AccessDenied("Dependency publication stopped or expired")
            if cancelled is not None and cancelled.is_set():
                raise DependencyPublicationCancelled(cancelled)

        while True:
            current()
            if lock.acquire(timeout=min(0.05, max(0, end - monotonic()))):
                break
        descriptor = None
        depths = getattr(self._publication_local, "depths", {})
        self._publication_local.depths = depths
        nested = depths.get(task_id, 0)
        try:
            current()
            if not nested:
                head, _ = self.receipts._keys(self._scope(), task_id, "publication")
                path = self.receipts.path.with_name(self.receipts.path.name + "." + head + ".lock")
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                opened = os.fstat(descriptor)

                def check_file():
                    observed = path.lstat()
                    if (
                        not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600
                        or opened.st_uid != os.getuid() or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
                    ):
                        raise AccessDenied("Dependency publication fence changed")

                while True:
                    current()
                    check_file()
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        sleep(min(0.01, max(0, end - monotonic())))
                check_file()
            depths[task_id] = nested + 1
            current()
            yield
            if descriptor is not None:
                check_file()
        finally:
            depths[task_id] = nested
            if descriptor is not None:
                os.close(descriptor)
            lock.release()

    def _prepare(self, task_id, operation_key, *, expected_version=None, decision_revision=None):
        scope = self._scope()
        task = self.core.get_task(self.handle, task_id)
        if expected_version is not None or decision_revision is not None:
            self.core.context.check(task, expected_version, decision_revision)
        prior = self.receipts.lookup(scope, task_id, operation_key)
        if prior and prior["receipt"]:
            if prior["source_id"] != self.source_id:
                raise AccessDenied("Original dependency operation uses another milestone policy")
            c = prior["context"]
            return Preparation(
                task_id, c["task_version"], c["decision_revision"], prior["receipt"], operation_key
            )
        if task.issue_id is None:
            raise EvidenceRequired("Dependency observation needs a bound canonical issue")
        state = self.core.scheduling.explain(self.handle, task_id)
        operation = self.receipts.begin(scope, task, state["graph_version"], operation_key)
        c = operation["context"]
        if (task.version, task.decision_revision, task.prose_digest, task.issue_id) != (
            c["task_version"],
            c["decision_revision"],
            c["prose_digest"],
            c["issue_id"],
        ):
            raise StaleVersion("Task changed during dependency collection")
        rows = self.core.list_tasks(self.handle)
        local = {}
        for row in rows:
            if row.issue_id:
                if row.issue_id in local:
                    raise EvidenceRequired("Canonical dependency ownership is ambiguous")
                local[row.issue_id] = row
        observation = DependencyReader(self.client, scope).observe(task, local)
        self._scope()
        observed_issues = observation.raw.get("first", {}).get("nodes", {})
        for issue in {*observed_issues, task.issue_id}:
            before = local[issue]
            if self.core.get_task(self.handle, before.id) != before:
                raise StaleVersion("Local dependency facts changed during provider observation")
        now = self.receipts.clock()
        if not 0 <= now - c["started_at"] <= 30:
            raise EvidenceRequired("Dependency observation exceeded its freshness window")
        references = {}
        for event in self.core.notifications.list_events(self.handle):
            if event.kind == "transition_requested":
                refs = event.payload.get("evidence_refs", {})
                if isinstance(refs, dict):
                    references.setdefault(event.task_id, {}).update(refs)
        merged = set()
        for task_digest, predecessor_digest in self.milestones:
            if task_digest != task.prose_digest:
                continue
            subjects = [row for row in rows if row.prose_digest == task_digest]
            predecessors = [row for row in rows if row.prose_digest == predecessor_digest]
            if (
                len(subjects) != 1
                or subjects[0].id != task.id
                or len(predecessors) != 1
                or not predecessors[0].code_bearing
                or predecessors[0].issue_id not in observation.predecessors
            ):
                raise EvidenceRequired("Explicit milestone requires one current observed code edge")
            merged.add(predecessors[0].issue_id)
            reference = references.get(predecessors[0].id, {}).get("merge")
            prepare = getattr(self.core.context.evidence, "prepare_merge_milestone", None)
            if reference is not None and callable(prepare):
                prepare(predecessors[0].id, reference)
        edges = tuple(
            Dependency(
                local[issue].id,
                "code_merged" if issue in merged else "work_done",
                evidence_refs=tuple(
                    sorted(
                        (kind, ref)
                        for kind, ref in references.get(local[issue].id, {}).items()
                        if kind
                        in {"merge", "staging", "production", "non_code_completion", "quiescence"}
                        and isinstance(ref, str)
                    )
                ),
            )
            for issue in observation.predecessors
        )
        snapshot = DependencySnapshot(
            scope,
            task.id,
            task.prose_digest,
            task.issue_id,
            self.source_id,
            operation["generation"],
            now,
            now + 30,
            "complete" if observation.complete else "unavailable",
            observation.priority,
            edges,
            c["previous_graph_version"],
        )
        reference = self.receipts.finish(
            scope,
            task_id,
            operation_key,
            c,
            snapshot,
            {
                "observation": observation.raw,
                "schema_sha256": observation.schema_hash,
                "collection_started_at": c["started_at"],
                "collection_finished_at": now,
                "task_versions": {
                    issue: {
                        "task_id": row.id,
                        "version": row.version,
                        "decision_revision": row.decision_revision,
                        "prose_digest": row.prose_digest,
                    }
                    for issue, row in local.items()
                    if issue in observed_issues
                },
            },
        )
        return Preparation(task.id, task.version, task.decision_revision, reference, operation_key)

    def refresh(self, task_id, operation_key):
        with self.publication_boundary(task_id):
            return self._refresh(task_id, operation_key)

    def _refresh(self, task_id, operation_key):
        prepared = self.prepare(task_id, operation_key)
        return self.core.scheduling.observe(
            self.handle,
            prepared.task_id,
            prepared.task_version,
            prepared.decision_revision,
            prepared.snapshot_ref,
            prepared.operation_key,
        )

    def prepare_current_milestones(self, snapshot_ref, *, task_id=None):
        """Refresh local publication witnesses inside the original provider window."""
        if not self.milestones:
            return
        scope = self._scope()
        if task_id is None:
            task_id = self.receipts.verify_snapshot(snapshot_ref).task_id
        task = self.core.get_task(self.handle, task_id)
        if not any(subject == task.prose_digest for subject, _ in self.milestones):
            return
        with self.publication_boundary(task_id):
            return self._prepare_published_milestones(
                scope, self.core.get_task(self.handle, task_id)
            )

    def _prepare_published_milestones(self, scope, task):
        from ai_task_manager.storage import scheduling

        with self.core.context.db.transaction() as conn:
            published = scheduling.current(conn, task.id)
        if published is None or published["invalidated_by"] is not None:
            raise AccessDenied("Current published dependency observation is unavailable")
        snapshot_ref = self.receipts.published_reference(
            scope, task.id, published["source_revision"]
        )
        snapshot = self.receipts.verify_snapshot(snapshot_ref)
        if snapshot != published["snapshot"]:
            raise AccessDenied("Current signed dependency observation differs from publication")
        view = self.core.scheduling.explain(self.handle, task.id)
        if (
            snapshot.scope != scope
            or snapshot.source_id != self.source_id
            or snapshot.completeness != "complete"
            or snapshot.prose_digest != task.prose_digest
            or snapshot.canonical_issue_id != task.issue_id
            or snapshot.source_revision != view["source_revision"]
            or snapshot.expires_at != view["expires_at"]
            or snapshot.expires_at <= self.core.context.clock()
            or task.human_hold
            or task.sync_state != "confirmed"
        ):
            raise AccessDenied("Original current provider milestone observation is required")
        prepare = getattr(self.core.context.evidence, "prepare_merge_milestone", None)
        for edge in snapshot.dependencies:
            if edge.condition != "code_merged":
                continue
            predecessor = self.core.get_task(self.handle, edge.predecessor)
            if (
                (task.prose_digest, predecessor.prose_digest) not in self.milestones
                or predecessor.human_hold
                or predecessor.sync_state != "confirmed"
            ):
                raise AccessDenied("Original current predecessor milestone binding is required")
            reference = dict(edge.evidence_refs).get("merge")
            if reference is None:
                continue
            try:
                proof = self.core.context.evidence.verify_evidence(reference)
                if (
                    proof.kind == "merge"
                    and proof.scope == scope
                    and proof.subject == predecessor.id
                    and proof.payload.get("prose_digest") == predecessor.prose_digest
                    and proof.expires_at is not None
                    and proof.expires_at - self.core.context.clock() > 2
                ):
                    continue
            except EvidenceRequired:
                pass
            if not callable(prepare):
                raise EvidenceRequired("The original current merge producer is unavailable")
            if prepare(predecessor.id, reference) != reference:
                raise EvidenceRequired("The original merge milestone reference changed")

    def refresh_for_start(self, task_id, caller_key, *, expected_version, decision_revision):
        with self.publication_boundary(task_id):
            return self._refresh_for_start(
                task_id, caller_key, expected_version=expected_version,
                decision_revision=decision_revision,
            )

    def _refresh_for_start(self, task_id, caller_key, *, expected_version, decision_revision):
        # Only uncommitted starts reach this method. Each fresh verified collection
        # gets its own source revision; a stale receipt is never renewed by replay.
        collection = "worker-dependency-collection:" + str(uuid4())
        prepared = self.prepare(
            task_id,
            collection,
            expected_version=expected_version,
            decision_revision=decision_revision,
        )
        context = self.receipts.lookup(self._scope(), task_id, collection)
        observed_key = "worker-dependency:" + digest(
            {
                "caller": caller_key,
                "source_id": self.source_id,
                "source_revision": context["generation"],
                "receipt": prepared.snapshot_ref,
            }
        )
        view = self.core.scheduling.observe(
            self.handle,
            task_id,
            expected_version,
            decision_revision,
            prepared.snapshot_ref,
            observed_key,
        )
        if view["state"] != "ready":
            raise SchedulingBlocked("; ".join(row["release_condition"] for row in view["blockers"]))
        return view
