"""Prepare current artifact/source witnesses outside Core proof consumption."""

import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from ai_task_manager.adapters.resources.paths import confined
from ai_task_manager.domains.work.errors import EvidenceRequired

from .models import FileWitness, Inspection


class CurrentArtifacts:
    def __init__(self, core, handle, store, inspector, runtime, plans, criteria):
        self.core, self.handle, self.store = core, handle, store
        self.inspector, self.runtime = inspector, runtime
        self.plans = MappingProxyType(dict(plans))
        if any(
            type(value) not in {tuple, list}
            or not value
            or any(
                type(c) not in {tuple, list}
                or len(c) != 2
                or not all(isinstance(x, str) and x.strip() for x in c)
                for c in value
            )
            or len({c[0] for c in value}) != len(value)
            for value in criteria.values()
        ):
            raise EvidenceRequired("Trusted distinct criteria and descriptions are required")
        self.criteria = MappingProxyType(
            {key: tuple(tuple(c) for c in value) for key, value in criteria.items()}
        )
        self._bound = (core, handle, store, inspector, runtime, self.plans, self.criteria)

    def check_binding(self):
        current = (
            self.core,
            self.handle,
            self.store,
            self.inspector,
            self.runtime,
            self.plans,
            self.criteria,
        )
        if any(a is not b for a, b in zip(current, self._bound)):
            raise EvidenceRequired("The configured artifact observer changed")

    def prepare(self, scope, task, artifact_digest):
        self.check_binding()
        _, actual_scope = self.core.session_binding(self.handle)
        if actual_scope != scope:
            raise EvidenceRequired("Review scope does not match this session")
        current = self.core.get_task(self.handle, task.id)
        if current != task or task.code_bearing or task.human_hold or task.delivered_once:
            raise EvidenceRequired("Current non-code task authority is required")
        if task.sync_state != "confirmed" or task.observed_status not in {
            "In Progress",
            "In Review",
        }:
            raise EvidenceRequired("Task is not ready for technical acceptance")
        saved = self.store.verified(scope, task.id, artifact_digest)
        if saved is None:
            return None
        if (saved.get("task_id"), saved.get("prose_digest"), saved.get("digest")) != (
            task.id,
            task.prose_digest,
            artifact_digest,
        ):
            raise EvidenceRequired("Historical inspection differs from the current task")
        if task.id not in self.plans or task.id not in self.criteria:
            raise EvidenceRequired("Trusted workflow plan and acceptance criteria are missing")
        attempt = self.core.get_attempt(self.handle, saved["attempt_id"])
        if attempt.task_id != task.id or attempt.state != "done":
            raise EvidenceRequired("A genuinely completed owned attempt is required")
        plan = self.plans[task.id]
        fresh = self.inspector.inspect(task, attempt, plan)
        if fresh != saved:
            raise EvidenceRequired("Current physical inspection differs from the saved result")
        pair = self.runtime.pair(attempt.id)
        if [attempt.worktree_id, attempt.database_id] != saved["resource_ids"] or (
            pair.worktree_id != attempt.worktree_id
            or pair.database_id != attempt.database_id
            or str(pair.worktree) != saved["worktree"]["path"]
        ):
            raise EvidenceRequired("Current physical resource ownership differs")
        witnesses = [
            FileWitness.capture(pair.worktree, identity_only=True),
            FileWitness.capture(pair.database, identity_only=True),
        ]
        for name, expected in saved["artifacts"].items():
            path = confined(pair.worktree, name)
            witness = FileWitness.capture(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o022
                ):
                    raise EvidenceRequired(
                        "Reviewed artifact must be an owned unshared regular file"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(262145)
                fact = saved["artifact_facts"][name]
                if (
                    len(raw) > 262144
                    or not raw.strip()
                    or hashlib.sha256(raw).hexdigest() != expected
                    or (info.st_dev, info.st_ino, len(raw), expected, str(path))
                    != (fact["device"], fact["inode"], fact["size"], fact["sha256"], fact["path"])
                ):
                    raise EvidenceRequired("Reviewed artifact bytes or identity differ")
                raw.decode("utf-8")
            finally:
                os.close(descriptor)
            witness.check()
            witnesses.append(witness)
        common = Path(fresh["worktree"]["common"])
        pointer = confined(pair.worktree, ".git")
        pointer_witness = FileWitness.capture(pointer)
        text = pointer.read_text().strip()
        if not text.startswith("gitdir: "):
            raise EvidenceRequired("Only configured linked worktree metadata is supported")
        gitdir = Path(text[8:]).resolve()
        if not gitdir.is_relative_to(common / "worktrees"):
            raise EvidenceRequired("Worktree metadata escaped its trusted repository")
        branch_ref = confined(common, "refs/heads/" + fresh["worktree"]["branch"])
        branch_witness = FileWitness.capture(branch_ref)
        if branch_ref.read_text().strip() != fresh["source_head"]:
            raise EvidenceRequired("Current source head differs from the inspected head")
        head = gitdir / "HEAD"
        head_witness = FileWitness.capture(head)
        if head.read_text().strip() != "ref: refs/heads/" + fresh["worktree"]["branch"]:
            raise EvidenceRequired("Current source branch differs from the inspected branch")
        witnesses += [
            pointer_witness,
            FileWitness.capture(common, identity_only=True),
            branch_witness,
            head_witness,
            FileWitness.capture(gitdir / "commondir"),
            FileWitness.capture(common / "packed-refs"),
            FileWitness.capture(common / "config"),
        ]
        for witness in witnesses:
            witness.check()
        context = {
            "scope": asdict(scope),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "required_work": list(task.required_work),
            "inspection": saved,
            "source_version": {"head": saved["source_head"], "plan_digest": plan.digest},
            "criteria": [list(c) for c in self.criteria[task.id]],
        }
        return Inspection(
            json.dumps(context, sort_keys=True, separators=(",", ":")), tuple(witnesses)
        )
