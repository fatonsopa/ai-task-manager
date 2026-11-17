"""Prepare isolated guarded candidates; no checks, database allocation or publication."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID

from ai_task_manager.config.files import digest
from ai_task_manager.domains.merging.candidates import CandidateResult
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict

from .store import CandidateStore


class CandidateService:
    def __init__(self, journal, profile, authority, *, process_factory, faults=None):
        if authority is None or process_factory is None:
            raise AccessDenied(
                "Independent merge authority and configured Git transport are required"
            )
        self.store, self.profile, self.authority = CandidateStore(journal), profile, authority
        self.grants = {}
        self.successor_grants = {}
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
        if self.store.get(ref)["stage"] in {"remove", "delete_ref"}:
            self.profile.validate()
        elif self._refs(grant):
            raise AccessDenied("Committed source or target changed")
        self.authority.check_current(grant)
        self.authority.check_source(grant, self.profile.source)
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

    def prepare(self, grant, *, _predecessor=None):
        grant.validate()
        self.authority.registration(grant)
        self.authority.check_current(grant)
        self.profile.validate()
        self.authority.check_source(grant, self.profile.source)
        if (
            grant.binding.worktree_id,
            grant.binding.repository_id,
            grant.binding.configuration_digest,
        ) != (
            self.profile.worktree_id,
            self.profile.repository_id,
            self.profile.source["configuration_digest"],
        ):
            raise AccessDenied("Candidate binding differs from configured repository")
        self.profile.refs(grant)
        ref, created = self.store.create(grant, self.profile, predecessor=_predecessor)
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

    def target_successor(self, previous, selected, *, validate_only=False):
        """Transfer only a known quiescent pre-publication candidate claim."""
        expected = replace(
            previous,
            target_head=selected.target_head,
            binding=replace(previous.binding, operation_key=selected.binding.operation_key),
        )
        selected_key = "candidate-target:" + digest(
            [asdict(previous.binding), selected.target_head]
        )
        if (
            selected != expected
            or selected.target_head == previous.target_head
            or selected.binding.operation_key != selected_key
        ):
            raise AccessDenied("Target successor cannot change original worker authority")
        self.authority.check_current(previous)
        self.profile.validate()
        self.authority.check_source(previous, self.profile.source)
        if self.profile.refs(previous) != (previous.source_head, selected.target_head):
            raise AccessDenied("Successor target must be the currently observed trusted target")
        if self.profile.query(
            self.profile.source["path"],
            "merge-base",
            "--is-ancestor",
            previous.target_head,
            selected.target_head,
            ok=(0, 1),
        ).returncode:
            raise AccessDenied("Target recovery requires forward descendant lineage")
        operations = self.store.journal.for_attempt(previous.binding.attempt_id)
        if any(
            op["facts"].get("publication_intent") or op["facts"].get("local_git")
            for op in operations
        ):
            raise AccessDenied("Publication intent or unknown publication forbids target replan")
        existing = [
            op["ref"]
            for op in operations
            if op["facts"].get("candidate", {}).get("request")
            == {"grant": asdict(selected), "profile": self.profile.digest}
        ]
        if existing:
            if len(existing) != 1:
                raise AccessDenied("Candidate successor ownership is ambiguous")
            return None if validate_only else self.prepare(selected)
        prior = [
            op["ref"]
            for op in operations
            if op["facts"].get("candidate", {}).get("request")
            == {"grant": asdict(previous), "profile": self.profile.digest}
        ]
        if len(prior) > 1:
            raise AccessDenied("Candidate predecessor ownership is ambiguous")
        predecessor = prior[0] if prior else None
        if predecessor is not None:
            data = self.store.get(predecessor)
            if any(
                not effect.get("result", {}).get("group", {}).get("confirmed_stop")
                for effect in data["effects"].values()
            ):
                raise AccessDenied("Unresolved predecessor work retains candidate ownership")
            pending = [
                op
                for op in operations
                if op["binding"] == asdict(selected.binding)
                and op["fingerprint"] == selected.binding.fingerprint
                and not op["facts"]
            ]
            claim = self.store.journal.current(data["claim"])["facts"]
            owner = claim.get("candidate_owner", {})
            if pending and owner.get("operation") == pending[0]["ref"]:
                prior_owner = {
                    "operation": predecessor,
                    "generation": data["generation"],
                    "owner_ref": previous.owner_ref,
                }
                try:
                    generation_ok = str(UUID(owner["generation"])) == owner["generation"]
                except (KeyError, ValueError, TypeError):
                    generation_ok = False
                if (
                    len(pending) != 1
                    or not generation_ok
                    or set(owner) != {"operation", "owner_ref", "generation"}
                    or owner["owner_ref"] != selected.owner_ref
                    or claim.get("candidate_owner_history", [])[-1:] != [prior_owner]
                    or data.get("result", {}).get("status") != "target_moved"
                ):
                    raise AccessDenied("Pending candidate successor ownership differs")
                if "resource" in data:
                    self._verify_resource(predecessor)
                return None if validate_only else self.prepare(selected)
            self.store.owner(predecessor)
            observed = self.reconcile(predecessor, previous)
            if observed.status != "target_moved":
                raise AccessDenied("Only a known target-moved candidate permits successor")
        if validate_only:
            return None
        return self.prepare(selected, _predecessor=predecessor)

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

    def link_successor(self, reference, original, selected):
        """Use a fresh task grant for the same retained owned candidate inputs."""
        if (
            replace(
                selected,
                task_version=original.task_version,
                attempt_version=original.attempt_version,
                decision_revision=original.decision_revision,
                decision_ref=original.decision_ref,
            )
            != original
        ):
            raise AccessDenied("Successor candidate cannot change source, target or ownership")
        self.authority.check_current(selected)
        self.store.owner(reference)
        if self.store.get(reference)["request"] != {
            "grant": asdict(original),
            "profile": self.profile.digest,
        }:
            raise AccessDenied("Original candidate grant differs from retained history")
        self.successor_grants[reference] = (original, selected)
        return self.reconcile(reference, selected)

    def reconcile(self, ref, grant=None):
        stored = self.store.get(ref)
        selected = grant if grant is not None else self.grants[ref]
        recorded = selected
        linked = self.successor_grants.get(ref)
        if linked is not None:
            original, current = linked
            if selected != current:
                raise AccessDenied("Current successor candidate authority changed")
            recorded = original
        if stored["request"] != {"grant": asdict(recorded), "profile": self.profile.digest}:
            raise IdempotencyConflict("Candidate recovery context differs from durable ownership")
        if grant is not None:
            self.grants[ref] = grant
        grant = self.grants[ref]
        self.authority.registration(grant)
        data = self.store.get(ref)
        old = data.get("result")
        if old and old["status"] == "cleaned":
            return CandidateResult(**old)
        self.store.owner(ref)
        self.runner.reconcile(ref)
        if old and old["status"] in {"conflict", "refused"}:
            return CandidateResult(**old)
        if old and old["status"] in {"ready", "target_moved", "source_moved"}:
            self._verify_resource(ref)
            path = Path(data["path"])
            if (
                self.profile.query(path, "rev-parse", "HEAD").stdout.decode().strip()
                != old["commit"]
            ):
                return self._result(
                    ref, "uncertain", "candidate_ref_changed", old["commit"], old["tree"]
                )
            if self.profile.query(
                path, "status", "--porcelain", "-z", "--untracked-files=all"
            ).stdout:
                return self._result(ref, "refused", "candidate_changed", old["commit"], old["tree"])
            moved = self._refs(grant)
            if moved:
                return self._result(ref, moved, commit=old["commit"], tree=old["tree"])
            return CandidateResult(**old)
        return self._result(ref, "uncertain", "candidate_requires_guarded_resume")

    def cleanup(self, ref):
        """Cleanup awaits independent retirement/publication verification.

        Confirmed delivery and its fence must be recorded independently. A ready
        candidate alone cannot prove its unique output is retained elsewhere.
        """
        grant = self.grants[ref]
        self.authority.registration(grant)
        raise EvidenceRequired(
            "Confirmed publication and scoped retirement authority are not configured; "
            "candidate resources and provenance remain retained"
        )
