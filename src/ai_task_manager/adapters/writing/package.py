"""Pin the actual external skill and its task-type guidance before execution."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import ApprovalRequired
from ai_task_manager.domains.work.models import ApprovalPolicy

ISSUE_KINDS = tuple(
    "issue_" + name
    for name in (
        "bug",
        "chore",
        "decision",
        "feature",
        "improvement",
        "release",
    )
)
EXCLUDED = {".git", ".venv", ".local", "__pycache__", ".pytest_cache", ".DS_Store"}


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED for part in relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ApprovalRequired("Writer package cannot contain symbolic links")
        if path.is_file():
            result[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def decision_template(value):
    """Validate only the pinned skill's structured decision substitution contract."""
    expected = {"version", "body", "option", "option_labels", "pending_outcome"}
    if (
        type(value) is not dict
        or set(value) != expected
        or value["version"] != "technical-writer-decision-v2"
        or any(type(value[k]) is not str or not value[k] for k in expected)
        or len(value["option_labels"]) < 8
        or len(set(value["option_labels"])) != len(value["option_labels"])
        or not value["option_labels"].isupper()
        or not value["option_labels"].isalpha()
    ):
        raise ApprovalRequired("Pinned Writer decision rendering contract is invalid")
    for key, fields in (
        (
            "body",
            {
                "title",
                "context",
                "options",
                "recommendation",
                "rationale",
                "outcome",
                "recipient_url",
            },
        ),
        ("option", {"id", "label", "description"}),
    ):
        try:
            parts = list(Formatter().parse(value[key]))
        except ValueError as exc:
            raise ApprovalRequired("Writer decision template has invalid substitutions") from exc
        if {name for _, name, _, _ in parts if name is not None} != fields or any(
            spec or conversion for _, _, spec, conversion in parts
        ):
            raise ApprovalRequired("Writer decision template substitutions changed")
    return dict(value)


@dataclass(frozen=True)
class WriterPackage:
    root: Path
    python: str
    pins: dict

    @classmethod
    def capture(cls, root, python):
        """Trusted installer operation; never a model-visible trust update."""
        root = Path(root).resolve()
        return cls(root, str(python), inventory(root))

    def verify(self):
        if not self.root.is_dir() or inventory(self.root) != self.pins:
            raise ApprovalRequired(
                "Writer package changed or is unavailable; audit the new version"
            )
        required = {
            "SKILL.md",
            "contracts/documents.json",
            "scripts/validate_document.py",
            "references/writing_style.md",
            "references/issue.md",
            "examples/issue.md",
        }
        if not required <= self.pins.keys():
            raise ApprovalRequired("Writer package is missing required guidance or runtime")
        return self.policy()

    def decision_template(self):
        self.verify()
        try:
            value = json.loads((self.root / "contracts/documents.json").read_text())["documents"][
                "decision"
            ]["proposal_rendering"]
        except (KeyError, OSError, ValueError) as exc:
            raise ApprovalRequired("Pinned Writer has no structured decision template") from exc
        result = decision_template(value)
        self.verify()
        return result

    def policy(self):
        try:
            contracts = json.loads((self.root / "contracts/documents.json").read_text())[
                "documents"
            ]
            references = {"references/issue.md", "references/bug_report.md", "examples/issue.md"}
            selected = {}
            for kind in ISSUE_KINDS:
                selected[kind] = contracts[kind]
                references.add(contracts[kind]["reference"])
            bundle = {
                "contracts": selected,
                "guidance": {name: self.pins[name] for name in sorted(references)},
            }
            return ApprovalPolicy(
                digest(self.pins),
                digest(bundle),
                self.pins["references/writing_style.md"],
            )
        except (KeyError, OSError, ValueError) as exc:
            raise ApprovalRequired("Writer issue contract bundle is incomplete") from exc
