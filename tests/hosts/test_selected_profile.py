"""Explicit selected subscription profiles; no native process or account read."""

import os
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.policy import clean_environment
from ai_task_manager.domains.work.errors import AccessDenied


@pytest.mark.parametrize("name", [".claude_info", ".claude", ".another-selected-profile"])
def test_selected_owned_physical_profile_is_used_exactly(tmp_path, name):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    profile = home / name
    profile.mkdir(mode=0o700)
    environment = clean_environment(home, claude_profile=profile)
    assert environment["HOME"] == str(home)
    expected = {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_ENTRYPOINT",
    }
    if name == ".claude":
        assert "CLAUDE_CONFIG_DIR" not in environment
        expected.remove("CLAUDE_CONFIG_DIR")
    else:
        assert environment["CLAUDE_CONFIG_DIR"] == str(profile)
    assert set(environment) == expected


@pytest.mark.parametrize("fault", ["symlink", "outside", "writable", "missing", "foreign_owner"])
def test_untrusted_selected_profile_refuses_without_fallback(tmp_path, monkeypatch, fault):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / ".claude_roni").mkdir(mode=0o700)
    profile = home / ".selected"
    if fault != "missing":
        profile.mkdir(mode=0o700)
    if fault == "symlink":
        profile.rmdir()
        profile.symlink_to(home / ".claude_roni", target_is_directory=True)
    elif fault == "outside":
        profile = tmp_path / "foreign"
        profile.mkdir(mode=0o700)
    elif fault == "writable":
        profile.chmod(0o777)
    elif fault == "foreign_owner":
        original = Path.stat

        def wrong_owner(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == profile:
                fields = list(value)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return value

        monkeypatch.setattr(Path, "stat", wrong_owner)
    with pytest.raises(AccessDenied):
        clean_environment(home, claude_profile=profile)
