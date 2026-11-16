"""Authored installed service/native ports; no genuine provider/model evidence."""

import hashlib
import json
import os
import time
from pathlib import Path


def install():
    from ai_task_manager.adapters.decisions.authority import HostGrant
    from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
    from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
    from ai_task_manager.adapters.hosts.bridge.policy import (
        NativePolicy,
        NativeProfile,
        clean_environment,
        policy_digest,
    )
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.config import service
    from ai_task_manager.domains.work.models import Scope
    from ai_task_manager.installation import native
    from ai_task_manager.installation.workflows import (
        NativeAuthorities,
        NativeRegistration,
        NativeRegistry,
    )
    from tests.application.helpers import Observations
    from tests.hosts.test_composition import AuthoredAccounting
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.scheduling.helpers import SnapshotSource
    from tests.workflow_installation.code_controls import ClaudePermits

    original = service.load_service

    class AuthoredColdProvider:
        """Deterministic local provider port for the zero-reservation component control."""

        def __init__(self, core, handle, config):
            from ai_task_manager.adapters.decisions.journal import AuthorityJournal

            self.core, self.handle = core, handle
            self.journal = AuthorityJournal.open_existing(
                config["authority_database"], config["authority_key"]
            )

        def confirm_pending(self, handle, task_id):
            task = self.core.get_task(handle, task_id)
            pending = [
                o
                for o in self.core.sync.list_operations(handle)
                if o.task_id == task_id and o.state != "confirmed"
            ]
            if not pending:
                return task
            operation = pending[-1]
            proof = self.journal._evidence(
                "provider_status",
                self.core.session_binding(handle)[1],
                task_id,
                {
                    "operation_id": operation.id,
                    "status": task.desired_status,
                    "prose_digest": task.prose_digest,
                    "title": task.title,
                    "body": task.body,
                    "representation": task.representation,
                    "issue_id": task.issue_id or "TST:" + task_id,
                },
                "authored-cold-provider",
            )
            return self.core.intake.confirm_status(
                handle,
                task_id,
                task.version,
                task.decision_revision,
                proof,
                "authored-cold-provider:" + operation.id,
            )

    def load(config_file, *args, **kwargs):
        config = json.loads(Path(config_file).read_text())
        scope = Scope(**json.loads(Path(config["session_file"]).read_text())["scope"])
        observations = Observations()
        observations.grants[config["grant_ref"]] = HostGrant(
            scope,
            scope.logical_session,
            ("start", "non_code_complete"),
            "authored-cold-contract",
            time.time() + 120,
        )
        if os.environ.get("COLD_TEST_GRANT_STATE") == "revoked":
            observations.grants.clear()
        defaults = {
            "scheduling_sources": SnapshotSource(),
            "observations": observations,
            "provider_factory": lambda c, h, *_: AuthoredColdProvider(c, h, config),
            "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
            "evidence_factory": lambda j: EvidenceRouter(
                [
                    EvidenceRoute(
                        "decision-proof:",
                        frozenset({"decision", "human_decision", "provider_status"}),
                        j,
                    )
                ]
            ),
        }
        return original(config_file, *args, **{**defaults, **kwargs})

    def resolve(config_file, installation_file, **kwargs):
        spec = json.loads(Path(installation_file).read_text())
        root = Path(config_file).parent
        executable = root / "synthetic-cli"
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

        def prepare(context):
            permits = ClaudePermits(root / ("cold-accounting-" + str(os.getpid())))

            def policy(host, pair):
                from ai_task_manager.adapters.hosts.bridge.admission import MANAGED_TOOLS

                env = clean_environment(root / "home", claude_profile=root / "home/.claude_fixture")
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
                    model="claude-opus-5-5",
                    effort="high",
                    allowed_tools=tuple(sorted(MANAGED_TOOLS)),
                )

            return NativeAuthorities(
                policy, permits, AuthoredAccounting(permits), "synthetic-accounting"
            )

        registration = NativeRegistration(
            spec["native_ref"],
            "claude",
            NativeProfile("claude-opus-5-5", "high"),
            claude_account_hash(
                {
                    "email": "fixture@example.invalid",
                    "orgId": "authored-org",
                    "userId": "authored-user",
                }
            ),
            NativeSourcePins({str(executable): executable_hash}),
            prepare,
        )
        return NativeRegistry(
            (registration,),
            service_options={
                "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
            },
        )

    service.load_service = load
    from ai_task_manager.installation.workflows import composition

    composition.load_service = load
    native.resolve = resolve
    seconds = os.environ.get("COLD_TEST_REPLACEMENT_SECONDS")
    if seconds:
        from threading import Timer

        from ai_task_manager.installation import workflows

        launching = workflows.launch

        def bounded(*args, **kwargs):
            if kwargs.get("recovery") is not None:
                ready = kwargs.get("ready")

                def admitted():
                    Timer(float(seconds), kwargs["stop"].set).start()
                    if ready:
                        ready()

                kwargs["ready"] = admitted
            return launching(*args, **kwargs)

        workflows.launch = bounded
    crash = os.environ.get("COLD_TEST_CRASH")
    reviewer_crash = os.environ.get("COLD_TEST_REVIEWER_CRASH")
    if crash or reviewer_crash:
        from ai_task_manager.application.execution import Execution
        from ai_task_manager.supervisor.journal import RuntimeJournal

        reserving, claiming = Execution.reserve, RuntimeJournal.claim

        def reserve(self, *args, **kwargs):
            result = reserving(self, *args, **kwargs)
            marker = Path(self.c.db.path).parent / "crashed.json"
            peer = marker.parent / "reviewer-task.json"
            if (
                crash == "after_reserve"
                or (
                    reviewer_crash == "after_reserve"
                    and peer.exists()
                    and result.task_id == json.loads(peer.read_text())["task_id"]
                )
            ) and not marker.exists():
                Path(self.c.db.path).parent.joinpath("crashed.json").write_text(
                    json.dumps({"attempt_id": result.id})
                )
                os._exit(86)
            return result

        def claim(self, attempt, *args, **kwargs):
            result = claiming(self, attempt, *args, **kwargs)
            marker = self.path.parent.parent / "crashed.json"
            if (
                crash == "after_claim"
                or (reviewer_crash == "after_claim" and self.path.parent.name == "reviewer-state")
            ) and not marker.exists():
                self.path.parent.parent.joinpath("crashed.json").write_text(
                    json.dumps({"attempt_id": attempt.id})
                )
                os._exit(87)
            return result

        Execution.reserve, RuntimeJournal.claim = reserve, claim
        if crash == "before_reserve":
            from ai_task_manager.supervisor.managed import ManagedWorkers

            starting = ManagedWorkers.start

            def start(self, *args, **kwargs):
                try:
                    return starting(self, *args, **kwargs)
                except Exception:
                    import traceback

                    traceback.print_exc()
                    raise

            ManagedWorkers.start = start
            from ai_task_manager.adapters.hosts.bridge.journal import HostJournal

            intending = HostJournal.intend

            def intend(self, identity, value):
                result = intending(self, identity, value)
                marker = self.path.parent.parent / "crashed.json"
                if identity.startswith("managed-reservation:") and not marker.exists():
                    marker.write_text(json.dumps({"identity": identity, "witness": value}))
                    os._exit(88)
                return result

            HostJournal.intend = intend
    if os.environ.get("COLD_TEST_UNKNOWN_INVENTORY") == "1":
        from ai_task_manager.adapters.process import recovery
        from ai_task_manager.adapters.process.groups import GroupObservation

        recovery.observe_recorded_group = lambda *_: GroupObservation(
            False, reason="authored-unknown"
        )
