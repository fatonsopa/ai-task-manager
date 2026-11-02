"""Confined executable/configuration and physical repository bindings for final Git effects.

Construction belongs to trusted composition. These bindings establish local scope,
not task authority or resistance to a hostile same-UID filesystem mutator.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

MAX_OUTPUT = 1024 * 1024
QUERY_SECONDS = 10
SAFE_CONFIG = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "user.name",
        "user.email",
    }
)
FINAL_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "maintenance.auto=false",
    "gc.auto=0",
    "merge.autoStash=false",
    "submodule.recurse=false",
    "core.attributesFile=/dev/null",
    "protocol.allow=never",
    "core.pager=cat",
    "commit.gpgSign=false",
)


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def physical(path, *, directory=True):
    p = Path(path).absolute()
    if p != p.resolve() or any(x.is_symlink() for x in [p, *p.parents]):
        raise AccessDenied("Symbolic physical paths are unsupported")
    s = p.stat()
    if (directory and not stat.S_ISDIR(s.st_mode)) or (
        not directory and not stat.S_ISREG(s.st_mode)
    ):
        raise AccessDenied("Unexpected physical resource type")
    if s.st_uid not in {0, os.getuid()} or s.st_mode & 0o022:
        raise AccessDenied("Resource has unsupported ownership or write permissions")
    return s.st_dev, s.st_ino


def oid(value):
    # Current F0 publication contract is SHA-1 only. Other formats fail explicitly.
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValidationError("An exact SHA-1 object identifier is required")
    return value


def refname(value):
    if (
        type(value) is not str
        or not value.startswith("refs/heads/")
        or len(value) > 200
        or any(c in value for c in " ~^:?*[\\\x00\n\r")
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValidationError("An explicit supported local branch reference is required")
    return value


@dataclass(frozen=True)
class Executable:
    path: str
    identity: tuple[int, int]
    sha256: str
    version: str
    exec_path: str
    exec_manifest: str

    @staticmethod
    def _manifest(path):
        p = Path(path)
        physical(p)
        rows, hashed = [], {}
        for q in sorted(p.iterdir()):
            if not q.name.startswith("git-") or not q.is_file():
                continue
            resolved = q.resolve()
            s = resolved.stat()
            if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o022:
                raise AccessDenied("Unsupported Git subprogram")
            key = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            if key not in hashed:
                hashed[key] = file_digest(resolved)
            rows.append((q.name, str(resolved), hashed[key]))
        if not rows:
            raise AccessDenied("Git subprogram profile is unavailable")
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    @classmethod
    def capture(cls, path, environment):
        p = Path(path).absolute()
        ident = physical(p, directory=False)
        if not os.access(p, os.X_OK):
            raise AccessDenied("Selected Git is not executable")
        values = []
        for arg in ("--version", "--exec-path"):
            r = subprocess.run(
                [str(p), arg], env=environment, capture_output=True, timeout=QUERY_SECONDS
            )
            if r.returncode or len(r.stdout) + len(r.stderr) > MAX_OUTPUT:
                raise AccessDenied("Git executable metadata is unavailable")
            values.append(r.stdout.decode("utf-8", "strict").strip())
        if not re.fullmatch(r"git version 2\.[0-9]+\.[0-9]+(?:[ .()A-Za-z0-9-]*)", values[0]):
            raise AccessDenied("Unsupported selected Git version")
        ep = Path(values[1])
        if not ep.is_absolute():
            raise AccessDenied("Git exec path is not absolute")
        return cls(str(p), ident, file_digest(p), values[0], str(ep), cls._manifest(ep))

    def validate(self):
        if (
            physical(self.path, directory=False) != self.identity
            or file_digest(self.path) != self.sha256
            or self._manifest(self.exec_path) != self.exec_manifest
        ):
            raise AccessDenied("Selected Git executable profile changed")


class GitProfile:
    """Only fresh, explicitly enrolled local repositories are supported here."""

    def __init__(self, *, root, executable, home):
        self.root, self.home = Path(root).absolute(), Path(home).absolute()
        self.root_identity, self.home_identity = physical(self.root), physical(self.home)
        if self.home != self.root and self.root not in self.home.parents:
            raise AccessDenied("Git HOME must belong to the confined root")
        if any(self.home.iterdir()):
            raise AccessDenied("Git HOME must be empty at enrollment")
        self.executable = executable
        self.repositories = {}

    @staticmethod
    def environment(home, exec_path=None):
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "TMPDIR": str(home),
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        if exec_path is not None:
            env["GIT_EXEC_PATH"] = exec_path
        return env

    @property
    def env(self):
        return dict(
            self.environment(self.home, self.executable.exec_path),
            GIT_CEILING_DIRECTORIES=str(self.root),
        )

    def _inside(self, path):
        p = Path(path).absolute()
        if self.root not in p.parents:
            raise AccessDenied("Repository is outside the confined root")
        return p

    def _routing(self, repo):
        """Resolve routing anew without trusting a previously enrolled directory."""
        repo = self._inside(repo)
        dotgit = repo / ".git"
        if dotgit.is_symlink():
            raise AccessDenied("Symbolic Git metadata is unsupported")
        metadata = dotgit
        if dotgit.is_file():
            physical(dotgit, directory=False)
            if dotgit.stat().st_size > 4096:
                raise AccessDenied("Oversized worktree pointer")
            text = dotgit.read_text()
            if not text.startswith("gitdir: ") or "\n" in text.rstrip("\n"):
                raise AccessDenied("Unknown worktree pointer")
            metadata = (repo / text[8:].strip()).resolve()
        self._inside(metadata)
        physical(metadata)
        common = metadata
        pointer = metadata / "commondir"
        if pointer.exists() or pointer.is_symlink():
            physical(pointer, directory=False)
            if pointer.stat().st_size > 4096:
                raise AccessDenied("Oversized common-directory pointer")
            text = pointer.read_text().strip()
            if not text or "\n" in text:
                raise AccessDenied("Unknown common-directory pointer")
            common = (metadata / text).resolve()
        self._inside(common)
        physical(common)
        return metadata, common

    def _record(self, repo):
        path = str(Path(repo).absolute())
        return next((r for r in self.repositories.values() if r["path"] == path), None)

    def _check_routing(self, r):
        metadata, common = self._routing(r["path"])
        if (
            str(metadata) != r["gitdir"]
            or str(common) != r["common"]
            or physical(metadata) != r["gitdir_identity"]
            or physical(common) != r["common_identity"]
        ):
            raise AccessDenied("Repository Git routing changed")

    def environment_for(self, repo):
        env = self.env
        r = self._record(repo)
        if r:
            self._check_routing(r)
            # Bind both paths explicitly so a later pointer rewrite cannot redirect
            # this child. This is not descriptor attestation against hostile mutation.
            env["GIT_COMMON_DIR"] = r["common"]
        return env

    def argv(self, repo, *args):
        r = self._record(repo)
        paths = ["-C", str(repo)]
        if r:
            self._check_routing(r)
            paths += ["--git-dir=" + r["gitdir"], "--work-tree=" + r["path"]]
        return [self.executable.path, *[a for c in FINAL_CONFIG for a in ("-c", c)], *paths, *args]

    def query(self, repo, *args, ok=(0,)):
        # Internal fixed call sites only; this is not a model-facing command tool.
        r = subprocess.run(
            self.argv(repo, *args),
            env=self.environment_for(repo),
            capture_output=True,
            timeout=QUERY_SECONDS,
        )
        if len(r.stdout) + len(r.stderr) > MAX_OUTPUT:
            raise AccessDenied("Git observation exceeded its output bound")
        if r.returncode not in ok:
            raise AccessDenied(
                "Git observation failed: " + r.stderr.decode("utf-8", "replace")[-300:]
            )
        return r

    def _configuration(self, metadata, common):
        """Read only owned config files, without repository discovery or includes."""
        directories = dict.fromkeys((metadata, common))
        for directory in directories:
            head = directory / "HEAD"
            physical(head, directory=False)
            if head.stat().st_size > 4096:
                raise AccessDenied("Oversized repository HEAD metadata")
            worktree_config = directory / "config.worktree"
            if worktree_config.exists() or worktree_config.is_symlink():
                raise AccessDenied("Worktree configuration is unsupported")
        for directory in directories:
            config = directory / "config"
            if not config.exists() and directory != common and not config.is_symlink():
                continue
            physical(config, directory=False)
            if config.stat().st_size > MAX_OUTPUT:
                raise AccessDenied("Repository configuration is oversized")
            # Explicit file mode and an empty owned cwd prevent config setup from
            # consuming repository includes before their keys are rejected.
            reply = subprocess.run(
                [
                    self.executable.path,
                    "config",
                    "--file",
                    str(config),
                    "--no-includes",
                    "--null",
                    "--list",
                ],
                cwd=self.home,
                env=self.env,
                capture_output=True,
                timeout=QUERY_SECONDS,
            )
            if reply.returncode or len(reply.stdout) + len(reply.stderr) > MAX_OUTPUT:
                raise AccessDenied("Repository configuration is unavailable")
            for field in reply.stdout.split(b"\0"):
                if not field:
                    continue
                key, sep, _ = field.partition(b"\n")
                try:
                    supported = bool(sep) and key.decode("ascii", "strict").lower() in SAFE_CONFIG
                except UnicodeError:
                    supported = False
                if not supported:
                    raise AccessDenied("Repository contains unsupported configuration")

    def enroll(self, path, *, worktree_id, common_git_id):
        p = self._inside(path)
        ident = physical(p)
        metadata, common_candidate = self._routing(p)
        self.executable.validate()
        self._configuration(metadata, common_candidate)
        # Configuration is already checked without includes before Git resolves metadata.
        common = Path(
            self.query(p, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .stdout.decode()
            .strip()
        )
        gitdir = Path(self.query(p, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
        for q in (common, gitdir):
            self._inside(q)
            physical(q)
        record = {
            "path": str(p),
            "identity": ident,
            "common": str(common),
            "common_identity": physical(common),
            "gitdir": str(gitdir),
            "gitdir_identity": physical(gitdir),
            "worktree_id": worktree_id,
            "common_git_id": common_git_id,
        }
        self.repositories[worktree_id] = record
        self.validate(worktree_id, common_git_id)
        return record.copy()

    def validate(self, worktree_id, common_git_id):
        if physical(self.root) != self.root_identity or physical(self.home) != self.home_identity:
            raise AccessDenied("Confined root identity changed")
        self.executable.validate()
        # No user configuration or credential files can appear in the isolated HOME.
        if any(p.name not in {"tmp"} for p in self.home.iterdir()):
            raise AccessDenied("Confined HOME changed")
        r = self.repositories.get(worktree_id)
        if r is None or r["common_git_id"] != common_git_id:
            raise AccessDenied("Repository is not enrolled for this binding")
        for k, identity in (
            ("path", "identity"),
            ("common", "common_identity"),
            ("gitdir", "gitdir_identity"),
        ):
            if physical(r[k]) != r[identity]:
                raise AccessDenied("Repository physical identity changed")
        self._check_routing(r)
        repo, common = Path(r["path"]), Path(r["common"])
        for p in (
            common / "objects/info/alternates",
            common / "objects/info/http-alternates",
            common / "info/grafts",
        ):
            if p.exists():
                raise AccessDenied("Object alternates or grafts are unsupported")
        self._configuration(Path(r["gitdir"]), common)
        if self.query(repo, "rev-parse", "--show-object-format").stdout.strip() != b"sha1":
            raise AccessDenied("Object format is unsupported by the publication journal")
        if self.query(repo, "for-each-ref", "--format=%(refname)", "refs/replace").stdout.strip():
            raise AccessDenied("Replacement references are unsupported")
        for p in (common / "info/attributes", common / "config.worktree"):
            if p.exists():
                raise AccessDenied("Worktree configuration or attributes are unsupported")
        if any(repo.rglob(".gitattributes")):
            raise AccessDenied("Checkout attributes are unsupported")
        return r.copy()

    def reference(self, repo, ref):
        refname(ref)
        r = self.query(repo, "symbolic-ref", "-q", ref, ok=(0, 1))
        if r.returncode == 0:
            raise AccessDenied("Symbolic publication references are unsupported")
        return oid(
            self.query(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
            .stdout.decode()
            .strip()
        )
