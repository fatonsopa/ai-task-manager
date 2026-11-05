"""Selected executable inventory reuse retains original hashes and routing."""

import os
import subprocess
from pathlib import Path

import pytest

from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.domains.work.errors import AccessDenied


def capture(root, monkeypatch):
    helpers = root / "helpers"
    helpers.mkdir(mode=0o700)
    (helpers / "git-alpha").write_bytes(b"authored immutable helper")
    (helpers / "git-alpha").chmod(0o700)
    (helpers / "git-beta").symlink_to("git-alpha")
    original = subprocess.run

    def metadata(argv, *args, **kwargs):
        if argv == ["/usr/bin/git", "--exec-path"]:
            return subprocess.CompletedProcess(argv, 0, (str(helpers) + "\n").encode(), b"")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", metadata)
    return helpers, Executable.capture("/usr/bin/git", GitProfile.environment(root / "home"))


def test_unchanged_inventory_reuses_only_its_original_manifest(tmp_path, monkeypatch):
    _, selected = capture(tmp_path, monkeypatch)
    original = selected.exec_manifest
    monkeypatch.setattr(
        Executable,
        "_manifest",
        lambda *args: pytest.fail("Unchanged selected inventory was rehashed"),
    )
    selected.validate()
    assert selected.exec_manifest == original


@pytest.mark.parametrize(
    "change", ["add", "remove", "replace", "permission", "hardlink", "routing", "bytes"]
)
def test_any_inventory_or_routing_witness_change_refuses(tmp_path, monkeypatch, change):
    helpers, selected = capture(tmp_path, monkeypatch)
    alpha = helpers / "git-alpha"
    if change == "add":
        (helpers / "git-gamma").write_bytes(b"added helper")
    elif change == "remove":
        (helpers / "git-beta").unlink()
    elif change == "replace":
        replacement = helpers / "replacement"
        replacement.write_bytes(alpha.read_bytes())
        replacement.chmod(0o700)
        replacement.replace(alpha)
    elif change == "permission":
        alpha.chmod(0o600)
    elif change == "hardlink":
        os.link(alpha, helpers / "git-gamma")
    elif change == "routing":
        (helpers / "alternate").write_bytes(alpha.read_bytes())
        (helpers / "git-beta").unlink()
        (helpers / "git-beta").symlink_to("alternate")
    else:
        info = alpha.stat()
        alpha.write_bytes(b"changed immutable helper")
        os.utime(alpha, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(AccessDenied):
        selected.validate()


def test_inventory_change_during_initial_hash_refuses(tmp_path, monkeypatch):
    original = Executable._manifest

    def changed(path):
        result = original(path)
        (Path(path) / "git-added-during-hash").write_bytes(b"new helper")
        return result

    monkeypatch.setattr(Executable, "_manifest", changed)
    with pytest.raises(AccessDenied, match="during initial hashing"):
        capture(tmp_path, monkeypatch)


def test_intermediate_symlink_component_cannot_redirect_unchanged_root_entry(tmp_path, monkeypatch):
    helpers, _ = capture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    for version, raw in (("v1", b"original helper"), ("v2", b"redirected helper")):
        directory = outside / version
        directory.mkdir(mode=0o700)
        (directory / "tool").write_bytes(raw)
        (directory / "tool").chmod(0o700)
    current = outside / "current"
    current.symlink_to("v1")
    (helpers / "git-through-directory").symlink_to(current / "tool")
    selected = Executable.capture("/usr/bin/git", GitProfile.environment(tmp_path / "home"))
    root_before = (helpers / "git-through-directory").lstat()
    current.unlink()
    current.symlink_to("v2")
    assert (helpers / "git-through-directory").lstat().st_ino == root_before.st_ino
    with pytest.raises(AccessDenied):
        selected.validate()
