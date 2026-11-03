"""Prepare isolated guarded candidates; no checks, database allocation or publication."""

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.domains.merging.candidates import CandidateResult
from ai_task_manager.domains.work.errors import AccessDenied

from .store import CandidateStore


class CandidateService:
    def __init__(self, journal, profile, authority, *, process_factory, faults=None):
        if authority is None or process_factory is None:
            raise AccessDenied(
                "Independent merge authority and configured Git transport are required"
            )
        self.store, self.profile, self.authority = CandidateStore(journal), profile, authority
        self.grants = {}
        self.runner = process_factory(self.store, profile, self.boundary, faults=faults)

    def _refs(self, grant):
        source, target = self.profile.refs(grant)
        return (
            "source_moved"
            if source != grant.source_head
            else "target_moved"
            if target != grant.target_head
            else None
        )

    @contextmanager
    def boundary(self, ref):
        grant = self.grants[ref]
        self.store.owner(ref)
        self.profile.validate()
        if self.store.get(ref)["stage"] not in {"remove", "delete_ref"} and self._refs(grant):
            raise AccessDenied("Committed source or target changed")
        self.authority.check_current(grant)
        self.store.owner(ref)
        with self.authority.boundary(grant):
            yield

    def _result(self, ref, status, reason=None, commit=None, tree=None):
        data = self.store.get(ref)
        grant = self.grants[ref]
        result = CandidateResult(
            ref,
            status,
            grant.source_head,
            grant.target_head,
            data["branch"],
            data["path"],
            commit,
            tree,
            reason,
        )
        self.store.write(ref, dict(data, result=asdict(result), stage=status), status)
        return result

    def _complete(self, ref, name):
        row = self.store.get(ref)["effects"].get(name)
        if row is None:
            return None
        result = row.get("result")
        if not result or not result["group"]["confirmed_stop"]:
            return False
        return result["returncode"] == 0

    def prepare(self, grant):
        grant.validate()
        self.authority.registration(grant)
        self.authority.check_current(grant)
        if grant.binding.configuration_digest != self.profile.source["configuration_digest"]:
            raise AccessDenied("Candidate binding differs from configured repository")
        ref, created = self.store.create(grant, self.profile)
        self.grants[ref] = grant
        data = self.store.get(ref)
        if data.get("result") and data["result"]["status"] in {
            "ready",
            "conflict",
            "refused",
            "cleaned",
        }:
            return self.reconcile(ref)
        try:
            self.store.owner(ref)
            moved = self._refs(grant)
            if moved:
                return self._result(ref, moved)
            path = Path(data["path"])
            branch = data["branch"]
            stage = self._complete(ref, "worktree")
            if stage is None:
                if (
                    path.exists()
                    or self.profile.query(
                        self.profile.source["path"],
                        "show-ref",
                        "--verify",
                        "--quiet",
                        branch,
                        ok=(0, 1),
                    ).returncode
                    == 0
                ):
                    raise AccessDenied("An unrelated resource occupies the candidate destination")
                self.runner.run(
                    ref,
                    "worktree",
                    self.profile.source["path"],
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch.removeprefix("refs/heads/"),
                        str(path),
                        grant.target_head,
                    ],
                )
                stage = self._complete(ref, "worktree")
            if not stage:
                return self._result(ref, "uncertain", "worktree_setup_unresolved")
            data = self.store.get(ref)
            if "resource" not in data:
                record = self.profile.enroll(path, branch)
                if (
                    self.profile.query(path, "rev-parse", "HEAD").stdout.decode().strip()
                    != grant.target_head
                ):
                    raise AccessDenied("Created candidate source differs from its intent")
                self.store.write(
                    ref,
                    dict(
                        data, resource={"physical": self.profile.identity(path), "record": record}
                    ),
                    "resource",
                )
            self._verify_resource(ref)
            stage = self._complete(ref, "merge")
            if stage is None:
                self.runner.run(
                    ref,
                    "merge",
                    path,
                    ["merge", "--no-ff", "--no-commit", "--no-edit", grant.source_head],
                )
                stage = self._complete(ref, "merge")
            if not stage:
                row = self.store.get(ref)["effects"]["merge"].get("result")
                if not row or not row["group"]["confirmed_stop"]:
                    return self._result(ref, "uncertain", "merge_process_unresolved")
                conflict = bool(self.profile.query(path, "ls-files", "--unmerged", "-z").stdout)
                return self._result(ref, "conflict" if conflict else "refused", "merge_failed")
            merge_head = self.profile.query(
                path, "rev-parse", "--verify", "-q", "MERGE_HEAD", ok=(0, 1, 128)
            )
            if merge_head.returncode == 0:
                stage = self._complete(ref, "commit")
                if stage is None:
                    self.runner.run(
                        ref,
                        "commit",
                        path,
                        [
                            "commit",
                            "-m",
                            "Merge task " + grant.binding.task_id + " (" + data["generation"] + ")",
                        ],
                    )
                    stage = self._complete(ref, "commit")
                if not stage:
                    return self._result(
                        ref,
                        "refused"
                        if self.store.get(ref)["effects"]["commit"]
                        .get("result", {})
                        .get("group", {})
                        .get("confirmed_stop")
                        else "uncertain",
                        "commit_guard_or_process_failed",
                    )
            return self._ready(ref)
        except Exception as error:
            self.runner.reconcile(ref)
            return self._result(ref, "uncertain", type(error).__name__)

    def _verify_resource(self, ref):
        data = self.store.get(ref)
        if "resource" not in data:
            raise AccessDenied("Candidate resource ownership is unobserved")
        record = self.profile.verify_candidate(
            Path(data["path"]), data["branch"], data["resource"]["physical"]
        )
        if record["configuration_digest"] != data["resource"]["record"]["configuration_digest"]:
            raise AccessDenied("Candidate physical Git routing changed")
        return record

    def _ready(self, ref):
        data = self.store.get(ref)
        path = Path(data["path"])
        grant = self.grants[ref]
        self._verify_resource(ref)
        for effect in data["effects"].values():
            if not effect.get("result", {}).get("group", {}).get("confirmed_stop"):
                return self._result(ref, "uncertain", "owned_child_unresolved")
        if self.profile.query(path, "status", "--porcelain", "-z", "--untracked-files=all").stdout:
            return self._result(ref, "refused", "candidate_not_clean")
        commit = self.profile.query(path, "rev-parse", "HEAD").stdout.decode().strip()
        tree = self.profile.query(path, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        for ancestor in (grant.source_head, grant.target_head):
            if self.profile.query(
                path, "merge-base", "--is-ancestor", ancestor, commit, ok=(0, 1)
            ).returncode:
                return self._result(ref, "refused", "candidate_lineage_differs", commit, tree)
        moved = self._refs(grant)
        return self._result(ref, moved or "ready", commit=commit, tree=tree)

    def reconcile(self, ref, grant=None):
        if grant is not None:
            self.grants[ref] = grant
        grant = self.grants[ref]
        self.authority.registration(grant)
        self.store.owner(ref)
        self.runner.reconcile(ref)
        data = self.store.get(ref)
        old = data.get("result")
        if old and old["status"] == "cleaned":
            return CandidateResult(**old)
        if old and old["status"] in {"ready", "target_moved", "source_moved"}:
            self._verify_resource(ref)
            moved = self._refs(grant)
            if moved:
                return self._result(ref, moved, commit=old["commit"], tree=old["tree"])
            return CandidateResult(**old)
        return self._result(ref, "uncertain", "candidate_requires_guarded_resume")

    def cleanup(self, ref):
        grant = self.grants[ref]
        self.authority.check_current(grant)
        self.store.owner(ref)
        data = self.store.get(ref)
        result = data.get("result", {})
        if result.get("status") == "cleaned":
            return CandidateResult(**result)
        if result.get("status") not in {"ready", "target_moved", "source_moved"} or not result.get(
            "commit"
        ):
            raise AccessDenied("Incomplete/conflicting candidate resources must be retained")
        path = Path(data["path"])
        self._verify_resource(ref)
        if self.profile.query(path, "status", "--porcelain", "-z", "--untracked-files=all").stdout:
            raise AccessDenied("Dirty candidate cannot be removed")
        if (
            self.profile.query(path, "rev-parse", "HEAD").stdout.decode().strip()
            != result["commit"]
        ):
            raise AccessDenied("Candidate reference changed before cleanup")
        self.runner.run(
            ref, "remove", self.profile.source["path"], ["worktree", "remove", str(path)]
        )
        if not self._complete(ref, "remove"):
            raise AccessDenied("Candidate removal is unresolved")
        self.runner.run(
            ref,
            "delete_ref",
            self.profile.source["path"],
            ["update-ref", "-d", data["branch"], result["commit"]],
        )
        if not self._complete(ref, "delete_ref"):
            raise AccessDenied("Candidate ref removal is unresolved")
        return self._result(ref, "cleaned", commit=result["commit"], tree=result["tree"])
