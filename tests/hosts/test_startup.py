import pytest

from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts, StartupObserver
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.conftest import FixturePolicy

ACCOUNT = {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "fixture"}


def codex_start(native, binding, **overrides):
    process, transcript, emit = native()
    policy = FixturePolicy(
        binding.configuration_digest, process.command.cwd, {"CODEX_HOME": "fixture-home"}
    )
    observer = StartupObserver(transcript, policy, expected_account=digest(ACCOUNT))
    frames = [
        (1, "initialize", {"codexHome": "fixture-home", "userAgent": "fixture"}),
        (2, "account/read", {"account": ACCOUNT}),
        (
            3,
            "thread/start",
            {
                "thread": {"id": "native-thread"},
                "model": "fixture-model",
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": "readOnly"},
                "cwd": str(policy.cwd),
            },
        ),
    ]
    for identity, method, result in frames:
        observer.expect(identity, method)
        emit({"id": identity, "result": overrides.get(method, result)})
    return process, transcript, emit, observer


def test_actual_owned_protocol_responses_create_scoped_startup(native, binding, journal):
    process, _, _, observer = codex_start(native, binding)
    verifier = HostStartupReceipts(journal, {binding.attempt_id: process})
    receipt = verifier.verify_host_ready(observer.ready_ref)
    assert receipt.scope == binding.scope and receipt.session_id == "native-thread"
    assert receipt.process_id == process.identity.pid
    assert receipt.configuration_digest == binding.configuration_digest
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal).verify_host_ready(observer.ready_ref)


@pytest.mark.parametrize(
    "account",
    [
        None,
        {"type": "apiKey"},
        {"type": "amazonBedrock"},
        {**ACCOUNT, "email": "other@example.invalid"},
    ],
)
def test_wrong_or_missing_subscription_refuses(native, binding, account):
    with pytest.raises(AccessDenied):
        codex_start(native, binding, **{"account/read": {"account": account}})


def test_model_text_replayed_file_and_guessed_uuid_do_not_create_startup(native, binding, journal):
    process, transcript, emit = native()
    observer = StartupObserver(
        transcript,
        FixturePolicy(
            binding.configuration_digest, process.command.cwd, {"CODEX_HOME": "fixture-home"}
        ),
        expected_account=digest(ACCOUNT),
    )
    emit({"type": "assistant", "text": '{"ready":true,"session_id":"invented"}'})
    assert observer.ready_ref is None
    trace = journal._append("trace", {"ready": True, "session_id": "invented"})
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(trace)


@pytest.mark.parametrize(
    "field,value",
    [
        ("modelProvider", "other"),
        ("approvalPolicy", "on-request"),
        ("sandbox", {"type": "dangerFullAccess"}),
        ("cwd", "/foreign"),
    ],
)
def test_unrestricted_native_thread_refuses(native, binding, field, value):
    thread = {
        "thread": {"id": "native-thread"},
        "model": "fixture-model",
        "modelProvider": "openai",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "readOnly"},
        "cwd": "/placeholder",
    }
    # Read the fixture cwd from its owned transport.
    process, transcript, emit = native()
    policy = FixturePolicy(
        binding.configuration_digest, process.command.cwd, {"CODEX_HOME": "fixture-home"}
    )
    observer = StartupObserver(transcript, policy, expected_account=digest(ACCOUNT))
    thread["cwd"] = str(policy.cwd)
    thread[field] = value
    for identity, method, result in [
        (1, "initialize", {"codexHome": "fixture-home"}),
        (2, "account/read", {"account": ACCOUNT}),
    ]:
        observer.expect(identity, method)
        emit({"id": identity, "result": result})
    observer.expect(3, "thread/start")
    with pytest.raises(AccessDenied):
        emit({"id": 3, "result": thread})


def test_account_change_after_startup_refuses(native, binding):
    _, _, emit, _ = codex_start(native, binding)
    with pytest.raises(AccessDenied):
        emit({"method": "account/updated", "params": {"authMode": "apikey"}})


def test_source_expiry_limits_startup_receipt(native, binding, journal):
    _, _, _, observer = codex_start(native, binding)
    data = journal._read(observer.ready_ref)
    assert data["expires_at"] <= binding.deadline


def test_stopped_process_cannot_replay_startup(native, binding, journal):
    process, _, _, observer = codex_start(native, binding)
    process.stop()
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(
            observer.ready_ref
        )


def test_late_bound_owned_process_registry_keeps_shared_identity(native, binding, journal):
    process, _, _, observer = codex_start(native, binding)
    shared = {}
    verifier = HostStartupReceipts(journal, shared)
    shared[binding.attempt_id] = process
    assert verifier.verify_host_ready(observer.ready_ref).process_id == process.identity.pid
