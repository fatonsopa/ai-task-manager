"""Observe actual owned repository/files; no model-selected paths or expected answers."""

import hashlib
import subprocess

from ai_task_manager.adapters.resources.paths import confined
from ai_task_manager.domains.checks.models import fingerprint, identifier, repo_path
from ai_task_manager.domains.work.errors import EvidenceRequired


class RepositoryInspector:
    def __init__(self, runtime):
        self.runtime = runtime

    def inspect(self, task, attempt, plan):
        pair = self.runtime.pair(attempt.id)
        fact = self.runtime.ownership(attempt.id)
        if (
            pair.attempt_id != attempt.id
            or attempt.worktree_id != pair.worktree_id
            or attempt.database_id != pair.database_id
            or fact["head"] != plan.base
        ):
            raise EvidenceRequired(
                "Source or resource lineage changed; reviewed continuation required"
            )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(pair.worktree),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            timeout=5,
        )
        if result.returncode or len(result.stdout) > 262144:
            raise EvidenceRequired("Actual changed-file inventory is unavailable")
        files = []
        for row in result.stdout.split(b"\0"):
            if not row:
                continue
            text = row.decode("utf-8")
            if len(text) < 4 or text[2] != " " or any(c in text[:2] for c in "RC"):
                raise EvidenceRequired("Unsupported or ambiguous changed-file inventory")
            path = text[3:]
            identifier(path)
            repo_path(path)
            files.append(path)
        if not set(files) <= set(plan.allowed_files):
            raise EvidenceRequired("Changed files exceed the trusted workflow scope")
        artifacts = {}
        artifact_facts = {}
        for name in plan.artifacts:
            path = confined(pair.worktree, name)
            if not path.is_file() or path.stat().st_size > 262144:
                raise EvidenceRequired("Required non-code artifact is missing or too large")
            raw = path.read_bytes()
            if not raw.strip():
                raise EvidenceRequired("Required non-code artifact is empty")
            raw.decode("utf-8")
            artifacts[name] = hashlib.sha256(raw).hexdigest()
            info = path.stat()
            artifact_facts[name] = {
                "path": str(path),
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": len(raw),
                "sha256": artifacts[name],
            }
        observation = {
            "task_id": task.id,
            "attempt_id": attempt.id,
            "prose_digest": task.prose_digest,
            "files": sorted(files),
            "artifacts": artifacts,
            "artifact_facts": artifact_facts,
            "worktree": fact,
            "source_head": fact["head"],
            "resource_ids": [pair.worktree_id, pair.database_id],
        }
        return dict(observation, digest=fingerprint(observation))
