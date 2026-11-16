"""Finite operator recovery capabilities in the original signed authority journal."""

import os
import time
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.workflow_control.identity import file_binding, settings, source_digest
from ai_task_manager.adapters.workflow_control.lineage import replacement_reference, resolve_control
from ai_task_manager.adapters.workflow_control.recovery import _existing, physical
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.installation.sessions.recovery import history
from ai_task_manager.storage.workflows import WorkflowStore

from .bootstrap import activation_reference
from .spec import Installation

PREFIX = "workflow-cold-recovery:"


def stop_reference(config_file, workflow_ref):
    return "workflow-stop:" + digest(
        {"config": str(Path(config_file).absolute()), "workflow": workflow_ref}
    )


class ColdRecovery:
    """Trusted code capability; JSON selects only the original run and action."""

    def __init__(self, config_file, installation_file, reference):
        self.config_file = Path(config_file).absolute()
        self.spec = Installation(installation_file, config_file, retained=True)
        self.config = read_private(config_file)
        self.journal = AuthorityJournal.open_existing(
            self.config["authority_database"], self.config["authority_key"]
        )
        self.reference = reference
        if not isinstance(reference, str) or not reference.startswith(PREFIX):
            raise AccessDenied("Original signed recovery reference required")
        self.data = history(self.journal, reference)
        if self.data is None:
            raise EvidenceRequired("Current recovery intent is unavailable")
        self.previous = settings(self.data["previous"])
        self.successor = settings(self.data["successor"])
        self.activation = history(self.journal, activation_reference(config_file, self.previous))
        if self.activation is None:
            raise EvidenceRequired("Original activation history is unavailable")
        self.witness = self.activation.get("recovery", {})
        self.check()

    @classmethod
    def admit(cls, config_file, installation_file, value, previous, activation):
        """The explicit operator interface records a fresh, finite stop/resume intent."""
        from .recovery import owner_state

        spec = Installation(installation_file, config_file, retained=True)
        config = read_private(config_file)
        journal = AuthorityJournal.open_existing(
            config["authority_database"], config["authority_key"]
        )
        descriptor = read_private(previous["record"])
        if owner_state(descriptor, activation["recovery"]) not in {"dead", "reused"}:
            raise EvidenceRequired("Original owner death must be independently observed")
        instance = str(uuid4())
        parent = Path(previous["endpoint"]).parent
        successor = settings(
            {
                "record": str(parent / ("r" + instance[:12] + ".json")),
                "endpoint": str(parent / ("s" + instance[:12])),
                "instance": instance,
            }
        )
        now = time.time()
        deadline = min(now + 30, spec.data["deadline"]) if value["action"] == "resume" else now + 30
        if deadline <= now:
            raise AccessDenied("Expired execution permission cannot be resumed")
        data = {
            "config": file_binding(config_file),
            "installation": file_binding(installation_file),
            "source": source_digest(),
            "previous": previous,
            "successor": successor,
            "descriptor": file_binding(previous["record"]),
            "request": value,
            "deadline": deadline,
            "operator": {"uid": os.getuid(), "pid": os.getpid()},
        }
        ref = PREFIX + str(uuid4())
        bind_record(journal, ref, data)
        return cls(config_file, installation_file, ref)

    def check(self, *, published=False):
        from .recovery import owner_state

        self.spec.check()
        d = self.data
        if (
            time.time() >= d["deadline"]
            or history(self.journal, self.reference) != d
            or history(self.journal, activation_reference(self.config_file, self.previous))
            != self.activation
            or d["config"] != file_binding(self.config_file)
            or d["installation"] != file_binding(self.spec.seal.path)
            or d["source"] != source_digest()
            or d["descriptor"] != file_binding(self.previous["record"])
        ):
            raise AccessDenied("Current cold recovery authority expired or changed")
        if any(
            self.activation.get(k) != v
            for k, v in {
                "config": d["config"],
                "scope": self.spec.data["scope"],
                "instance": self.previous["instance"],
                "source": d["source"],
                "deadline": self.spec.data["deadline"],
            }.items()
        ):
            raise AccessDenied("Original installed activation changed")
        if self.witness.get("installation") != d["installation"]:
            raise AccessDenied("Recovery installation differs from original activation")
        descriptor = read_private(self.previous["record"])
        if owner_state(descriptor, self.witness) not in {"dead", "reused"}:
            raise EvidenceRequired("Original owner is live or unknown")
        if descriptor.get("instance") != self.previous["instance"] or any(
            descriptor.get(k) != self.activation[k] for k in ("config", "source", "scope")
        ):
            raise AccessDenied("Original owner descriptor differs from activation")
        endpoint = Path(self.previous["endpoint"])
        if endpoint.exists() or endpoint.is_symlink():
            from ai_task_manager.adapters.workflow_control.identity import endpoint_binding

            if endpoint_binding(endpoint) != descriptor["endpoint"]:
                raise AccessDenied("Original owner endpoint was replaced")
        files = self.witness.get("files")
        required = {
            "core",
            "workflow",
            "runtime",
            "host",
            "runtime_key",
            "host_key",
            "review_key",
            "session",
            "authority",
            "authority_key",
        }
        if self.config.get("linear"):
            required.update({"provider", "dependency", "dependency_key"})
        if self.config.get("writer"):
            required.update({"writer", "writer_key"})
        if type(files) is not dict or set(files) != required:
            raise EvidenceRequired("Complete forward installation ownership facts are required")
        for name, binding in files.items():
            observed = (
                file_binding(binding["path"]) if "sha256" in binding else physical(binding["path"])
            )
            if observed != binding:
                raise AccessDenied("Original recovery source is missing or replaced")
        current = resolve_control(self.config_file)
        if current != (self.successor if published else self.previous):
            raise AccessDenied("Another current owner replacement already exists")
        return self

    def store(self):
        self.check()
        binding = self.witness["workflow"]
        return WorkflowStore.open_existing(binding["path"], store_id=binding["store_id"])

    def local_service(self):
        """Observation and stop require no provider client or execution enrollment."""
        from ai_task_manager.adapters.decisions.authority import DecisionAuthority
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
        from ai_task_manager.application.core import Core
        from ai_task_manager.application.service.facade import ApplicationService
        from ai_task_manager.config.claim_store import open_claim_store
        from ai_task_manager.config.sessions import SessionFile
        from ai_task_manager.domains.work.models import ApprovalPolicy

        self.check()
        database, claims = open_claim_store(
            self.config["core_database"], self.journal, existing_only=True
        )
        evidence = EvidenceRouter(
            [
                EvidenceRoute(
                    "decision-proof:", frozenset({"decision", "human_decision"}), self.journal
                )
            ]
        )
        core = Core(database, ApprovalPolicy(**self.config["approval_policy"]), evidence=evidence)
        handle = SessionFile(self.config["session_file"]).load(core)
        return ApplicationService(
            core, handle, decisions=DecisionAuthority(self.journal), claim_store=claims
        )

    def authorize(self, service):
        from ai_task_manager.application.workflows.inspection import current_grant

        self.check()
        if self.data["request"]["action"] != "resume":
            raise AccessDenied("Stop authority cannot create an execution owner")
        if time.time() >= self.spec.data["deadline"] or service.scope != Scope(
            **self.spec.data["scope"]
        ):
            raise AccessDenied("Original execution deadline or scope changed")
        current_grant(service)
        if (
            history(
                self.journal, stop_reference(self.config_file, self.data["request"]["workflow_ref"])
            )
            is not None
        ):
            raise AccessDenied("The original workflow has a current operator stop intent")
        self.check_service(service)

    def check_service(self, service):
        self.check()
        if str(service.core.context.db.path) != self.config["core_database"]:
            raise AccessDenied("Recovery Core differs from original claim store")
        service.claim_store.check()
        if service.scope != Scope(**self.spec.data["scope"]):
            raise AccessDenied("Current recovery session scope differs")
        row = self.store().read(self.data["request"]["workflow_ref"], service.scope)
        if (
            row["task_id"],
            row["plan_id"],
            self.store().original_key(row["id"], service.scope),
        ) != (
            self.spec.data["task_id"],
            self.spec.data["plan_ref"],
            self.data["request"]["operation_key"],
        ):
            raise AccessDenied("Recovery requires the original task, plan and operation key")
        return row

    def publish(self, service, descriptor):
        self.authorize(service)
        if descriptor["pid"] == self.witness["owner"]["pid"]:
            raise AccessDenied("Recovery requires an actual different owner process")
        bind_record(
            self.journal,
            replacement_reference(self.config_file, self.previous),
            {
                "config": self.data["config"],
                "source": self.data["source"],
                "previous": self.previous,
                "successor": self.successor,
                "descriptor": self.data["descriptor"],
                "authority": self.reference,
                "successor_descriptor": file_binding(self.successor["record"]),
            },
        )
        # Only this signed replacement lineage can retire the exact stale socket.
        self.check(published=True)
        endpoint = Path(self.previous["endpoint"])
        if endpoint.exists():
            endpoint.unlink()

    def validate_stores(self):
        self.check()
        for name in ("core", "workflow", "runtime", "host", "provider"):
            if name in self.witness["files"]:
                with _existing(self.witness["files"][name], name):
                    pass

    def release_claim(self, store, scope):
        self.check()
        row = store.read(self.data["request"]["workflow_ref"], scope)
        if row["owner"] is not None:
            expected = {**self.witness["owner"], "instance": self.previous["instance"]}
            store.release_dead_claim(row["id"], scope, row["owner"], expected, self.check)

    def stop(self, service):
        """Observe all original components before issuing a current Core outcome."""
        from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
        from ai_task_manager.adapters.process.recovery import observe_recorded_group, stop_waiting
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
        from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
        from ai_task_manager.application.service.models import result
        from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
        from ai_task_manager.supervisor.journal import RuntimeJournal

        self.validate_stores()
        row = self.check_service(service)
        fence = stop_reference(self.config_file, row["id"])
        if history(self.journal, fence) is None:
            bind_record(
                self.journal,
                fence,
                {
                    "config": self.data["config"],
                    "workflow": row["id"],
                    "operation_key": self.data["request"]["operation_key"],
                    "authority": self.reference,
                },
            )
        attempt_id = row["attempt_id"] or self._reservation(service, row)
        if not attempt_id:
            return result({"workflow": row["id"], "state": "stopped", "attempt_id": None})
        attempt = service.core.get_attempt(service.handle, attempt_id)
        root = Path(self.spec.data["state_root"])
        journal = RuntimeJournal.open_existing(root / "runtime.sqlite")
        hosts = HostJournal.open_existing(root / "host.sqlite", root / "host.key")
        owner = journal.attempt(attempt.id)
        managed = hosts.intent("managed-owner:" + attempt.id)
        if managed is None or managed != {
            "attempt_id": attempt.id,
            "worker_id": owner["worker_id"],
            "scope": self.spec.data["scope"],
            "controller": self.witness["owner"],
            "components": ["harness", "native", "commands", "collectors", "merge"],
        }:
            raise EvidenceRequired("Complete original managed component ownership is unavailable")
        if (
            owner["task_id"] != attempt.task_id
            or owner["scope"] != self.spec.data["scope"]
            or (attempt.worker_id is not None and attempt.worker_id != owner["worker_id"])
        ):
            raise AccessDenied("Original runtime worker differs from the canonical attempt")
        self.check()
        if journal.effect(attempt.id, "launch") is not None:
            stop_waiting(journal, attempt.id, check_authority=self.check)
        components = {"collectors": True}
        # Every record is read through the original host signature verifier.
        traces = [r["payload"] for r in hosts._history("trace")]
        self._related_stopped(service, traces)
        intents = hosts._intents()
        if journal.effect(attempt.id, "native_launch") is not None:
            native = [
                r
                for r in traces
                if r.get("effect") == "native_process_created"
                and r.get("binding", {}).get("attempt_id") == attempt.id
            ]
            if len(native) != 1 or native[0]["binding"]["worker_id"] != owner["worker_id"]:
                raise EvidenceRequired("Original native process ownership is unavailable")
            relay = native[0]["relay"]
            observed = observe_recorded_group(relay["pid"], relay["start_identity"], relay["group"])
            if not observed.known or observed.live_members:
                raise EvidenceRequired("Original native relay has not confirmed its owned stop")
        components["native"] = True
        # A lost reply cannot turn a known command or merge intent into no work.
        for identity, intent in intents:
            if intent.get("attempt_id") != attempt.id:
                continue
            if identity.startswith("container:"):
                matches = [
                    r
                    for r in traces
                    if r.get("effect") == "container_outcome" and r.get("intent") == intent["name"]
                ]
                if not matches or matches[-1].get("confirmed_stop") is not True:
                    self._container_stop(hosts, identity, intent, traces)
            elif identity.startswith("merge:"):
                raise EvidenceRequired(
                    "Protected merge owner needs complete original stop evidence"
                )
        components["commands"] = components["merge"] = True
        if any(
            name == "code-merge" and step["result"] is None for name, step in row["steps"].items()
        ):
            raise EvidenceRequired("Pending merge keeps its separate owner and resources")
        self._native_accounting_stop(self.spec, hosts, traces, attempt.id)
        self.check()
        components["harness"] = True
        self.check()
        observation = journal.observe(
            attempt.id,
            "runtime_outcome",
            {
                "state": "paused",
                "quiescent": True,
                "worker_id": attempt.worker_id,
                "runtime_owner_worker_id": owner["worker_id"],
                "managed_stop": True,
                "reason": "cold_operator_stop",
                "stop_components": components,
                "recovery_authority": self.reference,
                "model_success": "not_established",
            },
        )
        if attempt.state in ACTIVE_ATTEMPTS:
            proofs = RuntimeProofs(journal, root / "runtime.key", existing_only=True)
            current = service.core.context.evidence
            if not isinstance(current, EvidenceRouter):
                raise EvidenceRequired("Original evidence routing is required for Core settlement")
            service.core.context.evidence = EvidenceRouter(
                [*current.routes, EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs)]
            )
            task = service.core.get_task(service.handle, attempt.task_id)
            self.check()
            attempt = service.core.execution.settle(
                service.handle,
                attempt.id,
                task.version,
                task.decision_revision,
                proofs.outcome(
                    service.scope, attempt.id, observation, not_after=self.data["deadline"]
                ),
                self.reference + ":settle",
            )
        self.check()
        return result(
            {
                "workflow": row["id"],
                "attempt_id": attempt.id,
                "state": attempt.state,
                "quiescent": True,
                "components": components,
            }
        )

    def _reservation(self, service, row):
        pending = row["steps"].get("start")
        if pending is None:
            if row["steps"]:
                raise EvidenceRequired("Original reservation history is unavailable")
            return None
        events = service.core.notifications.list_events(service.handle)
        cursor = pending["payload"]["cursor"]
        original = next(
            (e for e in reversed(events) if e.task_id == row["task_id"] and e.sequence <= cursor),
            None,
        )
        reserved = [
            e
            for e in events
            if e.task_id == row["task_id"] and e.sequence > cursor and e.kind == "attempt_reserved"
        ]
        if original is None or len(reserved) > 1:
            raise EvidenceRequired("Original reservation is ambiguous")
        attempt = service.core.execution.existing_reservation(
            service.handle,
            row["task_id"],
            row["plan"]["host"],
            original.payload["task_version"],
            row["decision_revision"],
            pending["payload"]["decision_ref"],
            row["id"] + ":start:reserve",
        )
        if attempt is None:
            if reserved:
                raise EvidenceRequired("Original reserved attempt is unavailable")
            return None
        if len(reserved) != 1 or reserved[0].payload["attempt_id"] != attempt.id:
            raise EvidenceRequired("Original reserved attempt identity changed")
        return attempt.id

    def _related_stopped(self, service, traces):
        """Reviewer tasks keep their own recorded attempts and stop obligations."""
        from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
        from ai_task_manager.adapters.process.recovery import observe_recorded_group, stop_waiting
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
        from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
        from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
        from ai_task_manager.supervisor.journal import RuntimeJournal

        def pointers(value):
            if type(value) is list:
                return [p for item in value for p in pointers(item)]
            if type(value) is dict:
                if "path" in value:
                    return [value]
                return [p for item in value.values() for p in pointers(item)]
            return []

        events = service.core.notifications.list_events(service.handle)
        for pointer in pointers(self.spec.data.get("reviewer_installation")):
            if file_binding(pointer["path"]) != pointer:
                raise AccessDenied("Original reviewer installation changed")
            spec = Installation(pointer["path"], self.config_file, retained=True)
            ids = {
                e.payload["attempt_id"]
                for e in events
                if e.task_id == spec.data["task_id"] and e.kind == "attempt_reserved"
            }
            if not ids:
                continue
            facts = [
                r
                for r in traces
                if r.get("effect") == "reviewer_state_created" and r.get("installation") == pointer
            ]
            if not facts:
                raise EvidenceRequired("Original reviewer ownership facts are unavailable")
            fact = facts[-1]
            if fact["task_id"] != spec.data["task_id"] or fact["scope"] != self.spec.data["scope"]:
                raise AccessDenied("Original reviewer scope changed")
            for name, binding in fact["files"].items():
                actual = (
                    file_binding(binding["path"])
                    if name.endswith("_key")
                    else physical(binding["path"])
                )
                if actual != binding:
                    raise AccessDenied("Original reviewer history changed")
            files = fact["files"]
            journal = RuntimeJournal.open_existing(files["runtime"]["path"])
            hosts = HostJournal.open_existing(files["host"]["path"], files["host_key"]["path"])
            child_traces = [r["payload"] for r in hosts._history("trace")]
            for attempt_id in sorted(ids):
                attempt = service.core.get_attempt(service.handle, attempt_id)
                owner = journal.attempt(attempt_id)
                managed = hosts.intent("managed-owner:" + attempt_id)
                if (
                    not managed
                    or managed.get("controller") != self.witness["owner"]
                    or managed.get("scope") != self.spec.data["scope"]
                    or managed.get("worker_id") != owner["worker_id"]
                    or attempt.task_id != spec.data["task_id"]
                ):
                    raise EvidenceRequired("Original reviewer controller is unavailable")
                self.check()
                if journal.effect(attempt_id, "launch") is not None:
                    stop_waiting(journal, attempt_id, check_authority=self.check)
                if journal.effect(attempt_id, "native_launch") is not None:
                    native = [
                        r
                        for r in child_traces
                        if r.get("effect") == "native_process_created"
                        and r.get("binding", {}).get("attempt_id") == attempt_id
                    ]
                    if len(native) != 1:
                        raise EvidenceRequired("Original reviewer native identity is unavailable")
                    relay = native[0]["relay"]
                    observed = observe_recorded_group(
                        relay["pid"], relay["start_identity"], relay["group"]
                    )
                    if not observed.known or observed.live_members:
                        raise EvidenceRequired("Original reviewer native group remains unresolved")
                for identity, intent in hosts._intents():
                    if intent.get("attempt_id") == attempt_id and identity.startswith("container:"):
                        self._container_stop(hosts, identity, intent, child_traces)
                self._native_accounting_stop(spec, hosts, child_traces, attempt_id)
                if attempt.state not in ACTIVE_ATTEMPTS:
                    continue
                proof = RuntimeProofs(journal, files["runtime_key"]["path"], existing_only=True)
                observed = journal.observe(
                    attempt_id,
                    "runtime_outcome",
                    {
                        "state": "paused",
                        "quiescent": True,
                        "worker_id": attempt.worker_id,
                        "runtime_owner_worker_id": owner["worker_id"],
                        "managed_stop": True,
                        "reason": "cold_parent_stop",
                        "recovery_authority": self.reference,
                        "stop_components": {
                            "native": True,
                            "harness": True,
                            "commands": True,
                            "collectors": True,
                            "merge": True,
                        },
                    },
                )
                original = service.core.context.evidence
                if not isinstance(original, EvidenceRouter):
                    raise EvidenceRequired("Original reviewer evidence route is unavailable")
                service.core.context.evidence = EvidenceRouter(
                    [
                        *[r for r in original.routes if r.prefix != "runtime-proof:"],
                        EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proof),
                    ]
                )
                try:
                    task = service.core.get_task(service.handle, attempt.task_id)
                    self.check()
                    service.core.execution.settle(
                        service.handle,
                        attempt.id,
                        task.version,
                        task.decision_revision,
                        proof.outcome(
                            service.scope, attempt.id, observed, not_after=self.data["deadline"]
                        ),
                        self.reference + ":reviewer:" + attempt.id,
                    )
                finally:
                    service.core.context.evidence = original

    def _native_accounting_stop(self, spec, hosts, traces, attempt_id):
        """Reconcile current stop facts without resolving normal execution permission."""
        from ai_task_manager.adapters.process.recovery import observe_recorded_group
        from ai_task_manager.installation.native.controller import NormalInvocationController
        from ai_task_manager.installation.native.manifest import NativeManifest
        from ai_task_manager.installation.native.store import NativeStore

        if "native_manifest" not in spec.data:
            return
        manifest = NativeManifest(spec.data["native_manifest"], self.config_file, spec.seal.path)
        pointer = manifest.data.get("authority_store")
        if pointer is None:
            raise EvidenceRequired("Original native accounting authority is unavailable")
        store = NativeStore(pointer["path"], pointer)
        permission = store.permission(manifest.data["authorization_ref"])
        if (
            permission["scope"] != self.spec.data["scope"]
            or permission["task_id"] != spec.data["task_id"]
            or permission["manifest_digest"] != manifest.digest
            or permission["deadline"] != spec.data["deadline"]
        ):
            raise AccessDenied("Original native accounting permission changed")
        controller = NormalInvocationController(store, permission)
        calls = controller.snapshot()["calls"]
        selected = [
            c for c in calls if c["metadata"]["invocation_binding"]["attempt_id"] == attempt_id
        ]
        for call in selected:
            if call["process_stopped"]:
                continue
            purpose = call["metadata"]["invocation_binding"]["purpose"]
            if purpose == "auth" and call["launch_token"] is not None:
                facts = [
                    r
                    for r in traces
                    if r.get("effect") == "auth_process_created"
                    and r.get("invocation") == call["call_id"]
                ]
                if len(facts) != 1 or facts[0]["binding"]["attempt_id"] != attempt_id:
                    raise EvidenceRequired(
                        "Original authentication process identity is unavailable"
                    )
                fact = facts[0]
                observed = observe_recorded_group(
                    fact["pid"], fact["start_identity"], fact["group"]
                )
                if not observed.known or observed.live_members:
                    raise EvidenceRequired("Original authentication process remains unresolved")
        # Validate the complete call set before releasing even its host slot.
        for call in selected:
            if call["process_stopped"]:
                continue
            self.check()
            receipt = hosts._append(
                "trace",
                {
                    "effect": "accounting_cold_stop",
                    "attempt_id": attempt_id,
                    "call_id": call["call_id"],
                    "authority": self.reference,
                    "process_stopped": True,
                    "model_success": "not_established",
                },
            )
            controller.record_outcome(
                call["call_id"],
                outcome=call["outcome"] or "interrupted",
                process_stopped=True,
                evidence_ref=receipt,
                settlement_check=self.check,
            )

    def _container_stop(self, hosts, identity, intent, traces):
        import hashlib
        import hmac
        import json

        from ai_task_manager.adapters.process.recovery import observe_recorded_group

        started = [
            r
            for r in traces
            if r.get("effect") == "container_watcher_created" and r.get("intent") == identity
        ]
        if len(started) != 1 or "watch" not in started[0] or "recovery_key" not in intent:
            raise EvidenceRequired("Original container recovery identity is unavailable")
        fact = started[0]
        if file_binding(fact["watch"]["path"]) != fact["watch"]:
            raise AccessDenied("Original container watcher source changed")
        plan = read_private(fact["watch"]["path"])
        if (plan["owner"], plan["name"], plan["recovery_key"]) != (
            intent["owner"],
            intent["name"],
            intent["recovery_key"],
        ):
            raise AccessDenied("Original container owner changed")
        self.check()
        observed = observe_recorded_group(fact["pid"], fact["start_identity"], fact["pid"])
        if not observed.known or observed.live_members:
            raise EvidenceRequired("Original container watcher has not stopped")
        cleanup = read_private(Path(intent["directory"]) / "cleanup.json")
        body = {k: v for k, v in cleanup.items() if k != "recovery_mac"}
        signature = hmac.new(
            bytes.fromhex(intent["recovery_key"]),
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
            hashlib.sha256,
        ).hexdigest()
        if (
            not hmac.compare_digest(cleanup.get("recovery_mac", ""), signature)
            or cleanup.get("watcher_pid") != fact["pid"]
            or cleanup.get("owner") != intent["owner"]
            or cleanup.get("name") != intent["name"]
            or cleanup.get("container", {}).get("confirmed_stop") is not True
        ):
            raise EvidenceRequired("Original container cleanup is missing or unconfirmed")
        self.check()
        hosts._append(
            "trace",
            {
                "effect": "container_cold_stop",
                "intent": identity,
                "confirmed_stop": True,
                "watcher": observed.record(),
                "cleanup": file_binding(Path(intent["directory"]) / "cleanup.json"),
                "authority": self.reference,
            },
        )
