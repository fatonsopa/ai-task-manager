"""Stable capability recipes and separately checked physical source custody."""

import hashlib
import importlib.metadata
import json
import os
import platform
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from ..admission import MANAGED_TOOLS, launch_template
from ..codex_settings import RESTRICTIONS
from ..gateway import SCHEMAS, tool_schema
from ..policy import NativePolicy, codex_command, policy_digest


def require(value, message):
    if not value:
        raise AccessDenied(message)


def _hash(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while part := stream.read(1048576):
            value.update(part)
    return value.hexdigest()


def _physical(path):
    info = path.lstat()
    require(
        path.is_absolute()
        and path.resolve() == path
        and stat.S_ISREG(info.st_mode)
        and info.st_uid in {0, os.getuid()}
        and not info.st_mode & 0o022
        and info.st_nlink == 1,
        "Capability source must be a physical protected regular file",
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class ProfileSources:
    """Logical IDs map to (physical Path, expected SHA256), never task config."""

    def __init__(self, logical_files, dependency_versions, instruction_ids=()):
        require(
            type(logical_files) is dict
            and bool(logical_files)
            and type(dependency_versions) is dict
            and bool(dependency_versions),
            "Explicit logical source and installed dependency identities are required",
        )
        self.files = tuple(
            sorted(
                (name, Path(value[0]).absolute(), value[1]) for name, value in logical_files.items()
            )
        )
        require(
            all(
                isinstance(name, str)
                and name
                and not name.startswith("/")
                and isinstance(sha, str)
                and len(sha) == 64
                for name, _, sha in self.files
            ),
            "Source IDs must be logical names with exact content hashes",
        )
        self.versions = tuple(sorted(dependency_versions.items()))
        self.instruction_ids = tuple(instruction_ids)
        require(
            len(set(self.instruction_ids)) == len(self.instruction_ids)
            and set(self.instruction_ids) <= {name for name, _, _ in self.files},
            "Every fixed role instruction must have a sealed logical source",
        )
        self._physical = tuple(_physical(path) for _, path, _ in self.files)
        self._bound = (self.files, self.versions, self.instruction_ids, self._physical)
        self.check()

    def check(self):
        require(
            all(
                a is b
                for a, b in zip(
                    self._bound,
                    (self.files, self.versions, self.instruction_ids, self._physical),
                    strict=True,
                )
            ),
            "Capability source owner changed",
        )
        for (_, path, sha), physical in zip(self.files, self._physical, strict=True):
            require(
                _physical(path) == physical and _hash(path) == sha,
                "Current physical capability source changed",
            )
        for name, wanted in self.versions:
            try:
                actual = (
                    platform.python_version()
                    if name == "python"
                    else importlib.metadata.version(name)
                )
            except importlib.metadata.PackageNotFoundError:
                raise AccessDenied(
                    "Required installed capability dependency is unavailable"
                ) from None
            require(actual == wanted, "Installed capability dependency version changed")

    @property
    def fingerprint(self):
        return digest({"files": [(n, h) for n, _, h in self.files], "versions": self.versions})


@dataclass(frozen=True)
class StableProfile:
    version: int
    host: str
    model: str
    effort: str
    account_hash: str
    executable_sha256: str
    cli_version: str
    source_hashes: tuple[tuple[str, str], ...]
    dependency_versions: tuple[tuple[str, str], ...]
    tool_schema_sha256: str
    launch_recipe_sha256: str
    instruction_recipe_sha256: str
    restrictions_sha256: str

    @property
    def fingerprint(self):
        return digest(asdict(self))

    @classmethod
    def decode(cls, value):
        require(
            type(value) is dict and set(value) == set(cls.__dataclass_fields__),
            "An exact version2 capability profile is required",
        )
        value = dict(value)
        for name in ("source_hashes", "dependency_versions"):
            value[name] = tuple(tuple(pair) for pair in value[name])
        result = cls(**value)
        require(
            result.version == 2
            and result.host in {"claude", "codex"}
            and result.effort == "high"
            and bool(result.cli_version),
            "Unsupported capability profile version or host",
        )
        return result


def _projection_policy(manifest):
    d = manifest.data
    cwd = Path(d["home"])
    return NativePolicy(
        d["host"],
        Path(d["executable"]),
        d["executable_sha256"],
        manifest.environment,
        cwd,
        policy_digest(
            d["host"],
            d["executable_sha256"],
            manifest.environment,
            cwd,
            {},
            model=d["model"],
            effort=d["effort"],
        ),
        (),
        tuple(sorted(MANAGED_TOOLS)),
        d["model"],
        d["effort"],
        cli_version=d.get("cli_version"),
    )


def _recipe(policy):
    if policy.host == "claude":
        require(set(policy.allowed_tools) == MANAGED_TOOLS, "Managed tool surface changed")
        actual = launch_template(policy)
        argv = list(actual["argv"])
        restrictions = {"host": "claude", "closed_sdk_recipe": argv[1:]}
    elif policy.host == "codex":
        argv = list(codex_command(policy, 1).argv)
        # Per-user foreign server names are physical configuration facts. The
        # recipe requires all of them disabled, re-observed for every launch.
        clean, i = [], 1
        while i < len(argv):
            if argv[i] == "-c" and i + 1 < len(argv):
                key, _, value = argv[i + 1].partition("=")
                if key.startswith("mcp_servers.") and key.endswith(".enabled"):
                    require(json.loads(value) is False, "Foreign MCP server was enabled")
                    i += 2
                    continue
            clean.append(argv[i])
            i += 1
        argv = [argv[0], *clean]
        restrictions = {
            **RESTRICTIONS,
            "forced_login_method": "chatgpt",
            "foreign_mcp": "all-disabled-and-observed-empty",
        }
    else:
        raise AccessDenied("Unsupported capability recipe host")
    argv[0] = "<native-executable-sha256>"
    return {"argv": argv, "environment": dict(policy.environment)}, restrictions


def prepare_profile(manifest, sources, cli_version, *, policy=None):
    """Code-owned pre-worktree projection, then the same runtime policy projection."""
    require(isinstance(sources, ProfileSources), "Typed current logical profile sources required")
    manifest.check()
    sources.check()
    d = manifest.data
    policy = _projection_policy(manifest) if policy is None else policy
    require(
        (policy.host, policy.model, policy.effort, policy.executable_sha256)
        == (d["host"], d["model"], d["effort"], d["executable_sha256"])
        and dict(policy.environment) == dict(manifest.environment),
        "Current launch differs from the intended account/profile",
    )
    require(
        policy.host != "claude"
        or policy.cli_version == d.get("cli_version", "2.1.301") == cli_version,
        "Observed CLI version differs from the installed native version",
    )
    recipe, restrictions = _recipe(policy)
    hashes = tuple((name, sha) for name, _, sha in sources.files)
    return StableProfile(
        2,
        d["host"],
        d["model"],
        d["effort"],
        d["account_hash"],
        d["executable_sha256"],
        cli_version,
        hashes,
        sources.versions,
        digest({name: tool_schema(name) for name in SCHEMAS}),
        digest(recipe),
        digest([(name, dict(hashes)[name]) for name in sources.instruction_ids]),
        digest(restrictions),
    )
