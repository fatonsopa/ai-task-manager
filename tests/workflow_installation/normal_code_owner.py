"""Default cold code owner with declared provider/native protocols and production provisioning."""

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4


def owner_main(root, sockets, mode="ordinary"):
    from ai_task_manager.adapters.decisions.authority import HostGrant
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.git.profile import Executable, GitProfile
    from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.workflow_control.identity import file_binding
    from ai_task_manager.config.claim_store import enroll_claim_store
    from ai_task_manager.config.files import write_private
    from ai_task_manager.config.sessions import SessionFile
    from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy, TaskProse
    from ai_task_manager.installation.native import NativeManifest, normal
    from ai_task_manager.installation.native.code_provisioning import provisioning_digest
    from ai_task_manager.installation.native.manifest import installation_intent
    from ai_task_manager.installation.native.store import NativeStore
    from ai_task_manager.presentation.cli.main import main
    from tests.application.helpers import Observations
    from tests.code_provisioning.helpers import IMAGE, pointer
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.native_installation.helpers import bundle, refresh_digest
    from tests.native_installation.test_normal import AuthoredRestrictions
    from tests.runtime.helpers import ProviderFixture, RuntimeRig
    from tests.scheduling.helpers import known_declaration
    from tests.workflow_installation.code_controls import initialize

    root, sockets = Path(root), Path(sockets)
    os.umask(0o077)
    runtime = RuntimeRig(root)
    r = runtime.rig
    r.now = time.time()
    r.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
    r.capacity = CapacityPolicy("claude", 2, 2)
    r.core = r.new_core()
    task = r.queued(code=True)
    initialize(runtime.repo)
    subprocess.run(["git", "-C", str(runtime.repo), "branch", "integration"], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(runtime.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    authority = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
    enroll_claim_store(r.db, authority)
    SessionFile(root / "session.json").save(r.handle, r.scope)
    config = root / "service.json"
    write_private(
        config,
        {
            "version": 1,
            "core_database": str(r.db.path),
            "authority_database": str(authority.path),
            "authority_key": str(authority.key_path),
            "session_file": str(root / "session.json"),
            "approval_policy": asdict(r.policy),
            "grant_ref": "declared-installation",
            "workflow_control": {
                "record": str(sockets / "owner.json"),
                "endpoint": str(sockets / "owner.sock"),
                "instance": str(uuid4()),
            },
        },
    )
    deadline = time.time() + 300
    observations = Observations()
    observations.grants["declared-installation"] = HostGrant(
        r.scope,
        r.scope.logical_session,
        ("start", "merge", "non_code_complete"),
        "declared-owned-fixture",
        deadline,
    )

    def provider(core, handle, decisions, writer):
        r.core = core
        return ProviderFixture(r)

    options = {
        "observations": observations,
        "evidence_factory": lambda journal: EvidenceRouter(
            [
                EvidenceRoute(
                    "fixture:", frozenset({"provider_status", "external_observation"}), r.proofs
                ),
                EvidenceRoute(
                    "decision-proof:", frozenset({"decision", "human_decision"}), journal
                ),
            ]
        ),
        "provider_factory": provider,
        "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
        "scheduling_sources": r.scheduling_sources,
    }
    reviewer_tasks = {}
    for stage in ("code_worker", "code_candidate"):
        prose = TaskProse(
            "Independently review the exact " + stage + " input",
            "Review only the bound author task and stage. Inspect its current product-provided "
            "artifact facts against every criterion and return an attributed judgment. "
            "This is an authored local protocol control, not a genuine model evaluation.",
            False,
        )
        value = r.core.intake.submit(r.handle, prose, r.approval(prose), r.key())
        value = r.confirm(value)
        value = r.transition(value, "Todo", "enqueue")
        r.known_dependencies[value.id] = ((), "normal")
        known_declaration(r, value)
        reviewer_tasks[stage] = value
    assignment = (
        task.title
        + "\n\n"
        + task.body
        + "\n\nRequired work units: "
        + ", ".join(task.required_work)
    )
    original_script = (
        Path(__file__).parents[1].joinpath("hosts/fixture_claude_bootstrap.py").read_text()
    )
    extra = (
        '    elif value.get("type") == "user":\n'
        '        supplied = value["message"]["content"]\n'
        "        if supplied == " + repr(assignment) + ":\n"
        "            from pathlib import Path\n"
        '            Path("src").mkdir(exist_ok=True)\n'
        '            Path("src/a.txt").write_text("4")\n'
        '        answer = "READY"\n'
        '        if "Independently review" in supplied:\n'
        '            packet = json.loads(supplied.split("\\n")[-1])\n'
        '            answer = json.dumps({"context_digest": packet["context_digest"],\n'
        '                "criteria": [{"id": c[0], "judgment": "accept",\n'
        '                    "reason": "Authored native protocol criterion"}\n'
        '                    for c in packet["context"]["criteria"]],\n'
        '                "reason": "Synthetic semantic frame; no genuine model judgment"})'
    )
    script = original_script.replace('    elif value.get("type") == "user":', extra)
    script = script.replace('"result": "READY",', '"result": answer,')
    script = script.replace('"text": "READY"', '"text": answer')
    stores = {}

    def install(name, selected, role, more):
        b = bundle(root / name, "claude", "claude-opus-5-5", task_id=selected.id)
        executable = b.root / "declared-native"
        executable.write_text("#!" + sys.executable + "\n" + script)
        executable.chmod(0o700)
        store = NativeStore.create(b.root / "permission.sqlite")
        spec = {
            "version": 1,
            "config": file_binding(config),
            "scope": asdict(r.scope),
            "task_id": selected.id,
            "prose_digest": selected.prose_digest,
            "native_ref": "normal-native",
            "native_digest": "0" * 64,
            "native_manifest": str(b.manifest),
            "repository": str(runtime.repo),
            "source_head": base,
            "state_root": str(b.root / "state"),
            "deadline": deadline,
            "principal_ref": "declared-principal",
            "plan_ref": name,
            "files": ["src/a.txt"],
            "artifacts": ["src/a.txt"],
            "installation_path": str(b.installation),
            "role": role,
            **more,
        }
        write_private(b.installation, spec, replace=True)
        b.data.update(
            authority_store=store.pointer,
            config=file_binding(config),
            installation=installation_intent(b.installation),
            executable=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            account_hash=claude_account_hash(
                {
                    "email": "fixture@example.invalid",
                    "orgId": "authored-org",
                    "userId": "authored-user",
                }
            ),
        )
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        stores[name] = store
        return b

    slots = {
        stage: install(
            stage,
            selected,
            "reviewer",
            {
                "review_target": {
                    "task_id": task.id,
                    "prose_digest": task.prose_digest,
                    "stage": stage,
                }
            },
        )
        for stage, selected in reviewer_tasks.items()
    }
    policy = root / "code-policy.json"
    write_private(
        policy,
        {
            "reference": "installed-code",
            "scope": asdict(r.scope),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "target_ref": "refs/heads/integration",
            "require_human_acceptance": False,
        },
    )
    catalog = root / "checks.json"
    write_private(
        catalog,
        {
            "format": 2,
            "groups": [
                {
                    "labels": ["Checks"],
                    "category": "verification",
                    "runner": "installed-python-script",
                    "resources": ["application-database"],
                    "required": True,
                    "prerequisites": [],
                }
            ],
        },
    )
    docker = Path("/usr/local/bin/docker").resolve()
    socket = Path.home().joinpath(".docker/run/docker.sock").resolve()
    si = socket.stat()
    commands = root / "commands.json"
    write_private(
        commands,
        {
            "version": 1,
            "manifest_path": str(commands),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "docker_executable": str(docker),
            "docker_sha256": hashlib.sha256(docker.read_bytes()).hexdigest(),
            "docker_endpoint": "unix://" + str(socket),
            "endpoint_identity": {"path": str(socket), "device": si.st_dev, "inode": si.st_ino},
            "image_id": IMAGE,
            "commands": [
                {
                    "reference": "verify",
                    "recipe": "python-script",
                    "script": "verify.py",
                    "script_sha256": hashlib.sha256(
                        (runtime.repo / "verify.py").read_bytes()
                    ).hexdigest(),
                    "files": ["verify.py", "src/a.txt"],
                    "seconds": 8,
                }
            ],
        },
    )
    git = Executable.capture("/usr/bin/git", GitProfile.environment(root / "git-home"))
    code = root / "code.json"
    write_private(
        code,
        {
            "version": 1,
            "manifest_path": str(code),
            "policy": pointer(policy),
            "git": {
                "root": str(root),
                "executable": git.path,
                "sha256": git.sha256,
                "exec_manifest": git.exec_manifest,
            },
            "catalog": pointer(catalog),
            "units": [{"name": "verify", "group": "checks", "command_ref": "verify"}],
            "required_hooks": [],
            "attempts": 1,
        },
    )
    author = install(
        "author",
        task,
        "worker",
        {
            "reviewer_installation": {
                stage: file_binding(slot.installation) for stage, slot in slots.items()
            },
            "code_ref": "installed-code",
            "code_digest": provisioning_digest(pointer(code), pointer(commands)),
        },
    )
    author.data.update(code_manifest=pointer(code), commands_manifest=pointer(commands))
    author.data["installation"] = installation_intent(author.installation)
    write_private(author.manifest, author.data, replace=True)
    refresh_digest(author.installation, author.manifest, author.data)
    restrictions = AuthoredRestrictions()
    original_source = normal.NormalAuthoritySource
    normal.NormalAuthoritySource = lambda *a, **kw: original_source(
        *a, **{**kw, "conformance": restrictions}
    )
    for b in [*slots.values(), author]:
        manifest = NativeManifest(b.manifest, config, b.installation)
        normal.enroll(
            stores[b.root.name],
            manifest,
            {"host": 150, "relay": 150, "auth": 5, "neutral": 8, "assignment": 8},
            identity_refreshes=(),
            conformance_ref="authored-native-contract-not-live",
            role="worker" if b is author else "reviewer",
        )
    import ai_task_manager.installation.native as native_module

    original_resolve = native_module.resolve

    def observed_default(*args, **kwargs):
        registry = original_resolve(*args, **{**kwargs, "service_options": options})
        write_private(
            root / "default-resolved.json",
            {
                "code_references": list(registry.code),
                "prepare_owner_module": type(
                    registry.code["installed-code"].prepare.__self__
                ).__module__,
                "reviewer_factory_module": type(registry.reviewer_factory).__module__,
                "code_registration_injected": False,
                "reviewer_factory_injected": False,
                "module_origin": __import__("ai_task_manager").__path__[0],
            },
        )
        return registry

    native_module.resolve = observed_default
    write_private(
        root / "prepared.json",
        {
            "config": str(config),
            "installation": str(author.installation),
            "task_id": task.id,
            "plan_ref": "author",
            "state_root": str(author.root / "state"),
            "repository": str(runtime.repo),
            "module_source": __import__("ai_task_manager").__path__[0],
        },
    )
    if mode == "restart":
        from ai_task_manager.installation.workflows.code_delivery import InstalledCodeDelivery

        original_step = InstalledCodeDelivery._step
        cut = []

        def lose_reply(self, row, owner, name, payload, action):
            result = original_step(self, row, owner, name, payload, action)
            if name == "code-plan" and not cut:
                cut.append(True)
                self.components.clear()
                self.capabilities.clear()
                write_private(root / "lost-reply.json", {"workflow": row["id"], "after": name})
                raise RuntimeError("Authored reply loss after durable plan; same retained owner")
            return result

        InstalledCodeDelivery._step = lose_reply
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stop.set())
    import ai_task_manager.installation.workflows as api

    original_launch = api.launch

    def launch(*args, **kwargs):
        assert kwargs.get("registry") is None
        return original_launch(
            *args,
            **{
                **kwargs,
                "stop": stop,
                "service_options": options,
                "ready": lambda: write_private(root / "ready.json", {"ready": True}),
            },
        )

    api.launch = launch
    code = main(
        [
            "--config",
            str(config),
            "--json",
            "workflow",
            "owner",
            "--installation",
            str(author.installation),
        ]
    )
    assert code == 0, code
