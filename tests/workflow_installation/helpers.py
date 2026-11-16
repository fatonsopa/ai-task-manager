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
    from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
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
    claude = mode.startswith(("code", "cold"))
    rig.capacity = CapacityPolicy("claude" if claude else "codex", 2, 2)
    rig.core = rig.new_core()
    code = mode.startswith("code")
    task = rig.queued(code=code)
    r.task = task
    if code:
        from tests.workflow_installation.code_controls import initialize

        initialize(r.repo)
        import subprocess

        subprocess.run(["git", "-C", str(r.repo), "branch", "integration"], check=True)
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
    settings = (
        None if claude else CodexSettings(CodexHomeBoundary(profile), "synthetic-no-model", "high")
    )
    if claude:
        profile = home / ".claude_fixture"
        profile.mkdir(mode=0o700)
    executable = root / "synthetic-cli"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + Path(__file__).parents[1].joinpath("hosts/fixture_cli.py").read_text()
    )
    if claude:
        source = Path(__file__).parents[1].joinpath("hosts/fixture_claude_bootstrap.py").read_text()
        assignment = (
            task.title
            + "\n\n"
            + task.body
            + "\n\nRequired work units: "
            + ", ".join(task.required_work)
        )
        source = source.replace(
            '    elif value.get("type") == "user":',
            '    elif value.get("type") == "user":\n'
            + '        if value["message"]["content"] == '
            + repr(assignment)
            + ":\n"
            + "            from pathlib import Path\n"
            + '            Path("src").mkdir(exist_ok=True)\n'
            + '            Path("src/a.txt").write_text("4")',
        )
        if not code:
            source = source.replace(
                'Path("src").mkdir(exist_ok=True)', 'Path(".").mkdir(exist_ok=True)'
            )
            source = source.replace(
                'Path("src/a.txt").write_text("4")', 'Path("owned.py").write_text("answer = 42\\n")'
            )
            if mode in {"cold-active", "cold-lineage"}:
                source = source.replace(
                    'Path("owned.py").write_text("answer = 42\\n")',
                    "import time;Path("
                    + repr(str(root / "native-entered"))
                    + ").write_text('ready');time.sleep(20)",
                )
        executable.write_text("#!" + sys.executable + "\n" + source)
    executable.chmod(0o700)
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

    def prepare(context):
        if claude:
            from tests.workflow_installation.code_controls import ClaudePermits

            permits = ClaudePermits(
                root / ("accounting" if context.task.id == task.id else "reviewer-accounting")
            )
        else:
            permits = AuthoredPermits(root / "accounting", deny=mode == "deny-native")

        def policy(host, pair):
            if claude:
                from ai_task_manager.adapters.hosts.bridge.admission import MANAGED_TOOLS

                env = clean_environment(home, claude_profile=profile)
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
                        {},
                        model="claude-opus-5-5",
                        effort="high",
                    ),
                    (),
                    allowed_tools=tuple(sorted(MANAGED_TOOLS)),
                    model="claude-opus-5-5",
                    effort="high",
                )
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

        if code:
            from tests.workflow_installation.code_controls import worker_authority

            return worker_authority(context, policy, permits, AuthoredAccounting(permits))
        return NativeAuthorities(
            policy, permits, AuthoredAccounting(permits), "synthetic-accounting"
        )

    native = NativeRegistration(
        "authored-native",
        "claude" if claude else "codex",
        NativeProfile("claude-opus-5-5" if claude else settings.model, "high"),
        claude_account_hash(
            {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
        )
        if claude
        else digest(ACCOUNT),
        NativeSourcePins({str(executable): executable_hash}),
        prepare,
    )
    observations = Observations()
    observations.grants["authored-grant"] = HostGrant(
        rig.scope,
        rig.scope.logical_session,
        ("start", "non_code_complete", "merge") if code else ("start", "non_code_complete"),
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
    code_registry, reviewer_factory = (), None
    if code:
        from tests.workflow_installation.code_controls import registration, review_factory

        code_registry = (registration(root, r),)
        reviewer_factory = review_factory(r, root)
    registry = NativeRegistry(
        (native,),
        service_options=options,
        code_registrations=code_registry,
        reviewer_factory=reviewer_factory,
    )
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
        "deadline": time.time() + (180 if code else 40),
        "principal_ref": "authored-principal",
        "plan_ref": "installed-non-code",
        "files": ["owned.py"],
        "artifacts": ["owned.py"],
    }
    if code:
        spec.update(
            code_ref=code_registry[0].reference,
            code_digest=code_registry[0].fingerprint,
            plan_ref="installed-code",
            files=["src/a.txt"],
            artifacts=["src/a.txt"],
        )
    if mode == "code-missing-registration":
        spec.pop("code_ref")
        spec.pop("code_digest")
    if mode == "code-wrong-policy":
        spec["code_digest"] = "0" * 64
    if mode == "wrong-scope":
        spec["scope"]["principal"] = "foreign"
    if mode == "wrong-prose":
        spec["prose_digest"] = "0" * 64
    if mode == "wrong-native":
        spec["native_digest"] = "0" * 64
    if mode == "wrong-source":
        spec["source_head"] = "0" * 40
    installation = root / "installation.json"
    reviewed = reviewer_task = None
    if mode == "cold-lineage":
        reviewer_task = rig.queued(code=False)
        reviewer_file = root / "reviewer.json"
        reviewer_spec = {
            **spec,
            "task_id": reviewer_task.id,
            "prose_digest": reviewer_task.prose_digest,
            "state_root": str(root / "reviewer-state"),
            "plan_ref": "reviewer",
            "role": "reviewer",
            "installation_path": str(reviewer_file),
            "review_target": {
                "task_id": task.id,
                "prose_digest": task.prose_digest,
                "source_head": base,
            },
        }
        write_private(reviewer_file, reviewer_spec)
        spec["reviewer_installation"] = file_binding(reviewer_file)
        from ai_task_manager.installation.workflows import InstalledWorkflow

        reviewed = InstalledWorkflow(config_file, reviewer_file, registry)
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
    if mode not in {
        "valid",
        "cold",
        "cold-active",
        "cold-lineage",
        "code",
        "code-restart",
        "recovery",
    }:
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
    if mode == "code-restart":
        from ai_task_manager.installation.workflows.code_delivery import InstalledCodeDelivery

        original_step = InstalledCodeDelivery._step
        cut = {"done": False}

        def lose_reply(self, row, owner, name, payload, action):
            observed = original_step(self, row, owner, name, payload, action)
            if name == "code-plan" and not cut["done"]:
                cut["done"] = True
                self.components.clear()
                self.capabilities.clear()
                write_private(
                    root / "lost-reply.json",
                    {"after": "code-plan", "workflow": row["id"], "plan": payload},
                )
                raise RuntimeError("Authored lost reply after durable merge-plan selection")
            return observed

        InstalledCodeDelivery._step = lose_reply
    if mode == "cold-lineage":
        from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner
        from ai_task_manager.adapters.workflow_control.recovery import physical

        original_serve = WorkflowOwner._serve

        def with_reviewer(self, stop=None, ready=None):
            def start_reviewer():
                parent = self.service.workflows.driver.runtime.workers.hosts.journal
                peer_root = Path(reviewed.spec.data["state_root"])
                parent._append(
                    "trace",
                    {
                        "effect": "reviewer_state_created",
                        "installation": spec["reviewer_installation"],
                        "task_id": reviewer_task.id,
                        "scope": spec["scope"],
                        "files": {
                            "runtime": physical(reviewed.journal.path),
                            "host": physical(reviewed.host_journal.path),
                            "runtime_key": file_binding(peer_root / "runtime.key"),
                            "host_key": file_binding(reviewed.host_journal.key_path),
                        },
                    },
                )
                current = reviewed.service.core.get_task(reviewed.service.handle, reviewer_task.id)
                decision = reviewed.service.decisions.decision(
                    reviewed.service.scope,
                    current,
                    "In Progress",
                    "start",
                    grant_ref="authored-grant",
                )
                attempt = reviewed.workers.start(
                    reviewed.service.handle,
                    current.id,
                    "claude",
                    expected_version=current.version,
                    decision_revision=current.decision_revision,
                    decision_ref=decision,
                    operation_key="reviewer-start",
                )
                write_private(
                    root / "reviewer-ready.json", {"task_id": current.id, "attempt_id": attempt.id}
                )

            def admitted():
                workers = self.service.workflows.driver.runtime.workers
                starting = workers.start

                def author_then_reviewer(*args, **kwargs):
                    attempt = starting(*args, **kwargs)
                    start_reviewer()
                    return attempt

                workers.start = author_then_reviewer
                if ready:
                    ready()

            try:
                return original_serve(self, stop, admitted)
            except BaseException:
                import traceback

                traceback.print_exc()
                raise

        WorkflowOwner._serve = with_reviewer
    if mode == "recovery":
        # A recorded, unexecuted intent is enough for refusal boundaries. Do not
        # launch an unrelated synthetic native worker in every negative case.
        from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner

        original_serve = WorkflowOwner._serve

        def with_intent(self, stop=None, ready=None):
            def admitted():
                workflow = self.service.workflows.driver.begin(
                    task.id, spec["plan_ref"], "recovery-original"
                )
                write_private(root / "recovery-intent.json", {"workflow_ref": workflow})
                if ready:
                    ready()

            return original_serve(self, stop, admitted)

        WorkflowOwner._serve = with_intent
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
