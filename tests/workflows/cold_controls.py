"""Authored installed service/native ports; no genuine provider/model evidence."""

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace


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
    from tests.workflow_installation.code_controls import ClaudePermits

    original = service.load_service

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
            "observations": observations,
            "provider_factory": lambda *_: SimpleNamespace(),
            "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
            "evidence_factory": lambda j: EvidenceRouter(
                [EvidenceRoute("decision-proof:", frozenset({"decision", "human_decision"}), j)]
            ),
        }
        return original(config_file, *args, **{**defaults, **kwargs})

    def resolve(config_file, installation_file, **kwargs):
        spec = json.loads(Path(installation_file).read_text())
        root = Path(config_file).parent
        executable = root / "synthetic-cli"
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

        def prepare(context):
            permits = ClaudePermits(root / "cold-accounting")

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
    if os.environ.get("COLD_TEST_UNKNOWN_INVENTORY") == "1":
        from ai_task_manager.adapters.process import recovery
        from ai_task_manager.adapters.process.groups import GroupObservation

        recovery.observe_recorded_group = lambda *_: GroupObservation(
            False, reason="authored-unknown"
        )
