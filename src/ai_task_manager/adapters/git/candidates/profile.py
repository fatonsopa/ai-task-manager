"""Candidate-only guarded Git configuration; final publication keeps its own policy."""

import os
import threading
from pathlib import Path

from ai_task_manager.adapters.git.profile import file_digest, physical
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied


class CandidateProfile:
    def __init__(self, profile, worktree_id, repository_id, root, *, required_hooks=()):
        self.profile, self.worktree_id, self.repository_id = profile, worktree_id, repository_id
        self.root = Path(root).absolute()
        self.root_identity = physical(self.root)
        if profile.root not in self.root.parents:
            raise AccessDenied("Candidate root must be inside the configured isolated root")
        self.source = profile.validate(worktree_id, repository_id)
        if self.root == Path(self.source["path"]) or Path(self.source["path"]) in self.root.parents:
            raise AccessDenied("Candidates cannot occupy the worker worktree")
        self.hooks = Path(self.source["common"]) / "hooks"
        self.required_hooks = tuple(required_hooks)
        if any(
            name not in {"pre-commit", "prepare-commit-msg", "commit-msg", "pre-merge-commit"}
            for name in self.required_hooks
        ):
            raise AccessDenied("Unsupported required commit guard")
        self._hooks = self._manifest()
        self._lock = threading.RLock()
        self.records = {}
        self.digest = digest(
            {
                "source": self.source,
                "hooks": self._hooks,
                "required": self.required_hooks,
                "root": str(self.root),
                "physical": self.root_identity,
            }
        )

    def _manifest(self):
        physical(self.hooks)
        rows = []
        for path in sorted(self.hooks.iterdir()):
            ident = physical(path, directory=False)
            rows.append((path.name, ident, path.stat().st_mode, file_digest(path)))
        for name in self.required_hooks:
            path = self.hooks / name
            if not path.is_file() or not os.access(path, os.X_OK):
                raise AccessDenied("A required configured commit guard is unavailable")
        return rows

    def validate(self):
        with self._lock:
            if physical(self.root) != self.root_identity or self._manifest() != self._hooks:
                raise AccessDenied("Candidate root or commit guards changed")
            if self.profile.validate(self.worktree_id, self.repository_id) != self.source:
                raise AccessDenied("Configured source repository changed")

    def query(self, path, *args, ok=(0,)):
        with self._lock:
            return self.profile.query(path, *args, ok=ok)

    def refs(self, grant):
        self.validate()
        branch = self.query(self.source["path"], "symbolic-ref", "HEAD").stdout.decode().strip()
        if branch != grant.source_ref:
            raise AccessDenied("Candidate source is not the owned worker branch")
        source = self.profile.reference(self.source["path"], grant.source_ref)
        target = self.profile.reference(self.source["path"], self.source["target_ref"])
        return source, target

    def argv(self, path, *args):
        self.validate()
        with self._lock:
            original = self.profile.argv(path, *args)
            output = []
            index = 0
            while index < len(original):
                if original[index] == "-c" and original[index + 1] == "commit.gpgSign=false":
                    index += 2
                    continue
                value = original[index]
                if value == "core.hooksPath=/dev/null":
                    value = "core.hooksPath=" + str(self.hooks)
                output.append(value)
                index += 1
            return output

    def environment(self, path, marker):
        with self._lock:
            return dict(
                self.profile.environment_for(path),
                GIT_REFLOG_ACTION=marker,
                GIT_MERGE_AUTOEDIT="no",
                GIT_EDITOR=":",
            )

    def enroll(self, path, ref):
        with self._lock:
            identifier = "candidate:" + ref.rsplit("/", 1)[-1]
            if identifier not in self.profile.repositories:
                self.profile.enroll(
                    path,
                    worktree_id=identifier,
                    repository_id=self.repository_id,
                    target_ref=self.source["target_ref"],
                )
            self.records[str(path)] = (
                identifier,
                self.profile.validate(identifier, self.repository_id),
            )
            record = self.records[str(path)][1]
            if (record["common"], record["common_identity"]) != (
                self.source["common"],
                self.source["common_identity"],
            ):
                raise AccessDenied("Candidate belongs to a different physical repository")
            return self.records[str(path)][1]

    def verify_candidate(self, path, ref, physical_identity):
        if physical(path) != tuple(physical_identity):
            raise AccessDenied("Candidate physical identity changed")
        record = self.enroll(path, ref)
        head = self.query(path, "symbolic-ref", "HEAD").stdout.decode().strip()
        if head != ref:
            raise AccessDenied("Candidate branch ownership changed")
        return record

    @staticmethod
    def identity(path):
        return physical(path)
