"""Opt-in traces for authored preflights; never enable native account operations."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge import codex_auth, codex_home, darwin_acl
from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy


class Trace:
    LIMIT = 128

    def __init__(self, nodeid):
        self.nodeid, self.records, self.dropped = nodeid, [], 0
        self.local = threading.local()
        self.started = time.monotonic()

    @property
    def in_core(self):
        return bool(getattr(self.local, "core_depth", 0))

    def add(self, kind, **value):
        if len(self.records) < self.LIMIT:
            self.records.append(
                {
                    "kind": kind,
                    "elapsed": time.monotonic() - self.started,
                    "core_start_active": self.in_core,
                    "auth_active": bool(getattr(self.local, "auth_depth", 0)),
                    **value,
                }
            )
        else:
            self.dropped += 1

    def flush(self):
        assert not self.in_core
        leaf = "preflight-" + hashlib.sha256(self.nodeid.encode()).hexdigest()[:16] + ".json"
        path = Path(os.environ["AITM_TEST_EVIDENCE_DIR"]) / leaf
        path.write_text(
            json.dumps(
                {
                    "scope": "Authored fixture diagnosis; no native account data",
                    "nodeid": self.nodeid,
                    "records": self.records,
                    "dropped": self.dropped,
                },
                indent=2,
            )
            + "\n"
        )
        path.chmod(0o600)


def _stat(path):
    try:
        info = path.lstat()
        return {
            name: getattr(info, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_uid",
                "st_gid",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
    except OSError as error:
        return {"error_class": type(error).__name__, "errno": error.errno}


def _sources(policy):
    source = policy.codex_settings.sources
    paths = [("native_executable", policy.executable), ("auth_cache", source.home / "auth.json")]
    paths.extend(
        ("ancestor_" + str(n), p) for n, p in enumerate((source.home, *source.home.parents))
    )
    paths.extend(("protected_source", Path(p)) for p in source._sources)
    if len(paths) > 96:
        return {"unknown": "diagnostic_path_limit"}

    def expected(role, path):
        if role == "native_executable":
            value = policy._executable_identity
            if not isinstance(value, tuple) or len(value) != 5:
                return {"diagnostic_unknown": "executable_identity_shape"}
            return dict(
                zip(
                    ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"),
                    value,
                    strict=True,
                )
            )
        if role == "auth_cache":
            metadata = source._credential
        elif role == "protected_source":
            entry = source._sources.get(str(path))
            if isinstance(entry, dict):
                return {"expected_absent": entry.get("absent") is True}
            if not isinstance(entry, tuple) or len(entry) != 2:
                return {"diagnostic_unknown": "protected_source_pair_shape"}
            metadata = entry[0]  # The second item is a content digest, never metadata.
        else:
            return None
        if not isinstance(metadata, tuple) or len(metadata) != 4:
            return {"diagnostic_unknown": "source_metadata_shape"}
        identity, size, modified, changed = metadata
        if (
            not isinstance(identity, tuple)
            or len(identity) != 5
            or not all(type(v) is int for v in (*identity, size, modified, changed))
        ):
            return {"diagnostic_unknown": "source_identity_shape"}
        return {
            **dict(
                zip(("st_dev", "st_ino", "st_uid", "mode_bits", "st_nlink"), identity, strict=True)
            ),
            "st_size": size,
            "st_mtime_ns": modified,
            "st_ctime_ns": changed,
        }

    return {
        hashlib.sha256(str(path).encode()).hexdigest(): {
            "role": role,
            "metadata": _stat(path),
            "sealed_metadata": expected(role, path),
        }
        for role, path in paths
    }


def _message_code(error):
    known = {
        "Native executable changed or is not explicitly approved": "native_executable_changed",
        "Native source generation changed; do not silently re-seal": "source_generation_changed",
        "Protected source metadata changed after preparation": "prepared_source_changed",
        "Protected source path cannot be verified": "source_path_unverified",
        "Native ordinary user identity changed": "ordinary_identity_changed",
        "Native configuration digest omits its protected source plan": "config_digest_changed",
        "ACL policy binding changed; prepare a new source binding": "acl_binding_changed",
        "The bound ACL predicate changed; prepare a new source binding": "acl_predicate_changed",
    }
    return known.get(str(error), "other_exception_class_only")


def _install_policy(monkeypatch, trace):
    original = NativePolicy.check

    def checked(policy):
        # This is a diagnostic snapshot, not admission authority. No extra path I/O under Core.
        before = None if trace.in_core else _sources(policy)
        if before is not None:
            for value in before.values():
                sealed = value.get("sealed_metadata") if isinstance(value, dict) else None
                if isinstance(sealed, dict) and "diagnostic_unknown" in sealed:
                    trace.add("diagnostic_unknown", reason=sealed["diagnostic_unknown"])
        try:
            return original(policy)
        except BaseException as error:
            after = None if trace.in_core else _sources(policy)
            changes = (
                None
                if before is None
                else {
                    key: {"before": before.get(key), "after": after.get(key)}
                    for key in before.keys() | after.keys()
                    if before.get(key) != after.get(key)
                }
            )
            trace.add(
                "policy_exception",
                error_class=type(error).__name__,
                message_code=_message_code(error),
                metadata_changes=changes,
            )
            raise

    monkeypatch.setattr(NativePolicy, "check", checked)
    inspect = darwin_acl.inspect_acl

    def inspected(fd):
        def info():
            try:
                value = os.fstat(fd)
                return {
                    name: getattr(value, name)
                    for name in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_ctime_ns")
                }
            except OSError as error:
                return {"error_class": type(error).__name__, "errno": error.errno}

        before = info()
        observed = inspect(fd)
        if observed.kind is darwin_acl.AclKind.UNKNOWN:
            trace.add("acl_unknown", reason=observed.reason, before=before, after=info())
        return observed

    monkeypatch.setattr(darwin_acl, "inspect_acl", inspected)
    metadata = codex_home.CodexHomeBoundary.check_metadata

    def current(source):
        try:
            return metadata(source)
        except BaseException as error:
            trace.add(
                "current_metadata_exception",
                error_class=type(error).__name__,
                message_code=_message_code(error),
            )
            raise

    monkeypatch.setattr(codex_home.CodexHomeBoundary, "check_metadata", current)


def _install_auth(monkeypatch, trace):
    observe = codex_auth.CodexAuthProbe.observe

    def observed(probe, policy, binding, directory):
        previous = getattr(trace.local, "auth_depth", 0)
        trace.local.auth_depth = previous + 1
        trace.add(
            "auth_enter", binding_deadline=binding.deadline, authority_clock=probe.journal.clock()
        )
        try:
            return observe(probe, policy, binding, directory)
        except BaseException as error:
            trace.add(
                "auth_exception",
                error_class=type(error).__name__,
                message_code=_message_code(error),
            )
            raise
        finally:
            trace.local.auth_depth = previous

    monkeypatch.setattr(codex_auth.CodexAuthProbe, "observe", observed)
    supervise = codex_auth.supervise

    def supervised(group, *, timeout_seconds, boundary_check=None):
        trace.add(
            "status_supervision",
            timeout_seconds=timeout_seconds,
            leader_pid=group.process.pid,
            group_id=group.group_id,
            start_identity=group.start_identity,
        )

        def boundary():
            try:
                return boundary_check() if boundary_check is not None else None
            except BaseException as error:
                trace.add(
                    "status_boundary_exception",
                    error_class=type(error).__name__,
                    message_code=_message_code(error),
                )
                raise

        result = supervise(group, timeout_seconds=timeout_seconds, boundary_check=boundary)
        trace.add(
            "status_completion",
            outcome=result.get("outcome"),
            reason=result.get("reason"),
            returncode=result.get("returncode"),
            group=result.get("group"),
        )
        return result

    monkeypatch.setattr(codex_auth, "supervise", supervised)


def _composition(original, monkeypatch, trace):
    def create(*, deny=False, provider_fail=False, early_tool=False):
        value = original(deny=deny, provider_fail=provider_fail, early_tool=early_tool)
        core, factory, workers = value[0].rig.core, value[4], value[3]
        start = core.execution.start

        def started(*args, **kwargs):
            previous = getattr(trace.local, "core_depth", 0)
            trace.local.core_depth = previous + 1
            try:
                return start(*args, **kwargs)
            except BaseException as error:
                trace.add("core_start_exception", error_class=type(error).__name__)
                raise
            finally:
                trace.local.core_depth = previous

        monkeypatch.setattr(core.execution, "start", started)
        authorize = factory.permits.authorize_auth

        def authorized(policy, binding):
            trace.add(
                "auth_permit_enter",
                binding_deadline=binding.deadline,
                executable_is_authored=policy.executable.name == "synthetic-cli",
            )
            try:
                result = authorize(policy, binding)
            except BaseException as error:
                trace.add("auth_permit_exception", error_class=type(error).__name__)
                raise
            trace.add("auth_permit_returned")
            return result

        monkeypatch.setattr(factory.permits, "authorize_auth", authorized)
        startup = workers.proofs.startup

        def proof(scope, attempt_id, observation_id, *, host_ready_ref=None):
            trace.add("startup_proof_enter", attempt_id=attempt_id)
            result = startup(scope, attempt_id, observation_id, host_ready_ref=host_ready_ref)
            trace.add("startup_proof_returned", attempt_id=attempt_id)
            return result

        monkeypatch.setattr(workers.proofs, "startup", proof)
        return value

    return create


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    """Keep the original selected pytest IDs and actual production object graph."""
    if "composition" not in item.funcargs:
        return (yield)
    trace = Trace(item.nodeid)
    original = item.funcargs["composition"]
    # Share the test's function-scoped patch stack. Its later undo restores nested
    # test patches before ours; a shorter private context would leak restored wrappers.
    monkeypatch = item._request.getfixturevalue("monkeypatch")
    _install_policy(monkeypatch, trace)
    _install_auth(monkeypatch, trace)
    item.funcargs["composition"] = _composition(original, monkeypatch, trace)
    try:
        return (yield)
    finally:
        item.funcargs["composition"] = original
        trace.flush()
