"""Closed command recipes; operator-selected script references never become host argv."""

import hashlib
import stat
from pathlib import Path

from ai_task_manager.config.files import read_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.installation.workflows.spec import BoundFile

from . import build
from .registry import CommandDefinition

FIELDS = frozenset(
    {
        "version",
        "manifest_path",
        "task_id",
        "prose_digest",
        "docker_executable",
        "docker_sha256",
        "docker_endpoint",
        "endpoint_identity",
        "image_id",
        "commands",
    }
)


class CommandManifest:
    def __init__(self, path, expected_sha256):
        self.seal = BoundFile(path)
        if self.seal.sha256 != expected_sha256:
            raise AccessDenied("Command manifest differs from the installed native source")
        d = read_private(path)
        if (
            type(d) is not dict
            or set(d) != FIELDS
            or type(d["version"]) is not int
            or d["version"] != 1
        ):
            raise ValidationError("Closed command manifest fields required")
        if d["manifest_path"] != str(self.seal.path):
            raise AccessDenied("Copied command registration is not its installed source")
        if type(d["commands"]) is not list or not d["commands"]:
            raise ValidationError("Explicit installed command definitions required")
        definitions = []
        for value in d["commands"]:
            if (
                type(value) is not dict
                or set(value)
                != {"reference", "recipe", "script", "script_sha256", "files", "seconds"}
                or value["recipe"] != "python-script"
            ):
                raise ValidationError("Only the fixed Python script recipe is installed")
            definitions.append(
                CommandDefinition(
                    value["reference"],
                    value["script"],
                    value["script_sha256"],
                    tuple(value["files"]),
                    value["seconds"],
                )
            )
        self.data = d
        self.definitions = tuple(definitions)
        if len({definition.reference for definition in definitions}) != len(definitions):
            raise ValidationError("Command references must be unique")
        for definition in self.definitions:
            definition.plan(d["image_id"])
        self.check()

    def check(self):
        self.seal.check()
        d = self.data
        if read_private(self.seal.path) != d:
            raise AccessDenied("Parsed command registration changed")
        executable = Path(d["docker_executable"])
        if (
            not executable.is_absolute()
            or executable.resolve() != executable
            or not executable.is_file()
            or hashlib.sha256(executable.read_bytes()).hexdigest() != d["docker_sha256"]
        ):
            raise AccessDenied("Installed container executable source changed")
        endpoint = d["docker_endpoint"]
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise AccessDenied("Only an explicit local container capability is supported")
        path = Path(endpoint[7:])
        info = path.lstat()
        actual = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
        if (
            path.is_symlink()
            or path.resolve() != path
            or not stat.S_ISSOCK(info.st_mode)
            or actual != d["endpoint_identity"]
        ):
            raise AccessDenied("Container endpoint identity changed")

    def prepare(self, context):
        self.check()
        if (context.task.id, context.task.prose_digest) != (
            self.data["task_id"],
            self.data["prose_digest"],
        ):
            raise AccessDenied("Command registration belongs to another approved task")
        return build(
            context,
            docker_executable=self.data["docker_executable"],
            docker_endpoint=self.data["docker_endpoint"],
            image_id=self.data["image_id"],
            definitions=self.definitions,
            check_sources=self.check,
        )
