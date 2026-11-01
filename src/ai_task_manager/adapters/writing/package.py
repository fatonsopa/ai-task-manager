"""Pin the actual external skill and its task-type guidance before execution."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

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
