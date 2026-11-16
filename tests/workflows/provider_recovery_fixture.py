"""Authored installed provider control; native capabilities refuse every launch."""

import hashlib
import threading
from pathlib import Path


def registry():
    from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
    from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile
    from ai_task_manager.installation.workflows import (
        NativeAuthorities,
        NativeRegistration,
        NativeRegistry,
    )
    from tests.hosts.test_continuity import AuthoredDependencyCollection

    class NoNative:
        def refused(self, *args, **kwargs):
            raise AssertionError("This provider-only fixture cannot launch native work")

        check_admission = authorize_initialization = authorize = record = refused

    source = Path(__file__).resolve()
    refused = NoNative()
    registration = NativeRegistration(
        "authored-no-native",
        "claude",
        NativeProfile("authored-no-model", "high"),
        "a" * 64,
        NativeSourcePins({str(source): hashlib.sha256(source.read_bytes()).hexdigest()}),
        lambda _: NativeAuthorities(refused.refused, refused, refused, "authored-no-accounting"),
    )
    return NativeRegistry(
        (registration,), service_options={"dependencies_factory": AuthoredDependencyCollection}
    )


def owner_main(config, installation, ready):
    import signal

    from ai_task_manager.config.files import write_private
    from ai_task_manager.installation.workflows import InstalledWorkflow

    installed = InstalledWorkflow(config, installation, registry())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def admitted():
        driver = installed.service.workflows.driver
        reference = driver.begin(
            installed.spec.data["task_id"], installed.spec.data["plan_ref"], "t03-original"
        )
        write_private(ready, {"workflow_ref": reference})

    installed.serve(stop, admitted)
