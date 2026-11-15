"""The actual Technical Writer packet is the semantic review input."""

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.application.intake import prose_digest
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.domains.work.errors import ApprovalRequired, ValidationError
from ai_task_manager.domains.work.models import Scope, TaskProse

from .handoff import check_handoff
from .journal import ApprovalView
from .package import ISSUE_KINDS
from .runner import WriterRunner


class WritingService:
    def __init__(
        self,
        package,
        journal,
        root,
        renderer,
        *,
        reviewer_identity="Current host self-review",
        reviewer_model=None,
    ):
        self.package, self.journal, self.root, self.renderer = (
            package,
            journal,
            Path(root),
            renderer,
        )
        self.reviewer_identity, self.reviewer_model = reviewer_identity, reviewer_model
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ApprovalRequired("Writing sessions require private storage")
        self.runner = WriterRunner(package)

    def approvals_for_tasks(self, scope=None):
        return ApprovalView(self, "task", scope)

    def approvals_for_progress(self, scope, task_id, task_prose_digest):
        return ApprovalView(self, "progress", scope, task_id, task_prose_digest)

    def approvals_for_decisions(self, scope, task_id, task_prose_digest):
        return ApprovalView(self, "decision", scope, task_id, task_prose_digest)

    def _session(self, writing_id, scope):
        record = self.journal._get(writing_id, "writing-session:")
        if record["scope"] != asdict(scope):
            raise ApprovalRequired("Writing session belongs to another scope")
        folder = self.root / record["folder"]
        if folder.is_symlink() or folder.parent.resolve() != self.root.resolve():
            raise ApprovalRequired("Writing session location changed")
        if record["policy"] != asdict(self.package.verify()):
            raise ApprovalRequired("Writing guidance changed; audit and prepare a new session")
        return folder, record

    def prepare(self, scope, kind, prose, *, purpose="task", task=None, decision=None):
        self.package.verify()
        if not (
            (kind in ISSUE_KINDS and purpose in {"task", "progress"})
            or (
                kind == "decision"
                and purpose == "decision"
                and task is not None
                and isinstance(decision, dict)
            )
        ):
            raise ValidationError("Select a supported issue kind and writing purpose")
        if purpose == "progress":
            if task is None:
                raise ValidationError("Progress writing needs its current scoped task")
            prose = TaskProse("Task progress update", prose.body, False, ("progress",))
        rendered = (
            self.renderer.render_decision(prose, decision["recipient"]["url"])
            if purpose == "decision"
            else self.renderer.render(prose)
        )
        prose = TaskProse(rendered.title, rendered.body, prose.code_bearing, prose.required_work)
        document = "# " + prose.title + "\n\n" + prose.body
        # Review the exact provider body, including its terminal-byte contract.
        # Adding a document newline here would review bytes absent from approval.
        folder = self.root / str(uuid4())
        folder.mkdir(mode=0o700)
        draft = folder / "draft.md"
        draft.write_text(document)
        os.chmod(draft, 0o600)
        request = {
            "kind": kind,
            "operation": "create",
            "scope": "complete" if purpose in {"task", "decision"} else "excerpt",
            "sources": [
                {"id": "task-brief", "text": document},
                {
                    "id": "task-metadata",
                    "text": json.dumps(
                        {
                            "kind": kind,
                            "purpose": purpose,
                            "code_bearing": prose.code_bearing,
                            "required_work": list(prose.required_work),
                            "task_id": task.id if task else None,
                        },
                        sort_keys=True,
                    ),
                },
            ],
            "destination": str(folder / "accepted.md"),
            "requirements": [
                {
                    "id": "PRESERVE",
                    "text": (
                        "Preserve the supplied work facts, scope, conditions and exact "
                        "literals. Do not invent results or authority."
                    ),
                }
            ],
            "context": {},
            "limits": {"max_attempts": 3, "max_seconds": 900},
        }
        write_private(folder / "request.json", request)
        self.runner.run(
            folder,
            "prepare",
            "--request",
            folder / "request.json",
            "--session",
            folder / "validator",
        )
        packet = self.runner.run(
            folder, "packet", "--session", folder / "validator", "--draft", draft
        )
        row = {
            "purpose": purpose,
            "kind": kind,
            "scope": asdict(scope),
            "folder": folder.name,
            "policy": asdict(self.package.policy()),
            "prose": asdict(prose),
            "representation": rendered.representation,
            "digest": prose_digest(prose, rendered),
            "document_sha256": hashlib.sha256(document.encode()).hexdigest(),
            "packet_sha256": digest(packet),
            "objective_sha256": packet["objective_sha256"],
            "task_id": task.id if task else None,
            "task_prose_digest": task.prose_digest if task else None,
        }
        if purpose == "task":
            task_type = packet["contract"].get("task_type")
            if task_type not in {"Bug", "Chore", "Decision", "Feature", "Improvement", "Release"}:
                raise ApprovalRequired("Writer issue kind has no supported native Type")
            row["native_task_type"] = task_type
        if purpose == "decision":
            row["decision"] = decision
        write_private(folder / "packet.json", packet)
        ref = self.journal._put("writing-session:", row)
        return {
            "writing_id": ref,
            "packet": packet,
            "mechanical_passed": packet["mechanical_checks"]["passed"],
            "semantic_review_required": True,
        }

    def review(self, scope, writing_id, review):
        folder, record = self._session(writing_id, scope)
        packet = read_private(folder / "packet.json")
        if digest(packet) != record["packet_sha256"]:
            raise ApprovalRequired("Review packet changed")
        reviewer = review.get("reviewer", {})
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("kind") != "current_host_self_review"
            or reviewer.get("identity") != self.reviewer_identity
            or reviewer.get("model") != self.reviewer_model
        ):
            raise ApprovalRequired(
                "Reviewer identity must match this actual host; independent or human review "
                "needs separate observed authority"
            )
        write_private(
            folder / "review-submission.json",
            review,
            replace=(folder / "review-submission.json").exists(),
        )
        result = self.runner.run(
            folder,
            "review",
            "--session",
            folder / "validator",
            "--review",
            folder / "review-submission.json",
        )
        write_private(
            folder / "review-result.json", result, replace=(folder / "review-result.json").exists()
        )
        return result

    def finalize(self, scope, writing_id):
        folder, record = self._session(writing_id, scope)
        result = read_private(folder / "review-result.json")
        if (
            not isinstance(result, dict)
            or result.get("accepted") is not True
            or not isinstance(result.get("review"), str)
        ):
            raise ApprovalRequired("Actual accepted semantic/style review is required")
        review_path = Path(result["review"]).resolve()
        if not review_path.is_relative_to((folder / "validator/reviews").resolve()):
            raise ApprovalRequired("Review receipt location changed")
        delivered = self.runner.run(
            folder,
            "finalize",
            "--session",
            folder / "validator",
            "--draft",
            folder / "draft.md",
            "--review",
            review_path,
        )
        handoff, seals = check_handoff(self, folder, record, delivered, run_verifier=True)
        # Recheck after the actual validator command, before issuing authority.
        self._session(writing_id, scope)
        if check_handoff(self, folder, record, delivered) != (handoff, seals):
            raise ApprovalRequired("Writer delivery changed during verification")
        record = {
            **record,
            "validator_delivery": delivered,
            "writing_id": writing_id,
            "handoff": handoff,
            "delivery_seals": seals,
        }
        ref = self.journal._put("writing-" + record["purpose"] + ":", record)
        return {
            "approval_ref": ref,
            "purpose": record["purpose"],
            "kind": record["kind"],
            "policy": record["policy"],
            "digest": record["digest"],
            "handoff": handoff,
        }

    def verify_delivery(self, record):
        """A current approval cannot outlive its exact checked local artifacts."""
        try:
            folder, original = self._session(record["writing_id"], Scope(**record["scope"]))
            if any(record.get(key) != value for key, value in original.items() if key != "ref"):
                raise ApprovalRequired("Writing approval differs from its original session")
            checked = check_handoff(self, folder, original, record["validator_delivery"])
            if checked != (record["handoff"], record["delivery_seals"]):
                raise ApprovalRequired("Writing approval delivery changed")
        except (KeyError, TypeError, ValueError) as exc:
            raise ApprovalRequired("Current writing approval lacks its checked handoff") from exc
