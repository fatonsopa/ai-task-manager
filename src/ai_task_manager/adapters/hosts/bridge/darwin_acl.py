"""Bounded descriptor-only Darwin ACL observations, without principal resolution."""

import ctypes
import errno
import os
from dataclasses import dataclass, replace
from enum import Enum


class AclKind(Enum):
    ABSENT = "absent"
    EMPTY = "empty"
    DENY_ONLY = "deny_only"
    HAS_ALLOW = "has_allow"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AclObservation:
    kind: AclKind
    entries: int | None = None
    reason: str | None = None
    metadata_before: tuple[int, ...] | None = None
    metadata_after: tuple[int, ...] | None = None
    acl_kind: AclKind | None = None

    def record(self):
        return {
            "kind": self.kind.value,
            "entries": self.entries,
            "reason": self.reason,
            "before": self.metadata_before,
            "after": self.metadata_after,
            "acl_kind": self.acl_kind.value if self.acl_kind is not None else None,
        }


class DarwinAclApi:
    """Bind only the opaque copy/entry APIs needed for this observation."""

    def __init__(self):
        self.lib = ctypes.CDLL(None, use_errno=True)
        signatures = {
            "acl_get_fd": ([ctypes.c_int], ctypes.c_void_p),
            "acl_init": ([ctypes.c_int], ctypes.c_void_p),
            "acl_valid": ([ctypes.c_void_p], ctypes.c_int),
            "acl_size": ([ctypes.c_void_p], ctypes.c_ssize_t),
            "acl_get_entry": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.c_int,
            ),
            "acl_get_tag_type": (
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
                ctypes.c_int,
            ),
            "acl_free": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result

    def get(self, fd):
        ctypes.set_errno(0)
        value = self.lib.acl_get_fd(fd)
        return value, ctypes.get_errno()

    def empty(self):
        return self.lib.acl_init(0)

    def valid(self, value):
        # Apple acl_valid is a structural pointer guard, not semantic validation.
        return self.lib.acl_valid(value) == 0

    def size(self, value):
        return self.lib.acl_size(value)

    def entry(self, value, selector):
        entry = ctypes.c_void_p()
        if self.lib.acl_get_entry(value, selector, ctypes.byref(entry)) != 0:
            return None
        return entry.value

    def tag(self, entry):
        tag = ctypes.c_int()
        if self.lib.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
            return None
        return tag.value

    def free(self, value):
        return self.lib.acl_free(value) == 0


def _unknown(reason):
    return AclObservation(AclKind.UNKNOWN, reason=reason)


def _pointer(value):
    # ACL_REMOVE_ACL is (acl_t)1. Never pass a sentinel to pointer-dereferencing APIs.
    return type(value) is int and value > 1


def _metadata(fd):
    info = os.fstat(fd)
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_ctime_ns,
    )


def _classify(api, acl):
    if not _pointer(acl) or not api.valid(acl):
        return _unknown("invalid_acl_copy")
    empty = api.empty()
    if not _pointer(empty):
        return _unknown("empty_witness_unavailable")
    result = _unknown("empty_witness_invalid")
    try:
        if api.valid(empty):
            baseline, size = api.size(empty), api.size(acl)
            if type(baseline) is int and type(size) is int and 0 < baseline <= size <= 1048576:
                result = (
                    AclObservation(AclKind.EMPTY, 0) if size == baseline else _entries(api, acl)
                )
            else:
                result = _unknown("invalid_size_witness")
    finally:
        if not api.free(empty):
            result = _unknown("empty_witness_release_failed")
    return result


def _entries(api, acl):
    first = api.entry(acl, 0)  # FIRST must succeed before querying LAST.
    if not _pointer(first):
        return _unknown("first_entry_unavailable")
    last = api.entry(acl, -2)
    if not _pointer(last):
        return _unknown("last_entry_unavailable")
    # LAST advances Darwin's opaque iterator; reset FIRST before NEXT traversal.
    if api.entry(acl, 0) != first:
        return _unknown("entry_iterator_changed")
    current, seen, has_allow = first, set(), False
    for _ in range(128):
        if not _pointer(current) or current in seen:
            return _unknown("incomplete_or_repeated_entry")
        seen.add(current)
        tag = api.tag(current)
        if tag not in {1, 2}:
            return _unknown("entry_tag_unavailable")
        has_allow = has_allow or tag == 1
        if current == last:
            return AclObservation(AclKind.HAS_ALLOW if has_allow else AclKind.DENY_ONLY, len(seen))
        current = api.entry(acl, -1)
    return _unknown("entry_limit_or_missing_last")


def inspect_acl(fd):
    """Observe one open descriptor; unknown/error/partial copies never mean absence."""
    acl, api = None, None
    before = after = observed_kind = None
    result = _unknown("acl_api_unavailable")
    try:
        before = _metadata(fd)
        api = DarwinAclApi()
        acl, error = api.get(fd)
        if not acl:
            result = (
                AclObservation(AclKind.ABSENT, 0)
                if error == errno.ENOENT
                else _unknown("acl_copy_unavailable")
            )
        else:
            result = _classify(api, acl)
        observed_kind = result.kind
        after = _metadata(fd)
        if after != before:
            result = _unknown("descriptor_changed")
    except (OSError, AttributeError, TypeError, ValueError, OverflowError):
        result = _unknown("acl_observation_failed")
    finally:
        if api is not None and _pointer(acl):
            try:
                if not api.free(acl):
                    result = _unknown("acl_copy_release_failed")
            except (OSError, AttributeError, TypeError, ValueError, OverflowError):
                result = _unknown("acl_copy_release_failed")
    return replace(result, metadata_before=before, metadata_after=after, acl_kind=observed_kind)
