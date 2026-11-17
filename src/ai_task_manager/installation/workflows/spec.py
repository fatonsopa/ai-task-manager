"""Closed installation input with physical binding; no imported code or verdicts."""

import json
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.application.workflows.models import WorkflowPlan
from ai_task_manager.config.files import FileSeal, read_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


class BoundFile(FileSeal):
    def __init__(self, path):
        super().__init__(path)
        if self.path.resolve() != self.path or self.path.stat().st_nlink != 1:
            raise AccessDenied("Installed sources require physical unshared files")
        self.original = file_binding(self.path)

    def check(self):
        super().check()
        if file_binding(self.path) != self.original:
            raise AccessDenied("Installed file identity changed")


class Installation:
    def __init__(self, path, config_file, *, clock=time.time, retained=False):
        self.seal = BoundFile(path)
        self.config_seal = BoundFile(config_file)
        data = read_private(path)
        schema = json.loads(Path(__file__).with_name("schema.json").read_text())
        if list(Draft202012Validator(schema).iter_errors(data)):
            raise ValidationError("Installation does not match the closed workflow schema")
        if data["version"] is True or (not retained and data["deadline"] <= clock()):
            raise ValidationError("Installation needs an original finite future deadline")
        if data["installation_path"] != str(self.seal.path):
            raise AccessDenied("Copied installation path does not hold this registration")
        if data["config"] != file_binding(config_file):
            raise AccessDenied("Installation belongs to another configured session file")
        for name in ["repository", "state_root"]:
            p = Path(data[name])
            if not p.is_absolute() or p.resolve() != p:
                raise AccessDenied("Installed roots must be explicit physical paths")
        self.data = data

    def check(self):
        self.seal.check()
        self.config_seal.check()
        if read_private(self.seal.path) != self.data:
            raise AccessDenied("Parsed installation facts changed independently of the file")

    def plan(self, host):
        d = self.data
        return WorkflowPlan(
            d["plan_ref"],
            "assignment",
            host,
            d["source_head"],
            tuple(d["files"]),
            tuple(d["artifacts"]),
            d["deadline"],
            resource_locks=(tuple(tuple(pair) for pair in d["resource_locks"])
                            if "resource_locks" in d else None),
        )


    def members(self, *, retained=False):
        """Resolve only the sealed finite task membership; never discover installations."""
        self.check()
        values = []
        tasks, plans = {self.data["task_id"]}, {self.data["plan_ref"]}
        roots = [Path(self.data["state_root"])]
        for binding in self.data.get("task_installations", ()):
            if file_binding(binding["path"]) != binding:
                raise AccessDenied("Installed task membership changed")
            member = Installation(binding["path"], self.config_seal.path, retained=retained)
            d = member.data
            root = Path(d["state_root"])
            if (d.get("task_installations") or d.get("role", "worker") != "worker"
                    or d["scope"] != self.data["scope"]
                    or d["deadline"] != self.data["deadline"]
                    or d["task_id"] in tasks or d["plan_ref"] in plans
                    or any(root == old or root.is_relative_to(old) or old.is_relative_to(root)
                           for old in roots)):
                raise AccessDenied("Distinct bounded task installations in the same scope are required")
            tasks.add(d["task_id"])
            plans.add(d["plan_ref"])
            roots.append(root)
            values.append(member)
        return tuple(values)
