import os

import pytest

from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.domains.work.errors import AccessDenied, InvalidTransition, StaleVersion
from tests.runtime.helpers import RuntimeRig


@pytest.fixture
def managed_workspace(tmp_path, journal):
    r = RuntimeRig(tmp_path / "runtime")
    dispatch = r.start()
    r.admit(dispatch)
    gateway = ManagedToolGateway(
        r.rig.core,
        r.rig.handle,
        dispatch,
        Workspace(dispatch.resources.worktree),
        r.resources,
        journal,
        deadline=journal.clock() + 30,
    )
    try:
        yield r, dispatch, gateway
    finally:
        r.close()


def test_work_is_refused_until_actual_core_lease_is_admitted(managed_workspace):
    _, dispatch, gateway = managed_workspace
    with pytest.raises(AccessDenied):
        gateway.call(
            "write_file",
            {
                "path": "code.py",
                "text": "answer=42",
                "expected_hash": None,
                "operation_key": "edit",
            },
        )
    assert not (dispatch.resources.worktree / "code.py").exists()
    gateway.admit()
    result = gateway.call(
        "write_file",
        {"path": "code.py", "text": "answer=42", "expected_hash": None, "operation_key": "edit"},
    )
    assert gateway.call("read_file", {"path": "code.py"})["sha256"] == result["sha256"]


def test_hold_revokes_next_edit_and_retains_current_file(managed_workspace):
    r, dispatch, gateway = managed_workspace
    gateway.admit()
    gateway.call(
        "write_file",
        {"path": "code.py", "text": "before", "expected_hash": None, "operation_key": "first"},
    )
    r.hold(dispatch)
    with pytest.raises(InvalidTransition):
        gateway.call(
            "write_file",
            {"path": "code.py", "text": "after", "expected_hash": None, "operation_key": "second"},
        )
    assert (dispatch.resources.worktree / "code.py").read_text() == "before"


def test_unsettled_edit_intent_does_not_blindly_repeat_publication(managed_workspace):
    _, dispatch, gateway = managed_workspace
    gateway.admit()
    args = {"path": "code.py", "text": "first", "expected_hash": None, "operation_key": "same"}
    gateway.call("write_file", args)
    with pytest.raises(AccessDenied):
        gateway.call("write_file", args)
    assert (dispatch.resources.worktree / "code.py").read_text() == "first"


@pytest.mark.parametrize("state", ["unadmitted", "held", "expired", "duplicate"])
def test_create_uses_the_same_lease_and_intent_fences(managed_workspace, journal, state):
    r, dispatch, gateway = managed_workspace
    args = {"path": "created.py", "text": "owned", "operation_key": "create"}
    if state != "unadmitted":
        gateway.admit()
    if state == "held":
        r.hold(dispatch)
    elif state == "expired":
        journal.clock = lambda: gateway.deadline
    elif state == "duplicate":
        gateway.call("create_file", args)
    with pytest.raises((AccessDenied, InvalidTransition)):
        gateway.call("create_file", args)
    path = dispatch.resources.worktree / args["path"]
    if state == "duplicate":
        assert path.read_text() == "owned"
    else:
        assert not path.exists()


@pytest.mark.parametrize("first", ["create_file", "write_file"])
def test_create_and_write_cannot_reuse_each_others_operation_key(managed_workspace, first):
    _, dispatch, gateway = managed_workspace
    gateway.admit()
    args = {"path": "first.py", "text": "first", "operation_key": "shared"}
    gateway.call(first, {**args, **({"expected_hash": None} if first == "write_file" else {})})
    second = "write_file" if first == "create_file" else "create_file"
    with pytest.raises(AccessDenied):
        gateway.call(second, {**args, "path": "second.py",
                             **({"expected_hash": None} if second == "write_file" else {})})
    assert (dispatch.resources.worktree / "first.py").read_text() == "first"
    assert not (dispatch.resources.worktree / "second.py").exists()


