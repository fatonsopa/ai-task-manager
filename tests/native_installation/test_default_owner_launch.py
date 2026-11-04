"""Shared owner entrypoint under declared provider/authority contracts, never live proof."""

import importlib
import json
import tempfile
import threading
from pathlib import Path

import pytest

from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.workflows import NativeRegistry, launch
from tests.native_installation import test_reviewer_composition as reviewer_contract
from tests.native_installation.helpers import refresh_digest

installed = reviewer_contract.installed


@pytest.fixture(autouse=True)
def physical_owned_socket_parent(monkeypatch):
    """The reused capture fixture did not serve its /tmp socket on macOS."""
    original = tempfile.TemporaryDirectory

    def physical_directory(*args, **kwargs):
        if kwargs.get("dir") == "/tmp":
            kwargs["dir"] = str(Path("/tmp").resolve())
        return original(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", physical_directory)


def stopped_owner(s, *, registry=None, options=None):
    """Run the real owner loop until its actual socket/descriptor readiness."""
    stop = threading.Event()
    observed = []
    settings = json.loads(s["config"].read_text())["workflow_control"]

    def ready():
        observed.append(json.loads(Path(settings["record"]).read_text()))
        assert Path(settings["endpoint"]).is_socket()
        stop.set()

    answer = launch(
        s["config"],
        s["primary"].installation,
        registry=registry,
        service_options=options,
        stop=stop,
        ready=ready,
    )
    assert len(observed) == 1
    assert answer == {
        "ok": True,
        "outcome": "confirmed",
        "data": {"state": "owner_closed", "shutdown": []},
    }
    assert not Path(settings["endpoint"]).exists()
    for store in s["stores"].values():
        with store.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM native_calls").fetchone()[0] == 0
    return observed


def test_DO01_default_api_resolves_sealed_normal_installation_and_reaches_ready(installed):
    stopped_owner(installed)


def test_DO02_cli_uses_same_default_launch_once(installed, monkeypatch, capsys):
    api = importlib.import_module("ai_task_manager.installation.workflows")
    native = importlib.import_module("ai_task_manager.installation.native")
    cli = importlib.import_module("ai_task_manager.presentation.cli.main")
    actual_resolve = native.resolve
    resolutions = []
    launches = []

    def resolve(*args, **kwargs):
        resolutions.append((args, kwargs))
        return actual_resolve(*args, **kwargs)

    def bounded_launch(config, installation, **kwargs):
        # Only supply the public launch lifetime controls; no owner/serve substitute.
        launches.append(kwargs)
        stop = threading.Event()
        return launch(config, installation, **kwargs, stop=stop, ready=stop.set)

    monkeypatch.setattr(native, "resolve", resolve)
    monkeypatch.setattr(api, "launch", bounded_launch)
    code = cli.main(
        [
            "--config",
            str(installed["config"]),
            "--installation",
            str(installed["primary"].installation),
            "--json",
            "workflow",
            "owner",
        ]
    )
    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["data"]["state"] == "owner_closed"
    assert launches == [{"registry": None}]
    assert len(resolutions) == 1


def test_DO03_explicit_registry_is_preserved_and_not_resolved_again(installed, monkeypatch):
    native = importlib.import_module("ai_task_manager.installation.native")
    registry = native.resolve(installed["config"], installed["primary"].installation)

    def unexpected(*args, **kwargs):
        pytest.fail("Explicit trusted registry must not invoke the normal resolver")

    monkeypatch.setattr(native, "resolve", unexpected)
    stopped_owner(installed, registry=registry)


def test_DO04_explicit_empty_registry_does_not_fall_back(installed, monkeypatch):
    native = importlib.import_module("ai_task_manager.installation.native")
    monkeypatch.setattr(native, "resolve", lambda *a, **kw: pytest.fail("Unexpected fallback"))
    with pytest.raises(Unavailable, match="registration"):
        launch(installed["config"], installed["primary"].installation, registry=NativeRegistry())


def test_DO05_service_options_reach_normal_reviewer_and_owner(installed, monkeypatch):
    native = importlib.import_module("ai_task_manager.installation.native")
    actual = native.resolve
    options = actual(installed["config"], installed["primary"].installation).service_options
    observed = []

    def resolve(*args, **kwargs):
        assert kwargs["service_options"] is options
        registry = actual(*args, **kwargs)
        assert registry.reviewer_factory is not None
        observed.append(registry)
        return registry

    monkeypatch.setattr(native, "resolve", resolve)
    stopped_owner(installed, options=options)
    assert len(observed) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "lead_role",
        "reviewer_role",
        "missing_pointer",
        "foreign_reference",
        "revoked",
        "missing_permission",
    ],
)
def test_DO06_invalid_native_authority_refuses_before_service_or_effects(
    installed, monkeypatch, fault
):
    s = installed
    b = s["primary"]
    spec = json.loads(b.installation.read_text())
    if fault.endswith("_role"):
        spec["role"] = fault.removesuffix("_role")
    elif fault == "missing_pointer":
        del spec["native_manifest"]
    elif fault == "foreign_reference":
        spec["native_ref"] = "foreign-native"
    elif fault == "revoked":
        s["stores"]["author"].revoke("operator-permission")
    elif fault == "missing_permission":
        b.data["authorization_ref"] = "not-enrolled"
    if fault not in {"revoked"}:
        write_private(b.installation, spec, replace=True)
        b.data["installation"] = installation_intent(b.installation)
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
    composition = importlib.import_module("ai_task_manager.installation.workflows.composition")
    monkeypatch.setattr(
        composition, "load_service", lambda *a, **kw: pytest.fail("Provider reached")
    )
    with pytest.raises((AccessDenied, Unavailable)):
        launch(s["config"], b.installation)
    for store in s["stores"].values():
        with store.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM native_calls").fetchone()[0] == 0
    control = json.loads(s["config"].read_text())["workflow_control"]
    assert not Path(control["endpoint"]).exists()
    assert not Path(control["record"]).exists()
