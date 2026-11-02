"""One owner for coherent-home metadata and immutable configuration sources."""

import ctypes
import errno
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied


def no_access_acl(fd):
    """Return positive absence only; unsupported/inaccessible ACLs are unknown."""
    if sys.platform == "darwin":
        lib = ctypes.CDLL(None, use_errno=True)
        lib.acl_get_fd.argtypes, lib.acl_get_fd.restype = [ctypes.c_int], ctypes.c_void_p
        lib.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        lib.acl_to_text.restype = ctypes.c_void_p
        lib.acl_free.argtypes = [ctypes.c_void_p]
        ctypes.set_errno(0)
        acl = lib.acl_get_fd(fd)
        if not acl:
            # Apple Libc acl_file.c calls filesec_get_property(FILESEC_ACL).
            # For a valid open descriptor, its absent property reports ENOENT;
            # access/unsupported/other errors remain unknown, never empty ACLs.
            return True if ctypes.get_errno() == errno.ENOENT else None
        text = None
        try:
            length = ctypes.c_ssize_t()
            text = lib.acl_to_text(acl, ctypes.byref(length))
            if not text:
                return None
            lines = ctypes.string_at(text, length.value).decode().splitlines()
            return not any(line.strip() and not line.startswith("!#acl") for line in lines)
        finally:
            if text:
                lib.acl_free(text)
            lib.acl_free(acl)
    if sys.platform.startswith("linux"):
        try:
            os.getxattr(fd, "system.posix_acl_access")
            return False
        except OSError as error:
            return True if error.errno == errno.ENODATA else None
    return None


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink


@dataclass(frozen=True)
class SourceFile:
    path: Path
    private: bool = True


class CodexHomeBoundary:
    MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 64, 262144, 2097152

    def __init__(self, home, *, extra_sources=(), acl_query=no_access_acl):
        self.home, self.acl_query = Path(home).absolute(), acl_query
        self.extra_sources = tuple(extra_sources)
        self._home_identity = self._directory(self.home)
        self._credential = self.credential_metadata()
        self._sources = self._snapshot()
        self.check()

    def _open(self, path, *, directory=False):
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
                info = os.fstat(fd)
                if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
                    raise AccessDenied("A source path permits replacement by another user")
                if self.acl_query(fd) is not True:
                    raise AccessDenied("Source ACL safety is unavailable or unsupported")
            return fd
        except (OSError, AccessDenied, AttributeError, UnicodeError):
            os.close(fd)
            raise AccessDenied("Protected source path cannot be verified") from None

    def _directory(self, path):
        fd = self._open(path, directory=True)
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
        fd = self._open(path)
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

    def _snapshot(self):
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
            metadata, data = self._file(path, private=source.private, content=True)
            total += len(data)
            if total > self.MAX_TOTAL_BYTES:
                raise AccessDenied("Configuration inventory exceeds its aggregate limit")
            result[str(path)] = (metadata, hashlib.sha256(data).hexdigest())
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

    def check(self):
        if self._directory(self.home) != self._home_identity or self._snapshot() != self._sources:
            raise AccessDenied("Native source generation changed; do not silently re-seal")
        self.credential_metadata()

    def check_readonly_credential(self, before):
        self.check()
        if self.credential_metadata() != before:
            raise AccessDenied("Existing-auth preflight changed its credential metadata")

    @property
    def fingerprint(self):
        from ai_task_manager.config.files import digest

        return digest({"home": self._home_identity, "sources": self._sources})

    def read_config(self):
        self.check()
        return self._file(self.home / "config.toml", content=True)[1]
