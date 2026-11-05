"""Explicit prepared-code-candidate review; never a non-code completion issuer."""

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.resources.paths import confined
from ai_task_manager.domains.merging.contracts import sha
from ai_task_manager.domains.work.errors import EvidenceRequired

from .models import CapturedReview, FileWitness, Inspection
from .source import MissingReviews


class CodeCandidateReviews:
    def __init__(self, core, handle, candidates, files, reviews=None):
        if type(files) is not tuple or not files or len(set(files)) != len(files):
            raise EvidenceRequired("Explicit installed candidate review files are required")
        self.core, self.handle, self.candidates, self.files = core, handle, candidates, files
        self.reviews = reviews or MissingReviews()
        self._bound = (core, handle, candidates, files, self.reviews)
        self._prepared = None

    def read(self, task, candidate):
        if any(
            a is not b
            for a, b in zip(
                (self.core, self.handle, self.candidates, self.files, self.reviews), self._bound
            )
        ):
            raise EvidenceRequired("Installed code-candidate review binding changed")
        check = getattr(self.reviews, "check_binding", None)
        if callable(check):
            check()
        _, scope = self.core.session_binding(self.handle)
        current = self.core.get_task(self.handle, task.id)
        grant = self.candidates.grants[candidate.operation_ref]
        binding = grant.binding
        attempt = self.core.get_attempt(self.handle, binding.attempt_id)
        if (
            current != task
            or not task.code_bearing
            or task.human_hold
            or task.delivered_once
            or task.observed_status != "In Progress"
            or task.sync_state != "confirmed"
            or attempt.state != "done"
            or attempt.task_id != task.id
            or (binding.task_id, binding.principal_id, binding.project_id, binding.session_id)
            != (task.id, scope.principal, scope.project, scope.logical_session)
            or binding.prose_digest != task.prose_digest
        ):
            raise EvidenceRequired("Exact current completed code-task authority is required")
        observed = self.candidates.reconcile(candidate.operation_ref)
        if observed != candidate or observed.status != "ready":
            raise EvidenceRequired("Exact current prepared candidate is required for review")
        root = Path(candidate.path)
        changed = self.candidates.profile.query(
            root, "diff", "--name-only", "-z", grant.target_head, candidate.commit
        ).stdout
        if not {name.decode("utf-8") for name in changed.split(b"\0") if name} <= set(self.files):
            raise EvidenceRequired("Candidate review scope omits changed files")
        witnesses = [
            FileWitness.capture(root, identity_only=True),
            FileWitness.capture(root / ".git"),
        ]
        artifacts = {}
        for name in self.files:
            path = confined(root, name)
            witness = FileWitness.capture(path)
            info = path.stat()
            if (
                not path.is_file()
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o022
            ):
                raise EvidenceRequired("Candidate review files must be owned unshared artifacts")
            with path.open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise EvidenceRequired("Candidate review artifact exceeds its bounded size")
            artifacts[name] = hashlib.sha256(raw).hexdigest()
            witness.check()
            witnesses.append(witness)
        common = Path(self.candidates.profile.source["common"])
        for reference in (
            candidate.candidate_ref,
            grant.source_ref,
            self.candidates.profile.source["target_ref"],
            "packed-refs",
            "config",
        ):
            witnesses.append(FileWitness.capture(common / reference))
        context = {
            "scope": asdict(scope),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "required_work": list(task.required_work),
            "author": {
                "attempt_id": attempt.id,
                "worker_id": attempt.worker_id,
                "host": attempt.host,
            },
            "inspection": {
                "candidate": asdict(candidate),
                "artifacts": artifacts,
                "worktree": {"path": str(root), "common": str(common)},
            },
            "source_version": {
                "head": candidate.commit,
                "tree": candidate.tree,
                "grant_digest": grant.digest,
            },
            "criteria": [[unit, task.title + "\n\n" + task.body] for unit in task.required_work],
        }
        inspection = Inspection(
            json.dumps(context, sort_keys=True, separators=(",", ":")), tuple(witnesses)
        )
        inspection.check()
        captured = self.reviews.resolve(inspection)
        if captured is None:
            self._prepared = None
            return None
        if (
            type(captured) is not CapturedReview
            or captured.context_digest != inspection.digest
            or captured.reviewer_id == attempt.worker_id
            or not captured.accepted(tuple(task.required_work), self.core.context.clock())
        ):
            raise EvidenceRequired("Independent exact-candidate captured acceptance is required")
        sha(captured.trace_sha256)
        sha(captured.source_digest)
        inspection.check()
        for witness in captured.witnesses:
            witness.check()
        result = {
            "review_ref": captured.reference,
            "reviewer_id": captured.reviewer_id,
            "context_digest": inspection.digest,
            "candidate_commit": candidate.commit,
            "candidate_tree": candidate.tree,
            "trace_sha256": captured.trace_sha256,
            "capture_sha256": captured.source_digest,
        }
        self._prepared = (dict(result), inspection, captured)
        return result

    def check_prepared(self, commit):
        if self._prepared is None:
            raise EvidenceRequired("Exact candidate review must be prepared outside publication")
        result, inspection, captured = self._prepared
        if result["candidate_commit"] != commit or self.core.context.clock() >= captured.expires_at:
            raise EvidenceRequired("Prepared candidate review is stale or expired")
        inspection.check()
        for witness in captured.witnesses:
            witness.check()
