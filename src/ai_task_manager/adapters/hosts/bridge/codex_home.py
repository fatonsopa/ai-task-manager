"""One owner for coherent-home metadata and immutable configuration sources."""

import errno
import hashlib
import json
import os
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ai_task_manager.domains.work.errors import AccessDenied

ACL_POLICY_GENERATION = "descriptor-no-added-rights-v2"


def no_access_acl(fd):
    """Preserve literal positive absence/empty semantics; inspection errors are unknown."""
    if sys.platform == "darwin":
        from .darwin_acl import AclKind, inspect_acl

        kind = inspect_acl(fd).kind
        if kind is AclKind.UNKNOWN:
            return None
        return kind in {AclKind.ABSENT, AclKind.EMPTY}
    if sys.platform.startswith("linux"):
        try:
            os.getxattr(fd, "system.posix_acl_access")
            return False
        except OSError as error:
            return True if error.errno == errno.ENODATA else None
    return None


@dataclass(frozen=True)
class _NoAclGrants:
    def inspect(self, fd):
        from .darwin_acl import inspect_acl

        return inspect_acl(fd)

    def allows(self, observation):
        from .darwin_acl import AclKind

        if observation.kind is AclKind.UNKNOWN:
            return None
        return observation.kind in {AclKind.ABSENT, AclKind.EMPTY, AclKind.DENY_ONLY}

    def __call__(self, fd):
        """Preserve the single-read callable helper; only the source owner retries."""
        return self.allows(self.inspect(fd)) if sys.platform == "darwin" else no_access_acl(fd)


no_acl_grants = _NoAclGrants()


_REGISTERED_DEFAULT_ACL_QUERY = no_acl_grants


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink


@dataclass(frozen=True)
class SourceFile:
    path: Path
    private: bool = True


@dataclass(frozen=True, eq=False)
class AclAuditPort:
    source: object
    journal: object
    context: str
    _lock: object = field(default_factory=threading.Lock, repr=False)
    _journal_identity: tuple = field(init=False, repr=False)

    def __post_init__(self):
        from .journal import HostJournal

        context = json.loads(self.context)
        if (
            not isinstance(self.journal, HostJournal)
            or not isinstance(self.source, CodexHomeBoundary)
            or not isinstance(context, dict)
            or set(context) != {"host", "attempt_id", "scope", "worktree_id", "database_id"}
        ):
            raise AccessDenied("ACL audit port needs a trusted source, journal and complete scope")
        if (
            context["host"] != "codex"
            or any(
                not isinstance(context[k], str) or not context[k]
                for k in ("attempt_id", "worktree_id", "database_id")
            )
            or not isinstance(context["scope"], dict)
            or set(context["scope"])
            != {"provider", "workspace", "principal", "project", "logical_session"}
            or any(not isinstance(v, str) or not v for v in context["scope"].values())
        ):
            raise AccessDenied("ACL audit scope fields are incomplete")
        info = self.journal.path.lstat()
        object.__setattr__(self, "_journal_identity", (info.st_dev, info.st_ino))

    def verify(self, source, journal, context):
        if source is not self.source or journal is not self.journal or context != self.context:
            raise AccessDenied("ACL audit port belongs to another source, journal or attempt")

    def __call__(self):
        with self._lock:
            info = self.journal.path.lstat()
            if (
                info.st_dev,
                info.st_ino,
            ) != self._journal_identity or (
                self.source._acl_auditor is not self
                or self.source._bound_acl_auditor is not self
            ):
                raise AccessDenied("ACL audit source or journal identity changed")
            batch = self.source.pending_acl_observations()
            if not batch:
                return
            try:
                self.journal._append(
                    "trace",
                    {
                        "effect": "acl_descriptor_observations",
                        "audit_scope": json.loads(self.context),
                        "source_instance": self.source._acl_instance_id,
                        "observations": [json.loads(value) for value in batch],
                    },
                )
                self.source.acknowledge_acl_observations(batch, auditor=self)
            except BaseException as error:
                refused = AccessDenied("Required ACL audit flush failed")
                refused.acl_observations = self.source.pending_acl_observations()
                raise refused from error


