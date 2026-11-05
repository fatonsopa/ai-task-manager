"""Read-only native intent; this file is never evidence of actual execution."""

import hashlib
import os
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile, clean_environment
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.installation.workflows.spec import BoundFile

MODELS = {
    "claude": frozenset({"claude-opus-5-5", "claude-sonnet-5-5"}),
    "codex": frozenset({"gpt-6.1-sol", "gpt-6-astra"}),
}
FIELDS = frozenset(
    {
        "version",
        "manifest_path",
        "installation",
        "config",
        "native_ref",
        "host",
        "model",
        "effort",
        "account_hash",
        "executable",
        "executable_sha256",
        "home",
        "profile",
        "authorization_ref",
    }
)


def installation_intent(path):
    # The registration digest is the derived output, never its own hash input.
    value = read_private(path)
    return {
        "path": str(Path(path).absolute()),
        "intent_sha256": digest({k: v for k, v in value.items() if k != "native_digest"}),
    }


class NativeManifest:
    def __init__(self, path, config_file, installation_file):
        self.seal = BoundFile(path)
        self.config = BoundFile(config_file)
        self.installation = BoundFile(installation_file)
        d = read_private(path)
        if (
            type(d) is not dict
            or not FIELDS <= set(d)
            or set(d) - FIELDS - {"commands_manifest", "authority_store", "code_manifest"}
            or type(d["version"]) is not int
            or d["version"] != 1
        ):
            raise ValidationError("Native intent fields or version are incomplete")
        if d["manifest_path"] != str(self.seal.path):
            raise AccessDenied("Copied native manifest is not its installed path")
        if d["config"] != file_binding(config_file) or d["installation"] != installation_intent(
            installation_file
        ):
            raise AccessDenied("Native intent belongs to another installation/configuration")
        if (
            type(d["host"]) is not str
            or type(d["model"]) is not str
            or d["host"] not in MODELS
            or d["model"] not in MODELS[d["host"]]
            or d["effort"] != "high"
            or any(
                type(d[k]) is not str
                or len(d[k]) != 64
                or any(c not in "0123456789abcdef" for c in d[k])
                for k in ("account_hash", "executable_sha256")
            )
            or any(type(d[k]) is not str or not d[k] for k in ("native_ref", "authorization_ref"))
        ):
            raise AccessDenied("Exact authorized account/model/High profile is required")
        for name in ("commands_manifest", "code_manifest"):
            self._pointer(d.get(name))
        self.data = d
        self.profile = NativeProfile(d["model"], d["effort"])
        self._paths(d)

    @staticmethod
    def _pointer(pointer):
        if pointer is not None and (
            type(pointer) is not dict
            or set(pointer) != {"path", "sha256"}
            or type(pointer["path"]) is not str
            or type(pointer["sha256"]) is not str
        ):
            raise ValidationError("Exact installed command manifest reference required")

    def _paths(self, d):
        for name in ("home", "profile", "executable"):
            p = Path(d[name])
            if not p.is_absolute() or p.resolve() != p or p.is_symlink():
                raise AccessDenied("Native paths must be explicit physical installed sources")
        self.environment = clean_environment(
            Path(d["home"]),
            **(
                {"claude_profile": Path(d["profile"])}
                if d["host"] == "claude"
                else {"codex_home": Path(d["profile"])}
            ),
        )
        self.executable = Path(d["executable"])
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise AccessDenied("The intended native executable is unavailable")
        if hashlib.sha256(self.executable.read_bytes()).hexdigest() != d["executable_sha256"]:
            raise AccessDenied("Native executable source differs from installed intent")
        info = self.executable.stat()
        self.executable_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
        info = Path(d["profile"]).stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise AccessDenied("Intended profile must be owned and protected from foreign writes")
        self.profile_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
        self.digest = digest(d)

    def check(self):
        self.seal.check()
        self.config.check()
        self.installation.check()
        if read_private(self.seal.path) != self.data:
            raise AccessDenied("Parsed native intent changed")
        executable_info = self.executable.lstat()
        if (
            self.executable.is_symlink()
            or self.executable.resolve() != self.executable
            or (
                executable_info.st_dev,
                executable_info.st_ino,
                executable_info.st_uid,
                executable_info.st_mode,
            )
            != self.executable_identity
            or hashlib.sha256(self.executable.read_bytes()).hexdigest()
            != self.data["executable_sha256"]
        ):
            raise AccessDenied("Native executable source changed")
        profile = Path(self.data["profile"])
        info = profile.lstat()
        if (
            profile.is_symlink()
            or (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != self.profile_identity
        ):
            raise AccessDenied("Intended account profile directory changed")