def test_create_publishes_without_replacing_a_racing_existing_file(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    link = os.link

    def racing_link(source, target, *, src_dir_fd, dst_dir_fd, follow_symlinks):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dst_dir_fd)
        with os.fdopen(fd, "wb") as foreign:
            foreign.write(b"concurrent-owned-content")
        return link(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(StaleVersion):
        workspace.create("racing.py", "must-not-overwrite")
    assert (tmp_path / "racing.py").read_bytes() == b"concurrent-owned-content"
    assert not list(tmp_path.glob(".aitm-edit-*"))


def test_explicit_create_then_hash_update_preserves_strict_legacy_api(managed_workspace):
    from jsonschema.exceptions import ValidationError

    _, dispatch, gateway = managed_workspace
    gateway.admit()
    created = gateway.call("create_file", {"path": "created.py", "text": "before", "operation_key": "create"})
    with pytest.raises(StaleVersion):
        gateway.call("create_file", {"path": "created.py", "text": "wrong", "operation_key": "create-existing"})
    with pytest.raises(ValidationError):
        gateway.call("write_file", {"path": "created.py", "text": "wrong", "operation_key": "missing-hash"})
    gateway.call("write_file", {"path": "created.py", "text": "after",
                                "expected_hash": created["sha256"], "operation_key": "update"})
    assert (dispatch.resources.worktree / "created.py").read_text() == "after"


@pytest.mark.parametrize(
    "name",
    ["Bash", "spawn_agent", "update_plan", "issue_host_ready", "issue_human", "session_resume"],
)
def test_native_shortcuts_and_self_issuers_are_not_worker_tools(managed_workspace, name):
    _, _, gateway = managed_workspace
    gateway.admit()
    with pytest.raises(AccessDenied):
        gateway.call(name, {})


@pytest.mark.parametrize(
    "path",
    [
        "../foreign",
        "/tmp/foreign",
        ".git/config",
        ".claude/settings.json",
        ".local/key",
        "nested/../../foreign",
    ],
)
def test_workspace_escape_and_authority_paths_refuse(tmp_path, path):
    workspace = Workspace(tmp_path)
    with pytest.raises(AccessDenied):
        workspace.write(path, "forbidden", None)
    with pytest.raises(AccessDenied):
        workspace.create(path, "forbidden")


def test_workspace_rejects_symlinks_hardlinks_and_stale_edits(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_text("protected")
    (root / "link").symlink_to(foreign)
    os.link(foreign, root / "hardlink")
    workspace = Workspace(root)
    for path in ["link", "hardlink"]:
        with pytest.raises(AccessDenied):
            workspace.write(path, "bad", None)
        with pytest.raises((AccessDenied, StaleVersion)):
            workspace.create(path, "bad")
    first = workspace.write("code.py", "a", None)
    with pytest.raises(StaleVersion):
        workspace.write("code.py", "b", "wrong")
    workspace.write("code.py", "b", first["sha256"])
    assert foreign.read_text() == "protected"


def test_expired_deadline_and_missing_command_confinement_refuse(managed_workspace, journal):
    _, _, gateway = managed_workspace
    gateway.admit()
    with pytest.raises(AccessDenied):
        gateway.call("run_command", {"command_ref": "unknown", "operation_key": "run"})
    journal.clock = lambda: gateway.deadline + 1
    with pytest.raises(AccessDenied):
        gateway.call("read_file", {"path": "code.py"})


def test_edit_preserves_existing_command_executable_mode(tmp_path):
    import stat

    command = tmp_path / "command.sh"
    command.write_text("#!/bin/sh\necho before\n")
    command.chmod(0o755)
    workspace = Workspace(tmp_path)
    before = workspace.read("command.sh")
    workspace.write("command.sh", "#!/bin/sh\necho after\n", before["sha256"])
    assert stat.S_IMODE(command.stat().st_mode) == 0o755
