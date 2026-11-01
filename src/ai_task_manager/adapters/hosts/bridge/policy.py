"""Exact managed launch policies; host conformance remains an independent gate."""

import hashlib
import os
import pwd
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ai_task_manager.config.files import FileSeal, digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import Command

CODEX_RESTRICTIONS = {
    "forced_login_method": "chatgpt",
    "model_provider": "openai",
    "agents.enabled": False,
    "features.multi_agent": False,
    "features.shell_tool": False,
    "tools.update_plan.enabled": False,
    "features.apps": False,
    "features.plugins": False,
    "features.browser_use": False,
    "features.computer_use": False,
    "features.code_mode_host": False,
    "features.sleep_tool": False,
    "web_search": "disabled",
    "approval_policy": "never",
    "sandbox_mode": "read-only",
}


@dataclass(frozen=True)
class NativeProfile:
    model: str
    effort: str | None = None

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise AccessDenied("A trusted explicit model name is required")
        if self.effort is not None and (
            not isinstance(self.effort, str) or not self.effort.strip()
        ):
            raise AccessDenied("Requested effort must be an explicit configured string")


def clean_environment(home, *, claude_profile=None, codex_home=None):
    home = Path(home).resolve()
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(home),
        "USER": pwd.getpwuid(os.getuid()).pw_name,
        "LANG": "en_US.UTF-8",
    }
    if (claude_profile is None) == (codex_home is None):
        raise AccessDenied("Exactly one intended subscription profile must be bound")
    if claude_profile is not None:
        profile = Path(claude_profile)
        if (
            profile.is_symlink()
            or profile.resolve() != home / ".claude_roni"
            or not profile.is_dir()
        ):
            raise AccessDenied("Only the explicit approved Claude profile is supported")
        env["CLAUDE_CONFIG_DIR"] = str(profile.resolve())
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    else:
        profile = Path(codex_home)
        if profile.is_symlink() or not profile.is_dir() or profile.stat().st_mode & 0o077:
            raise AccessDenied("Codex requires a separate private managed home")
        env["CODEX_HOME"] = str(profile.absolute())
    return env


@dataclass(frozen=True)
class NativePolicy:
    host: str
    executable: Path
    executable_sha256: str
    environment: dict[str, str]
    cwd: Path
    configuration_digest: str
    seals: tuple[FileSeal, ...]
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    _executable_identity: tuple = field(init=False, repr=False)

    def __post_init__(self):
        if (
            self.executable.is_symlink()
            or not self.executable.is_file()
            or hashlib.sha256(self.executable.read_bytes()).hexdigest() != self.executable_sha256
        ):
            raise AccessDenied("Native executable does not match the approved hash")
        info = self.executable.stat()
        object.__setattr__(
            self,
            "_executable_identity",
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
        )
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def check(self):
        if self.host == "claude" and (not isinstance(self.model, str) or not self.model.strip()):
            raise AccessDenied("Claude requires a trusted explicit model profile")
        if self.effort is not None and (
            not isinstance(self.effort, str) or not self.effort.strip()
        ):
            raise AccessDenied("Requested effort must be explicit configured data")
        if (
            self.host not in {"claude", "codex"}
            or not self.executable.is_absolute()
            or self.executable.is_symlink()
            or not self.executable.is_file()
            or (
                lambda info: (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )(self.executable.stat())
            != self._executable_identity
        ):
            raise AccessDenied("Native executable changed or is not explicitly approved")
        for seal in self.seals:
            seal.check()
        allowed = {"PATH", "HOME", "USER", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"}
        if self.host == "codex":
            allowed = {"PATH", "HOME", "USER", "LANG", "CODEX_HOME"}
        if set(self.environment) != allowed:
            raise AccessDenied("Native provider environment is not the exact whitelist")
        if self.environment["USER"] != pwd.getpwuid(os.getuid()).pw_name:
            raise AccessDenied("Native ordinary user identity changed")


def codex_command(policy, timeout):
    policy.check()
    argv = [str(policy.executable)]
    import json

    for name, value in CODEX_RESTRICTIONS.items():
        argv.extend(["-c", name + "=" + json.dumps(value)])
    if policy.model is not None:
        argv.extend(["-c", "model=" + json.dumps(policy.model)])
    if policy.effort is not None:
        argv.extend(["-c", "model_reasoning_effort=" + json.dumps(policy.effort)])
    argv.extend(["app-server", "--strict-config", "--stdio"])
    return Command(tuple(argv), policy.cwd, dict(policy.environment), timeout, "codex")


def policy_digest(host, executable_sha256, environment, cwd, files, *, model=None, effort=None):
    """Auth contents never enter the configuration fingerprint."""
    return digest(
        {
            "host": host,
            "executable": executable_sha256,
            "environment": environment,
            "cwd": str(cwd),
            "files": files,
            "requested_model": model,
            "requested_effort": effort,
            "native_restrictions": CODEX_RESTRICTIONS if host == "codex" else "sdk-tools-only",
        }
    )
