"""Disposable descriptor fixtures; never inspect or mutate a normal account home."""

import ctypes
import os
from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge import darwin_acl


@contextmanager
def descriptor(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def set_fixture_acl(path, tags, *, permission=1 << 2):
    """Write only an owned test leaf using fixed synthetic UUIDs, without name lookup."""
    path = Path(path)
    assert path.is_absolute() and not path.is_symlink() and path.stat().st_uid == os.getuid()
    api = darwin_acl.DarwinAclApi()
    lib = api.lib
    signatures = {
        "acl_create_entry": (
            [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int,
        ),
        "acl_set_tag_type": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "acl_set_qualifier": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "acl_get_permset": (
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int,
        ),
        "acl_add_perm": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "acl_set_fd": ([ctypes.c_int, ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    acl = ctypes.c_void_p(api.empty())
    assert acl.value and acl.value > 1
    try:
        for index, tag in enumerate(tags):
            entry, permissions = ctypes.c_void_p(), ctypes.c_void_p()
            assert lib.acl_create_entry(ctypes.byref(acl), ctypes.byref(entry)) == 0
            assert lib.acl_set_tag_type(entry, tag) == 0
            # No live user/group qualifier is read. These fixed bytes identify only fixture ACEs.
            qualifier = (ctypes.c_ubyte * 16)(*([index + 1] * 16))
            assert lib.acl_set_qualifier(entry, qualifier) == 0
            assert lib.acl_get_permset(entry, ctypes.byref(permissions)) == 0
            # Installed ACL_WRITE_DATA or ACL_READ_DATA fixture constant.
            assert lib.acl_add_perm(permissions, permission) == 0
        with descriptor(path) as fd:
            assert lib.acl_set_fd(fd, acl) == 0, ctypes.get_errno()
    finally:
        assert api.free(acl.value)


class AuthoredAclApi:
    """Deterministic opaque-API failures; not native ACL evidence."""

    def __init__(self, tags=(2,), *, fault=None, error=0):
        self.tags, self.fault, self.error = tags, fault, error
        self.calls, self.freed, self.cursor = [], [], -1

    def get(self, fd):
        self.calls.append(("get", fd))
        if self.fault == "get_error":
            return None, self.error
        if self.fault == "special":
            return 1, 0
        if self.fault == "raise":
            raise OSError("authored unavailable metadata")
        return 100, 0

    def empty(self):
        self.calls.append(("empty",))
        return None if self.fault == "empty_missing" else 200

    def valid(self, value):
        self.calls.append(("valid", value))
        return not (self.fault == "invalid" or self.fault == "invalid_empty" and value == 200)

    def size(self, value):
        self.calls.append(("size", value))
        if self.fault == "negative_size":
            return -1
        if self.fault == "contradictory_size":
            return 45 if value == 200 else 44
        if self.fault == "huge_size":
            return 2**30
        return 44 if value == 200 else 44 + 24 * len(self.tags)

    def entry(self, value, selector):
        self.calls.append(("entry", selector))
        if selector == 0:
            self.cursor = 0
            if (
                self.fault == "first"
                or self.fault == "changed_first"
                and self.calls.count(("entry", 0)) > 1
            ):
                return None
        elif selector == -2:
            self.cursor = len(self.tags) - 1
            if self.fault == "last":
                return None
        else:
            self.cursor += 1
            if self.fault == "premature":
                return None
            if self.fault == "repeat":
                return 1000
        return 1000 + self.cursor if 0 <= self.cursor < len(self.tags) else None

    def tag(self, entry):
        self.calls.append(("tag", entry))
        return None if self.fault == "tag" else self.tags[entry - 1000]

    def free(self, value):
        self.freed.append(value)
        return not (
            self.fault == "free" and value == 100 or self.fault == "free_empty" and value == 200
        )


@contextmanager
def observe_fixture_boundaries(value, monkeypatch, case_name):
    """Retain only structured metadata from this authored, credential-free fixture."""
    import hashlib
    import json

    directory = Path(os.environ["AITM_TEST_EVIDENCE_DIR"])
    path = directory / (
        "acl-boundary-" + hashlib.sha256(case_name.encode()).hexdigest()[:16] + ".json"
    )
    records = []
    dropped = 0

    def record(kind, data):
        nonlocal dropped
        if len(records) < 128:
            records.append({"kind": kind, **data})
        else:
            dropped += 1

    def metadata(fd):
        try:
            info = os.fstat(fd)
            return {
                "device": info.st_dev,
                "inode": info.st_ino,
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": info.st_mode,
                "ctime_ns": info.st_ctime_ns,
            }
        except OSError as error:
            return {"error_class": type(error).__name__, "errno": error.errno}

    inspect = darwin_acl.inspect_acl

    def inspect_observed(fd):
        before = metadata(fd)
        observed = inspect(fd)
        if observed.kind is darwin_acl.AclKind.UNKNOWN:
            record(
                "acl_unknown", {"reason": observed.reason, "before": before, "after": metadata(fd)}
            )
        return observed

    monkeypatch.setattr(darwin_acl, "inspect_acl", inspect_observed)
    factory = value[4]
    original = factory.permits.record_auth

    def completion(binding, result):
        record(
            "authored_status_completion",
            {
                "attempt_id": binding.attempt_id,
                "outcome": result.get("outcome"),
                "reason": result.get("reason"),
                "returncode": result.get("returncode"),
                "group": result.get("group"),
            },
        )
        return original(binding, result)

    monkeypatch.setattr(factory.permits, "record_auth", completion)
    try:
        yield path
    finally:
        # The tested Core call has returned/raised before this context is left.
        path.write_text(
            json.dumps(
                {
                    "scope": "Authored process controls; no native account evidence",
                    "case": case_name,
                    "records": records,
                    "dropped": dropped,
                },
                indent=2,
            )
            + "\n"
        )
        path.chmod(0o600)
