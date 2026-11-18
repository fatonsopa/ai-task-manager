"""Connected real local retirement and interrupted acknowledgement controls."""

import pytest

from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy
from ai_task_manager.supervisor.models import RuntimeRefused

from .test_cleanup_helpers import stopped


@pytest.mark.parametrize("kind", ["worktree", "database"])
def test_original_removal_intent_reconciles_lost_reply_without_resend(rr, monkeypatch, kind):
    dispatch = rr.start(rr.rig.queued(code=False))
    rr.admit(dispatch)
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    pair = dispatch.resources
    rr.resources.release_policy = DeliveryReleasePolicy(rr.repo)
    provider = rr.resources.git if kind == "worktree" else rr.resources.databases
    original = provider.remove
    calls = []

    def lose_reply(path, receipt):
        calls.append(str(path))
        original(path, receipt)
        raise ConnectionError("Removal reply deliberately lost")

    monkeypatch.setattr(provider, "remove", lose_reply)
    with pytest.raises(ConnectionError):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    monkeypatch.setattr(provider, "remove", original)
    assert rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id) == {
        "worktree": "removed",
        "database": "removed",
    }
    assert rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id) == {
        "worktree": "removed",
        "database": "removed",
    }
    assert len(calls) == 1
    assert not pair.worktree.exists() and not pair.database.exists()
    assert rr.journal.observations(pair.attempt_id, kind + "_removed")[-1][
        "reconciled_original_intent"
    ]


def test_retired_path_replacement_is_never_removed(rr):
    pair = stopped(rr)
    rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    pair.database.write_text("foreign replacement")
    with pytest.raises(RuntimeRefused, match="new object"):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.database.read_text() == "foreign replacement"


def test_current_consumer_prevents_retirement(rr):
    pair = stopped(rr)
    with rr.resources.use(pair):
        with pytest.raises(RuntimeRefused, match="active resource consumer"):
            rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.worktree.exists() and pair.database.exists()


def test_missing_removal_intent_does_not_convert_absence_to_success(rr):
    pair = stopped(rr)
    pair.database.unlink()
    with pytest.raises((RuntimeRefused, FileNotFoundError)):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.worktree.exists()


def test_delivered_merged_resource_with_foreign_current_ownership_is_protected(rr):
    from dataclasses import replace

    pair = stopped(rr)
    task = rr.rig.core.get_task(rr.rig.handle, rr.journal.attempt(pair.attempt_id)["task_id"])

    class CurrentForeignProvider:
        def get_task(self, handle, task_id):
            return replace(task, delivered_once=True, sync_state="foreign")

    fact = rr.resources.verify(pair)
    with pytest.raises(RuntimeRefused, match="foreign or unconfirmed"):
        DeliveryReleasePolicy(rr.repo).authorize(
            CurrentForeignProvider(),
            rr.rig.handle,
            rr.rig.core.get_attempt(rr.rig.handle, pair.attempt_id),
            fact,
        )
    assert pair.worktree.exists() and pair.database.exists()


