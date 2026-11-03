"""Authored native/provider controls through actual installed managed composition."""

import hashlib
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4


def owner_main(root, socket_root, mode="valid"):
    from ai_task_manager.adapters.decisions.authority import HostGrant
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
    from ai_task_manager.adapters.hosts.bridge.codex_home import CodexHomeBoundary
    from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings
    from ai_task_manager.adapters.hosts.bridge.policy import (
        NativePolicy,
        NativeProfile,
        clean_environment,
        policy_digest,
    )
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.workflow_control.identity import file_binding
    from ai_task_manager.config.claim_store import enroll_claim_store
    from ai_task_manager.config.files import digest, write_private
    from ai_task_manager.config.sessions import SessionFile
    from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy
    from ai_task_manager.installation.workflows import (
        NativeAuthorities,
        NativeRegistration,
        NativeRegistry,
    )
    from ai_task_manager.presentation.cli.main import main
    from tests.application.helpers import Observations
    from tests.hosts.test_composition import ACCOUNT, AuthoredAccounting, AuthoredPermits
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.runtime.helpers import ProviderFixture, RuntimeRig

    root = Path(root)
    socket_root = Path(socket_root)
    os.umask(0o077)
    r = RuntimeRig(root)
    rig = r.rig
    rig.now = time.time()
    rig.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
    rig.capacity = CapacityPolicy("codex", 2, 2)
    rig.core = rig.new_core()
    task = rig.queued(code=False)
    authority = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
    enroll_claim_store(rig.db, authority)
    SessionFile(root / "session.json").save(rig.handle, rig.scope)
    config = {
        "version": 1,
        "core_database": str(rig.db.path),
        "authority_database": str(authority.path),
        "authority_key": str(authority.key_path),
        "session_file": str(root / "session.json"),
        "approval_policy": asdict(rig.policy),
        "grant_ref": "authored-grant",
        "workflow_control": {
            "record": str(socket_root / "owner.json"),
            "endpoint": str(socket_root / "owner.sock"),
            "instance": str(uuid4()),
        },
    }
    config_file = root / "service.json"
    write_private(config_file, config)
    home = root / "home"
    home.mkdir(mode=0o700)
    profile = root / "private-codex"
    profile.mkdir(mode=0o700)
    for name in ["auth.json", "config.toml"]:
        (profile / name).write_text("{}" if name == "auth.json" else "")
        (profile / name).chmod(0o600)
    settings = CodexSettings(CodexHomeBoundary(profile), "synthetic-no-model", "high")
    executable = root / "synthetic-cli"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + Path(__file__).parents[1].joinpath("hosts/fixture_cli.py").read_text()
    )
    executable.chmod(0o700)
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

    def prepare(context):
        permits = AuthoredPermits(root / "accounting", deny=mode == "deny-native")

        def policy(host, pair):
            env = clean_environment(home, codex_home=profile)
            return NativePolicy(
                host,
                executable,
                executable_hash,
                env,
                pair.worktree,
                policy_digest(
                    host,
                    executable_hash,
                    env,
                    pair.worktree,
                    {"codex_settings": settings.fingerprint},
                    model=settings.model,
                    effort=settings.effort,
                ),
                (),
                model=settings.model,
                effort=settings.effort,
                codex_settings=settings,
            )

        return NativeAuthorities(
            policy, permits, AuthoredAccounting(permits), "synthetic-accounting"
        )

    native = NativeRegistration(
        "authored-native",
        "codex",
        NativeProfile(settings.model, "high"),
        digest(ACCOUNT),
        NativeSourcePins({str(executable): executable_hash}),
        prepare,
    )
    observations = Observations()
    observations.grants["authored-grant"] = HostGrant(
        rig.scope,
        rig.scope.logical_session,
        ("start", "non_code_complete"),
        "authored-local-enrollment",
        time.time() + 120,
    )
    router = EvidenceRouter(
        [
            EvidenceRoute("fixture:", frozenset({"provider_status"}), rig.proofs),
            EvidenceRoute("decision-proof:", frozenset({"decision", "human_decision"}), authority),
        ]
    )

    def provider(core, handle, decisions, writer):
        rig.core = core
        return ProviderFixture(rig)

    options = {
        "observations": observations,
        "evidence_factory": lambda journal: router,
        "provider_factory": provider,
        "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
        "scheduling_sources": rig.scheduling_sources,
    }
    if mode == "missing-provider":
        options["provider_factory"] = lambda *args: None
    registry = NativeRegistry((native,), service_options=options)
    import subprocess

    base = subprocess.check_output(
        ["git", "-C", str(r.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    spec = {
        "version": 1,
        "config": file_binding(config_file),
        "scope": asdict(rig.scope),
        "task_id": task.id,
        "prose_digest": task.prose_digest,
        "native_ref": native.reference,
        "native_digest": native.fingerprint,
        "repository": str(r.repo),
        "source_head": base,
        "state_root": str(root / "installed-state"),
        "deadline": time.time() + 40,
        "principal_ref": "authored-principal",
        "plan_ref": "installed-non-code",
        "files": ["owned.py"],
        "artifacts": ["owned.py"],
    }
    if mode == "wrong-scope":
        spec["scope"]["principal"] = "foreign"
    if mode == "wrong-prose":
        spec["prose_digest"] = "0" * 64
    if mode == "wrong-native":
        spec["native_digest"] = "0" * 64
    if mode == "wrong-source":
        spec["source_head"] = "0" * 40
    installation = root / "installation.json"
    spec["installation_path"] = str(installation)
    write_private(installation, spec)
    write_private(
        root / "prepared.json",
        {
            "config": str(config_file),
            "installation": str(installation),
            "task_id": task.id,
            "plan_ref": spec["plan_ref"],
            "module_source": __import__("ai_task_manager").__path__[0],
        },
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stop.set())
    # The installed CLI intentionally accepts only a trusted in-process registry.
    # Its production default never imports this test helper or these controls.
    if mode != "valid":
        try:
            from ai_task_manager.installation.workflows import InstalledWorkflow

            InstalledWorkflow(
                config_file,
                installation,
                registry,
                reviews=object() if mode == "self-review" else None,
            )
        except Exception as error:
            write_private(
                root / "refused.json", {"type": type(error).__name__, "detail": str(error)}
            )
            return
        raise AssertionError("negative installer control did not refuse")
    # Real launcher signal lifecycle uses the same concrete launch function.
    import ai_task_manager.installation.workflows as api

    original = api.launch

    def observed(*args, **kwargs):
        kwargs["stop"] = stop
        kwargs["ready"] = lambda: write_private(root / "ready.json", {"ready": True})
        return original(*args, **kwargs)

    api.launch = observed
    result = main(
        [
            "--config",
            str(config_file),
            "--json",
            "workflow",
            "owner",
            "--installation",
            str(installation),
        ],
        workflow_registry=registry,
    )
    assert result == 0, result
