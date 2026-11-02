"""Actual owned metadata and deterministic race controls, without account files."""

import os

import pytest

from ai_task_manager.adapters.hosts.bridge.codex_home import (
    CodexHomeBoundary,
    SourceFile,
    no_access_acl,
)
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.codex_configuration_helpers import files


def test_owned_nonwritable_by_others_home_is_accepted_without_chmod(tmp_path, monkeypatch):
    for mode in (0o700, 0o755):
        home = files(tmp_path / str(mode), mode=mode)
        before = home.stat().st_mode
        guard = CodexHomeBoundary(home)
        guard.check()
        assert home.stat().st_mode == before
        home.chmod(0o777)
        with pytest.raises(AccessDenied):
            guard.check()
        assert home.stat().st_mode & 0o777 == 0o777
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(alias)
    owned = files(tmp_path / "owned")
    real_uid = os.getuid()
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(owned)


def test_protected_credential_and_user_config_files_are_exact(tmp_path, monkeypatch):
    home = files(tmp_path)
    original = os.read

    def no_credential_read(fd, amount):
        assert os.fstat(fd).st_ino != (home / "auth.json").stat().st_ino
        return original(fd, amount)

    monkeypatch.setattr(os, "read", no_credential_read)
    CodexHomeBoundary(home).check()
    for name in ("auth.json", "config.toml"):
        path = home / name
        path.chmod(0o644)
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(home)
        path.chmod(0o600)
        backup = home / (name + ".backup")
        path.rename(backup)
        path.symlink_to(backup)
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(home)
        path.unlink()
        path.hardlink_to(backup)
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(home)
        path.unlink()
        backup.rename(path)
    credential = home / "auth.json"
    credential.rename(home / "saved")
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(home)
    os.mkfifo(credential, 0o600)
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(home)


def test_acl_write_grants_and_unknown_acl_inspection_refuse(tmp_path):
    home = files(tmp_path)
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert no_access_acl(fd) is True
    finally:
        os.close(fd)
    for observation in (False, None):
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(home, acl_query=lambda fd: observation)


def test_directory_or_file_replacement_race_invalidates_the_source_binding(tmp_path, monkeypatch):
    home = files(tmp_path)
    original = os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = original(path, flags, *args, **kwargs)
        if path == "config.toml" and not swapped:
            swapped = True
            replacement = home / "replacement"
            replacement.write_text("# newer source\n")
            replacement.chmod(0o600)
            replacement.replace(home / "config.toml")
        return fd

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(home)
    assert swapped


def test_config_source_seals_reject_content_identity_and_permission_changes(tmp_path):
    for kind in ("content", "inode", "mode"):
        home = files(tmp_path / kind)
        guard = CodexHomeBoundary(home)
        config = home / "config.toml"
        if kind == "content":
            config.write_text("# changed\n")
        elif kind == "inode":
            replacement = home / "replacement"
            replacement.write_bytes(config.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(config)
        else:
            config.chmod(0o644)
        with pytest.raises(AccessDenied):
            guard.check()


def test_configuration_inventory_preserves_unknown_and_expected_absence(tmp_path, monkeypatch):
    home = files(tmp_path)
    guard = CodexHomeBoundary(home)
    source = home / "new.config.toml"
    source.write_text("# unexpected source")
    source.chmod(0o600)
    with pytest.raises(AccessDenied):
        guard.check()
    source.unlink()
    unprotected = tmp_path / "unprotected-source-parent"
    unprotected.mkdir(mode=0o777)
    unprotected.chmod(0o777)
    with pytest.raises(AccessDenied):
        CodexHomeBoundary(home, extra_sources=(SourceFile(unprotected / "missing.toml"),))
    for limit, amount in (("MAX_FILES", 1), ("MAX_FILE_BYTES", 4), ("MAX_TOTAL_BYTES", 4)):
        (home / "config.toml").write_text("# more than four bytes")
        with monkeypatch.context() as scoped:
            scoped.setattr(CodexHomeBoundary, limit, amount)
            with pytest.raises(AccessDenied):
                CodexHomeBoundary(home)


def test_native_cache_refresh_has_a_distinct_metadata_lifecycle(tmp_path):
    home = files(tmp_path)
    guard = CodexHomeBoundary(home)
    before = guard.credential_metadata()
    replacement = home / "refresh"
    replacement.write_text("new-fixture-cache-no-secret")
    replacement.chmod(0o600)
    replacement.replace(home / "auth.json")
    guard.check()  # An admitted native cache refresh does not change config seals.
    with pytest.raises(AccessDenied):
        guard.check_readonly_credential(before)
    (home / "auth.json").chmod(0o644)
    with pytest.raises(AccessDenied):
        guard.check()