@pytest.mark.parametrize(
    "guard",
    [
        "clean",
        "policy_changed",
        "task_changed",
        "native_changed",
        "dirty",
        "unconfirmed",
        "consumer",
    ],
)
def test_installed_retirement_uses_sealed_delivery_target(rr, guard):
    from dataclasses import asdict, replace
    from pathlib import Path
    from types import SimpleNamespace

    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.adapters.resources.manager import ResourceManager
    from ai_task_manager.adapters.workflow_control.identity import file_binding
    from ai_task_manager.config.files import digest, read_private, write_private
    from ai_task_manager.installation.native.code_provisioning import provisioning_digest
    from ai_task_manager.installation.native.manifest import installation_intent
    from ai_task_manager.installation.workflows.retirement import retire
    from ai_task_manager.installation.workflows.spec import BoundFile
    from ai_task_manager.storage import work

    root = rr.path
    rr.resources = ResourceManager(rr.journal, root / "resources", rr.repo)
    rr.supervisor.resources = rr.resources
    dispatched = rr.start(rr.rig.queued(code=True))
    rr.supervisor.stop(rr.rig.handle, dispatched, operation_key=rr.rig.key())
    pair = dispatched.resources
    (pair.worktree / "delivered.txt").write_text("delivered only on integration\n")
    git(pair.worktree, "add", ".")
    git(pair.worktree, "commit", "-qm", "Delivered fixture work")
    delivered = git(pair.worktree, "rev-parse", "HEAD")
    git(rr.repo, "update-ref", "refs/heads/integration", delivered)
    assert git(rr.repo, "rev-parse", "HEAD") != delivered
    task = rr.rig.core.get_task(rr.rig.handle, rr.journal.attempt(pair.attempt_id)["task_id"])
    with rr.rig.db.transaction() as conn:
        task = work.save(
            conn, replace(task, delivered_once=True, sync_state="confirmed"), task.version
        )
    config, installation, native = (
        root / n for n in ("service.json", "installation.json", "native.json")
    )
    policy, code, catalog, commands = (
        root / n for n in ("policy.json", "code.json", "catalog.json", "commands.json")
    )
    write_private(config, {"core_database": str(rr.rig.db.path)})
    write_private(
        policy,
        {
            "reference": "fixture-code",
            "scope": asdict(rr.rig.scope),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "target_ref": "refs/heads/integration",
            "require_human_acceptance": False,
        },
    )
    if guard == "task_changed":
        changed = read_private(policy)
        changed["task_id"] = "another-task"
        write_private(policy, changed, replace=True)
    write_private(catalog, {"fixture": "sealed original catalog"})
    write_private(commands, {"fixture": "sealed original commands"})
    write_private(
        code,
        {
            "policy": {"path": str(policy), "sha256": digest_file(policy)},
            "catalog": {"path": str(catalog), "sha256": digest_file(catalog)},
        },
    )
    pointers = (
        {"path": str(code), "sha256": digest_file(code)},
        {"path": str(commands), "sha256": digest_file(commands)},
    )
    spec = {
        "state_root": str(root),
        "repository": str(rr.repo),
        "code_ref": "fixture-code",
        "code_digest": provisioning_digest(*pointers),
        "native_manifest": str(native),
        "task_id": task.id,
        "prose_digest": task.prose_digest,
        "scope": asdict(rr.rig.scope),
        "native_ref": "fixture-native",
        "native_digest": "0" * 64,
    }
    write_private(installation, spec)
    home = root / "home"
    home.mkdir(mode=0o700)
    (home / ".claude").mkdir(mode=0o700)
    executable = Path("/usr/bin/true").resolve()
    write_private(
        native,
        {
            "version": 1,
            "manifest_path": str(native),
            "installation": installation_intent(installation),
            "config": file_binding(config),
            "native_ref": "fixture-native",
            "authorization_ref": "fixture-native",
            "host": "claude",
            "model": "claude-sonnet-5-5",
            "effort": "high",
            "account_hash": "a" * 64,
            "executable": str(executable),
            "executable_sha256": digest_file(executable),
            "home": str(home),
            "profile": str(home / ".claude"),
            "code_manifest": pointers[0],
            "commands_manifest": pointers[1],
        },
    )
    from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins

    pins = NativeSourcePins({str(native): digest_file(native)})
    spec["native_digest"] = digest(
        {
            "reference": "fixture-native",
            "host": "claude",
            "model": "claude-sonnet-5-5",
            "effort": "high",
            "account": "a" * 64,
            "source": pins.fingerprint,
        }
    )
    write_private(installation, spec, replace=True)
    policy_changed = [False]

    def current(service):
        if guard == "policy_changed" and not policy_changed[0]:
            changed = read_private(policy)
            changed["target_ref"] = "HEAD"
            write_private(policy, changed, replace=True)
            policy_changed[0] = True

    if guard == "native_changed":
        changed = read_private(native)
        changed["account_hash"] = "b" * 64
        write_private(native, changed, replace=True)
    if guard == "dirty":
        (pair.worktree / "dirty.txt").write_text("Preserve changed work\n")
    if guard == "unconfirmed":
        with rr.rig.db.transaction() as conn:
            work.save(conn, replace(task, sync_state="uncertain"), task.version)
    recovery = SimpleNamespace(
        spec=SimpleNamespace(data=spec, seal=BoundFile(installation)),
        config_file=config,
        config={"core_database": str(rr.rig.db.path)},
        validate_stores=lambda: None,
        check_service=current,
    )
    service = SimpleNamespace(core=rr.rig.core, handle=rr.rig.handle)
    row = {"id": "workflow:fixture", "task_id": task.id, "attempt_id": pair.attempt_id}
    if guard != "clean":
        from ai_task_manager.domains.work.errors import WorkError

        if guard == "consumer":
            with rr.resources.use(pair):
                with pytest.raises(RuntimeRefused, match="active resource consumer"):
                    retire(recovery, service, row)
        else:
            with pytest.raises((RuntimeRefused, WorkError)):
                retire(recovery, service, row)
        assert pair.worktree.exists() and pair.database.exists()
        assert git(rr.repo, "rev-parse", "refs/heads/integration") == delivered
        return
    result = retire(recovery, service, row)
    assert result["data"]["resources"] == {"worktree": "removed", "database": "removed"}
    assert not pair.worktree.exists() and not pair.database.exists()
    assert git(rr.repo, "rev-parse", "refs/heads/integration") == delivered
    assert git(rr.repo, "rev-parse", "HEAD") != delivered


def digest_file(path):
    from hashlib import sha256

    return sha256(path.read_bytes()).hexdigest()