class CodexHomeBoundary:
    MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 64, 262144, 2097152

    def __init__(self, home, *, extra_sources=(), acl_query=None):
        self.home = Path(home).absolute()
        self.extra_sources = tuple(extra_sources)
        self._registered_default_acl_query = _REGISTERED_DEFAULT_ACL_QUERY
        self._uses_default_acl_policy = (
            acl_query is None or acl_query is self._registered_default_acl_query
        )
        self.acl_query = self._registered_default_acl_query if acl_query is None else acl_query
        self._bound_acl_query = self.acl_query
        self._default_methods = tuple(
            getattr(type(self.acl_query), name, None) for name in ("inspect", "allows", "__call__")
        )
        self._acl_lock = threading.RLock()
        self._acl_pending, self._acl_reserved, self._acl_serial = [], 0, 0
        self._acl_auditor = self._bound_acl_auditor = None
        self._acl_instance_id = str(uuid4())
        self._acl_policy_generation = (
            ACL_POLICY_GENERATION if self._uses_default_acl_policy else "trusted-custom-acl-query"
        )
        self._check_acl_policy()
        self._home_identity = self._directory(self.home)
        self._credential = self.credential_metadata()
        self._sources = self._snapshot()
        self.check()

    ACL_AUDIT_LIMIT = 32

    def bind_acl_auditor(self, auditor):
        with self._acl_lock:
            if (
                not isinstance(auditor, AclAuditPort)
                or auditor.source is not self
                or self._acl_auditor is not self._bound_acl_auditor
                or self._bound_acl_auditor is not None
                and self._bound_acl_auditor is not auditor
            ):
                raise AccessDenied("ACL audit owner cannot be replaced")
            self._acl_auditor = auditor
            self._bound_acl_auditor = auditor

    def require_acl_acknowledgement(self):
        if self.pending_acl_observations():
            if self._acl_auditor is None or self._acl_auditor is not self._bound_acl_auditor:
                raise self._acl_error("ACL observations require a trusted outside-Core auditor")
            self._acl_auditor()
            self.assert_acl_acknowledged()

    def assert_acl_acknowledged(self):
        if self.pending_acl_observations():
            raise self._acl_error("Pending ACL observations cannot authorize a dependent effect")

    def pending_acl_observations(self):
        with self._acl_lock:
            return tuple(self._acl_pending)

    def acknowledge_acl_observations(self, batch, *, auditor=None):
        with self._acl_lock:
            if (
                auditor is not self._acl_auditor
                or auditor is not self._bound_acl_auditor
                or auditor is None
                or not isinstance(batch, tuple)
                or tuple(self._acl_pending[: len(batch)]) != batch
            ):
                raise AccessDenied("ACL audit acknowledgement does not match its retained prefix")
            del self._acl_pending[: len(batch)]

    def _acl_error(self, reason):
        error = AccessDenied(reason)
        error.acl_observations = self.pending_acl_observations()
        return error

    def _reserve_acl_audit(self):
        with self._acl_lock:
            if len(self._acl_pending) + self._acl_reserved >= self.ACL_AUDIT_LIMIT:
                raise self._acl_error("ACL audit capacity requires an outside-Core flush")
            self._acl_reserved += 1

    def _finish_acl_audit(self, path, trials, keep):
        with self._acl_lock:
            self._acl_reserved -= 1
            if keep:
                self._acl_serial += 1
                record = json.dumps(
                    {
                        "sequence": self._acl_serial,
                        "policy_generation": self._acl_policy_generation,
                        "path_hash": hashlib.sha256(str(path).encode()).hexdigest(),
                        "trials": trials,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self._acl_pending.append(
                    record
                )  # Immutable canonical text, never a mutable record.

    @staticmethod
    def _acl_metadata(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
            info.st_mode,
            info.st_nlink,
            info.st_ctime_ns,
        )

    def _descriptor(self, fd, path, posix):
        from .darwin_acl import AclKind

        self._check_acl_policy()
        if not self._uses_default_acl_policy or sys.platform != "darwin":
            before = self._acl_metadata(os.fstat(fd))
            posix(os.fstat(fd))
            accepted = self.acl_query(fd)
            after = self._acl_metadata(os.fstat(fd))
            posix(os.fstat(fd))
            if accepted is not True or before != after:
                raise AccessDenied("Source ACL safety is unavailable or unsupported")
            return
        self._reserve_acl_audit()
        trials, keep, initial_security, caught_error = [], False, None, None
        try:
            for number in range(2):
                before_info = os.fstat(fd)
                before = self._acl_metadata(before_info)
                trial = {
                    "before": before,
                    "after": None,
                    "acl": None,
                    "decision": "refused",
                    "file_data_before": (before_info.st_size, before_info.st_mtime_ns),
                    "file_data_after": None,
                }
                trials.append(trial)
                posix(before_info)
                if initial_security is not None and before[:6] != initial_security:
                    raise self._acl_error(
                        "Descriptor security identity changed between observations"
                    )
                initial_security = before[:6]
                observed = self.acl_query.inspect(fd)
                trial["acl"] = observed.record()
                after_info = os.fstat(fd)
                after = self._acl_metadata(after_info)
                trial["after"] = after
                trial["file_data_after"] = (after_info.st_size, after_info.st_mtime_ns)
                posix(
                    after_info
                )  # The final POSIX decision is current, never the initial decision.
                snapshots = (before, observed.metadata_before, observed.metadata_after, after)
                if any(
                    not isinstance(v, tuple) or len(v) != 7 or not all(type(x) is int for x in v)
                    for v in snapshots
                ):
                    raise self._acl_error("Complete descriptor observations are unavailable")
                if any(v[:6] != initial_security for v in snapshots):
                    raise self._acl_error("Descriptor security metadata changed during observation")
                stable = all(v == before for v in snapshots)
                if stable and self.acl_query.allows(observed) is True:
                    trial["decision"] = "accepted"
                    keep = len(trials) > 1
                    return
                eligible = (
                    observed.acl_kind in {AclKind.ABSENT, AclKind.EMPTY, AclKind.DENY_ONLY}
                    and observed.reason in {None, "descriptor_changed"}
                    and not stable
                    and (
                        not stat.S_ISREG(before_info.st_mode)
                        or trial["file_data_before"] == trial["file_data_after"]
                    )
                )
                trial["reason"] = "ctime_changed" if eligible else "unsafe_or_unknown_acl"
                if not eligible or number == 1:
                    raise self._acl_error("Descriptor ACL observation is unsafe or unresolved")
        except BaseException as error:
            keep, caught_error = True, error
            raise
        finally:
            self._finish_acl_audit(path, trials, keep)
            if caught_error is not None:
                caught_error.acl_observations = self.pending_acl_observations()

    def _open(self, path, *, directory=False, private=False, regular=False, owned_directory=False):
        path = Path(path).absolute()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(path.parts[1:]):
                final = index == len(path.parts) - 2
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if not final or directory:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child

                def posix(info):
                    if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
                        raise AccessDenied("A source path permits replacement by another user")
                    if (not final or directory) and not stat.S_ISDIR(info.st_mode):
                        raise AccessDenied("Source directory type differs")
                    if final and owned_directory and info.st_uid != os.getuid():
                        raise AccessDenied("The native home must be an owned directory")
                    if (
                        final
                        and regular
                        and (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or private
                            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
                        )
                    ):
                        raise AccessDenied("Protected file ownership, type or permissions differ")

                self._descriptor(fd, Path(*path.parts[: index + 2]), posix)
            return fd
        except (OSError, AccessDenied, AttributeError, UnicodeError):
            os.close(fd)
            refused = AccessDenied("Protected source path cannot be verified")
            refused.acl_observations = self.pending_acl_observations()
            raise refused from None

    def _directory(self, path):
        fd = self._open(path, directory=True, owned_directory=True)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
                raise AccessDenied("The native home must be an owned directory")
            if _identity(info) != _identity(Path(path).lstat()):
                raise AccessDenied("Native source directory changed during inspection")
            return _identity(info)[:-1]
        except OSError:
            raise AccessDenied("Native home identity changed or is unavailable") from None
        finally:
            os.close(fd)

    def _file(self, path, *, private=True, content=False):
        fd = self._open(path, private=private, regular=True)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or private
                and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise AccessDenied("Protected file ownership, type or permissions differ")
            value = (_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            data = None
            if content:
                data = b""
                while part := os.read(fd, min(65536, self.MAX_FILE_BYTES + 1 - len(data))):
                    data += part
                    if len(data) > self.MAX_FILE_BYTES:
                        break
            if data is not None and len(data) > self.MAX_FILE_BYTES:
                raise AccessDenied("Configuration source exceeds its size limit")
            if value != (
                _identity(os.fstat(fd)),
                os.fstat(fd).st_size,
                os.fstat(fd).st_mtime_ns,
                os.fstat(fd).st_ctime_ns,
            ) or _identity(info) != _identity(Path(path).lstat()):
                raise AccessDenied("Protected source changed during inspection")
            return value, data
        except OSError:
            raise AccessDenied("Protected source metadata is unavailable") from None
        finally:
            os.close(fd)

    def credential_metadata(self):
        # This descriptor is never read: native credentials stay in their cache.
        return self._file(self.home / "auth.json")[0]

    def _inventory(self):
        paths = {SourceFile(self.home / "config.toml")}
        paths.update(SourceFile(p) for p in self.home.glob("*.config.toml"))
        for name in (
            "requirements.toml",
            "managed_config.toml",
            "hooks.json",
            "AGENTS.md",
            "AGENTS.override.md",
        ):
            paths.add(SourceFile(self.home / name, private=not name.endswith(".md")))
        paths.update(self.extra_sources)
        if len(paths) > self.MAX_FILES:
            raise AccessDenied("Configuration inventory exceeds its source limit")
        return sorted(paths, key=lambda source: str(source.path))

    def _snapshot(self, *, content=True):
        result, total = {}, 0
        for source in self._inventory():
            path = Path(source.path).absolute()
            try:
                path.lstat()
            except FileNotFoundError:
                result[str(path)] = self._absence(path)
                continue
            except OSError:
                raise AccessDenied("Configuration presence is unknown") from None
            metadata, data = self._file(path, private=source.private, content=content)
            total += metadata[1]
            if total > self.MAX_TOTAL_BYTES:
                raise AccessDenied("Configuration inventory exceeds its aggregate limit")
            if metadata[1] > self.MAX_FILE_BYTES:
                raise AccessDenied("Configuration source exceeds its size limit")
            result[str(path)] = (metadata, hashlib.sha256(data).hexdigest() if content else None)
        if isinstance(result.get(str(self.home / "config.toml")), dict):
            raise AccessDenied("The coherent native user configuration is unavailable")
        return result

    def _absence(self, path):
        parent = path.parent
        while True:
            try:
                parent.lstat()
                break
            except FileNotFoundError:
                parent = parent.parent
            except OSError:
                raise AccessDenied("Expected-absence parent metadata is unknown") from None
        fd = self._open(parent, directory=True)
        try:
            try:
                path.lstat()
            except FileNotFoundError:
                return {
                    "absent": True,
                    "parent": str(parent),
                    "identity": _identity(os.fstat(fd))[:-1],
                }
            except OSError:
                raise AccessDenied("Expected configuration absence is unknown") from None
            raise AccessDenied("Expected-absence source changed during inspection")
        finally:
            os.close(fd)

    @property
    def sealed_paths(self):
        return frozenset(
            path for path, value in self._sources.items() if not isinstance(value, dict)
        )

    def _check_acl_policy(self):
        if self._acl_auditor is not self._bound_acl_auditor:
            raise AccessDenied("The bound ACL audit port changed")
        if not callable(self._bound_acl_query) or self.acl_query is not self._bound_acl_query:
            raise AccessDenied("The bound ACL predicate changed; prepare a new source binding")
        if self._uses_default_acl_policy and (
            no_acl_grants is not self._registered_default_acl_query
            or _REGISTERED_DEFAULT_ACL_QUERY is not self._registered_default_acl_query
            or self._acl_policy_generation != ACL_POLICY_GENERATION
            or tuple(
                getattr(type(self.acl_query), name, None)
                for name in ("inspect", "allows", "__call__")
            )
            != self._default_methods
        ):
            raise AccessDenied("ACL policy binding changed; prepare a new source binding")

    def check(self):
        self._check_acl_policy()
        if self._directory(self.home) != self._home_identity or self._snapshot() != self._sources:
            raise AccessDenied("Native source generation changed; do not silently re-seal")
        self.credential_metadata()

    def _metadata_snapshot(self):
        """Recheck the prepared generation without source-content reads."""
        self._check_acl_policy()
        expected = {
            path: value if isinstance(value, dict) else (value[0], None)
            for path, value in self._sources.items()
        }
        if (
            self._directory(self.home) != self._home_identity
            or self._snapshot(content=False) != expected
        ):
            raise AccessDenied("Protected source metadata changed after preparation")
        return self.credential_metadata()

    def check_metadata(self):
        self.assert_acl_acknowledged()
        result = self._metadata_snapshot()
        self.assert_acl_acknowledged()
        return result

    def prepare_metadata(self):
        """Trusted off-Core preparation; current consumption never invokes this port."""
        self.require_acl_acknowledgement()
        result = self._metadata_snapshot()
        self.require_acl_acknowledgement()
        return result

    def check_readonly_credential(self, before):
        self.check()
        if self.credential_metadata() != before:
            raise AccessDenied("Existing-auth preflight changed its credential metadata")

    @property
    def fingerprint(self):
        from ai_task_manager.config.files import digest

        return digest(
            {
                "home": self._home_identity,
                "sources": self._sources,
                "acl_policy": self._acl_policy_generation,
            }
        )

    def read_config(self):
        self.check()
        return self._file(self.home / "config.toml", content=True)[1]
