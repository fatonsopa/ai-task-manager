"""Darwin metadata policy controls; authored hosts/providers remain synthetic."""

import errno
import os
import sys
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge import codex_home, darwin_acl
from ai_task_manager.adapters.hosts.bridge.codex_home import (
    CodexHomeBoundary,
    no_access_acl,
    no_acl_grants,
)
from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.acl_fixtures import (
    AuthoredAclApi,
    descriptor,
    observe_fixture_boundaries,
    set_fixture_acl,
)
from tests.hosts.codex_configuration_helpers import files
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_startup_configuration_fence import (
    after_proof,
    assert_no_start,
    guard_core_checks,
    persist,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin descriptor ACL component")


def observe(tmp_path, monkeypatch, api):
    path = tmp_path / "owned-leaf"
    path.write_text("credential-free fixture")
    path.chmod(0o600)
    monkeypatch.setattr(darwin_acl, "DarwinAclApi", lambda: api)
    with descriptor(path) as fd:
        return darwin_acl.inspect_acl(fd)


@pytest.mark.parametrize("variant", ["actual", "enoent", "zero", "enotsup", "einval"])
def test_existing_absent_acl_remains_accepted(tmp_path, monkeypatch, variant):
    if variant == "actual":
        path = tmp_path / "actual-owned-leaf"
        path.touch(mode=0o600)
        with descriptor(path) as fd:
            assert darwin_acl.inspect_acl(fd).kind is darwin_acl.AclKind.ABSENT
            assert no_access_acl(fd) is no_acl_grants(fd) is True
    else:
        code = {
            "enoent": errno.ENOENT,
            "zero": 0,
            "enotsup": errno.ENOTSUP,
            "einval": errno.EINVAL,
        }[variant]
        result = observe(tmp_path, monkeypatch, AuthoredAclApi(fault="get_error", error=code))
        assert result.kind is (
            darwin_acl.AclKind.ABSENT if variant == "enoent" else darwin_acl.AclKind.UNKNOWN
        )


def test_positive_empty_acl_has_no_entry_iteration(tmp_path, monkeypatch):
    api = darwin_acl.DarwinAclApi()
    empty = api.empty()
    try:
        assert darwin_acl._classify(api, empty).kind is darwin_acl.AclKind.EMPTY
    finally:
        assert api.free(empty)
    path = tmp_path / "owned-empty"
    path.touch(mode=0o600)
    set_fixture_acl(path, ())
    with descriptor(path) as fd:
        actual = darwin_acl.inspect_acl(fd)
        # APFS may canonicalize a stored empty ACL to positive absent-property.
        assert actual.kind in {darwin_acl.AclKind.EMPTY, darwin_acl.AclKind.ABSENT}
    authored = AuthoredAclApi(tags=())
    assert observe(tmp_path, monkeypatch, authored).kind is darwin_acl.AclKind.EMPTY
    assert not any(call[0] == "entry" for call in authored.calls)


@pytest.mark.parametrize("count", [1, 2, 128])
def test_actual_deny_only_acl_retains_posix_path_guards(tmp_path, count):
    home = files(tmp_path)
    set_fixture_acl(home, (2,) * count)
    with descriptor(home) as fd:
        result = darwin_acl.inspect_acl(fd)
        assert result.kind is darwin_acl.AclKind.DENY_ONLY and result.entries == count
        assert no_access_acl(fd) is False and no_acl_grants(fd) is True
    guard = CodexHomeBoundary(home)
    guard.check_metadata()
    home.chmod(0o777)
    with pytest.raises(AccessDenied):
        guard.check_metadata()
    home.chmod(0o700)
    alias = tmp_path / "symlink"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(alias)


@pytest.mark.parametrize(
    "tags,permission",
    [((1,), 1 << 2), ((2, 1), 1 << 2), ((1, 2), 1 << 2), ((2, 1, 2), 1 << 2), ((1,), 1 << 1)],
)
def test_any_allow_entry_refuses_regardless_of_order(tmp_path, tags, permission):
    home = files(tmp_path)
    set_fixture_acl(home, tags, permission=permission)
    with descriptor(home) as fd:
        assert darwin_acl.inspect_acl(fd).kind is darwin_acl.AclKind.HAS_ALLOW
        assert no_acl_grants(fd) is False
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(home)


@pytest.mark.parametrize(
    "fault",
    [
        "invalid",
        "empty_missing",
        "invalid_empty",
        "first",
        "last",
        "tag",
        "raise",
        "free",
        "free_empty",
        "missing_api",
        "unknown_tag",
    ],
)
def test_unknown_tag_and_every_metadata_failure_refuse(tmp_path, monkeypatch, fault):
    api = AuthoredAclApi(tags=(9,) if fault == "unknown_tag" else (2,), fault=fault)
    if fault == "missing_api":

        def missing():
            raise AttributeError("authored missing binding")

        monkeypatch.setattr(darwin_acl, "DarwinAclApi", missing)
        path = tmp_path / "owned"
        path.touch(mode=0o600)
        with descriptor(path) as fd:
            assert darwin_acl.inspect_acl(fd).kind is darwin_acl.AclKind.UNKNOWN
    else:
        assert observe(tmp_path, monkeypatch, api).kind is darwin_acl.AclKind.UNKNOWN
        if fault != "raise":
            assert 100 in api.freed


@pytest.mark.parametrize("variant", ["multi", "premature", "repeat", "overflow", "changed_first"])
def test_independent_last_entry_must_be_reached(tmp_path, monkeypatch, variant):
    api = AuthoredAclApi(tags=(2,) * (129 if variant == "overflow" else 3), fault=variant)
    result = observe(tmp_path, monkeypatch, api)
    assert result.kind is (
        darwin_acl.AclKind.DENY_ONLY if variant == "multi" else darwin_acl.AclKind.UNKNOWN
    )
    selectors = [call[1] for call in api.calls if call[0] == "entry"]
    assert selectors[:3] == [0, -2, 0]


@pytest.mark.parametrize("variant", ["special", "negative_size", "contradictory_size", "huge_size"])
def test_special_pointer_and_false_size_witness_refuse(tmp_path, monkeypatch, variant):
    api = AuthoredAclApi(fault=variant)
    assert observe(tmp_path, monkeypatch, api).kind is darwin_acl.AclKind.UNKNOWN
    if variant == "special":
        assert not api.freed and not any(call[0] in {"valid", "size"} for call in api.calls)


@pytest.mark.parametrize("field", ["mode", "owner", "inode", "ctime"])
def test_fd_metadata_change_during_acl_read_refuses(tmp_path, monkeypatch, field):
    api = AuthoredAclApi()
    real = darwin_acl._metadata
    calls = 0

    def changed(fd):
        nonlocal calls
        calls += 1
        result = list(real(fd))
        if calls > 1:
            index = {"mode": 4, "owner": 2, "inode": 1, "ctime": 6}[field]
            result[index] += 1
        return tuple(result)

    monkeypatch.setattr(darwin_acl, "_metadata", changed)
    assert observe(tmp_path, monkeypatch, api).kind is darwin_acl.AclKind.UNKNOWN


@pytest.mark.parametrize("tags", [(), (2,), (1,), (2, 1)])
def test_legacy_absence_helper_keeps_its_meaning(tmp_path, monkeypatch, tags):
    path = tmp_path / "owned"
    path.touch(mode=0o600)
    monkeypatch.setattr(darwin_acl, "DarwinAclApi", lambda: AuthoredAclApi(tags=tags))
    with descriptor(path) as fd:
        assert no_access_acl(fd) is (not tags)
        assert no_acl_grants(fd) is (1 not in tags)
    monkeypatch.setattr(darwin_acl, "DarwinAclApi", lambda: AuthoredAclApi(fault="tag"))
    with descriptor(path) as fd:
        assert no_access_acl(fd) is no_acl_grants(fd) is None


def test_current_acl_grant_fences_cached_startup_proof(composition, monkeypatch, request):
    value = composition()
    with observe_fixture_boundaries(value, monkeypatch, request.node.name):

        def grant(native):
            set_fixture_acl(native.policy.codex_settings.sources.home, (1,))

        observed = after_proof(value, grant)
        assert_no_start(value)
        assert len(observed) == 1, "The actual late-grant callback must be reached"
        persist(
            "late-current-acl-grant",
            [{"worker_started": 0, "provider_confirmation": 0, "status": "Todo"}],
        )


def test_admission_acl_checks_do_not_read_content_or_names(composition, monkeypatch, request):
    value = composition()
    with observe_fixture_boundaries(value, monkeypatch, request.node.name):
        guard_core_checks(value, monkeypatch)
        api = darwin_acl.DarwinAclApi()
        allowed = {
            "acl_get_fd",
            "acl_init",
            "acl_valid",
            "acl_size",
            "acl_get_entry",
            "acl_get_tag_type",
            "acl_free",
        }
        native_library = api.lib

        class StrictLibrary:
            def __getattr__(self, name):
                assert name in allowed, name
                return getattr(native_library, name)

        api.lib = StrictLibrary()
        monkeypatch.setattr(darwin_acl, "DarwinAclApi", lambda: api)
        dispatch = start(value)
        assert dispatch.state == "running" and value[5].calls == [value[1].id]


@pytest.mark.parametrize(
    "variant",
    [
        "fingerprint",
        "cached_reference",
        "predicate_rebind",
        "instance_rebind",
        "registered_default_rebind",
        "custom_stable",
        "preconstruct_registered",
        "preconstruct_predicate",
    ],
)
def test_acl_policy_generation_changes_configuration_binding(
    tmp_path, monkeypatch, composition, variant
):
    if variant in {"preconstruct_registered", "preconstruct_predicate"}:
        home = files(tmp_path)
        symbol = (
            "_REGISTERED_DEFAULT_ACL_QUERY"
            if variant == "preconstruct_registered"
            else "no_acl_grants"
        )
        monkeypatch.setattr(codex_home, symbol, lambda fd: True)
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(home)
        return
    if variant in {
        "cached_reference",
        "predicate_rebind",
        "instance_rebind",
        "registered_default_rebind",
    }:
        value = composition()

        def change(native):
            if variant == "cached_reference":
                monkeypatch.setattr(
                    codex_home, "ACL_POLICY_GENERATION", "authored-next-policy-generation"
                )
            elif variant == "predicate_rebind":
                monkeypatch.setattr(codex_home, "no_acl_grants", lambda fd: True)
            elif variant == "registered_default_rebind":
                monkeypatch.setattr(codex_home, "_REGISTERED_DEFAULT_ACL_QUERY", lambda fd: True)
            else:
                native.policy.codex_settings.sources.acl_query = lambda fd: True

        observed = after_proof(value, change)
        assert_no_start(value)
        assert len(observed) == 1, "The actual late policy/predicate callback must be reached"
        return
    if variant == "custom_stable":

        def query(fd):
            return True

        source = CodexHomeBoundary(files(tmp_path), acl_query=query)
        source.check_metadata()
        monkeypatch.setattr(codex_home, "no_acl_grants", lambda fd: False)
        source.check_metadata()  # A stable explicit trusted override is independent of the default.
        source.acl_query = lambda fd: True
        with pytest.raises(AccessDenied):
            source.check_metadata()
        return
    home = files(tmp_path)
    source = CodexHomeBoundary(home)
    settings = CodexSettings(source, "synthetic-no-model", "high")
    before = source.fingerprint
    original = settings.fingerprint
    old_sources = digest({"home": source._home_identity, "sources": source._sources})
    assert before != old_sources
    monkeypatch.setattr(codex_home, "ACL_POLICY_GENERATION", "authored-next-policy-generation")
    with pytest.raises(AccessDenied):
        source.check_metadata()
    replacement = CodexHomeBoundary(home)
    assert replacement.fingerprint != before
    assert CodexSettings(replacement, settings.model, settings.effort).fingerprint != original


def test_normal_ancestor_read_only_observation_is_separate():
    home = Path(os.environ["AITM_ACL_NORMAL_ANCESTOR"])
    assert home.is_absolute() and not home.is_symlink()
    before = home.lstat()
    with descriptor(home) as fd:
        observed = darwin_acl.inspect_acl(fd)
    after = home.lstat()
    assert (before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_ctime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_mode,
        after.st_ctime_ns,
    )
    persist(
        "normal-ancestor-acl-only",
        [
            {
                "kind": observed.kind.value,
                "entries": observed.entries,
                "reason": observed.reason,
                "account_or_profile_observed": False,
                "writes": False,
            }
        ],
    )
    assert observed.kind is not darwin_acl.AclKind.UNKNOWN


@pytest.mark.parametrize("error", [None, errno.ENODATA, errno.ENOTSUP, errno.EACCES, errno.EINVAL])
def test_linux_metadata_branch_is_preserved(tmp_path, monkeypatch, error):
    monkeypatch.setattr(codex_home.sys, "platform", "linux")

    def getxattr(fd, key):
        assert key == "system.posix_acl_access"
        if error is not None:
            raise OSError(error, "authored xattr observation")
        return b"authored-existing-acl"

    monkeypatch.setattr(os, "getxattr", getxattr, raising=False)
    for query in (no_access_acl, no_acl_grants):
        assert query(42) is (True if error == errno.ENODATA else False if error is None else None)
