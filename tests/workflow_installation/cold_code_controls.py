"""Authored provider/native ports around the real installed cold code composition."""

import json
import os
import time
from pathlib import Path
from threading import Timer


def install():
    from ai_task_manager.adapters.decisions.authority import HostGrant
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.config import service
    from ai_task_manager.domains.work.models import Scope
    from ai_task_manager.installation.native import normal
    from tests.application.helpers import Observations
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.native_installation.test_normal import AuthoredRestrictions
    from tests.scheduling.helpers import SnapshotSource

    loading = service.load_service

    class Provider:
        def __init__(self, core, config):
            self.core = core
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
                task.id,
                {
                    "operation_id": operation.id,
                    "status": task.desired_status,
                    "prose_digest": task.prose_digest,
                    "title": task.title,
                    "body": task.body,
                    "representation": task.representation,
                    "issue_id": task.issue_id,
                },
                "authored-cold-code-provider",
            )
            return self.core.intake.confirm_status(
                handle,
                task.id,
                task.version,
                task.decision_revision,
                proof,
                "authored-cold-code-provider:" + operation.id,
            )

    def load(path, *args, **kwargs):
        config = json.loads(Path(path).read_text())
        scope = Scope(**json.loads(Path(config["session_file"]).read_text())["scope"])
        grants = Observations()
        prepared = Path(path).parent / "author/installation.json"
        deadline = json.loads(prepared.read_text())["deadline"]
        grants.grants[config["grant_ref"]] = HostGrant(
            scope, scope.logical_session, ("start", "merge"), "authored-cold-code", deadline
        )
        defaults = {
            "observations": grants,
            "scheduling_sources": SnapshotSource(),
            "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
            "provider_factory": lambda c, h, *_: Provider(c, config),
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
        return loading(path, *args, **{**defaults, **kwargs})

    from ai_task_manager.installation import native

    resolve = native.resolve

    def composed(*args, **kwargs):
        options = {"dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h)}
        if kwargs.get("service_options") is None:
            kwargs["service_options"] = options
        return resolve(*args, **kwargs)

    native.resolve = composed
    service.load_service = load
    from ai_task_manager.installation import workflows
    from ai_task_manager.installation.workflows import composition

    composition.load_service = load
    source = normal.NormalAuthoritySource
    normal.NormalAuthoritySource = lambda *a, **kw: source(
        *a, **{**kw, "conformance": AuthoredRestrictions()}
    )
    launch = workflows.launch

    def bounded(*args, **kwargs):
        if kwargs.get("recovery") is not None:
            ready = kwargs.get("ready")

            def observed():
                Timer(30, kwargs["stop"].set).start()
                if ready:
                    ready()

            kwargs["ready"] = observed
        return launch(*args, **kwargs)

    workflows.launch = bounded
    from ai_task_manager.application.merging.workflows.service import MergeWorkflowService

    step = MergeWorkflowService._step

    def crash(self, ref, owner, name, payload, action):
        result = step(self, ref, owner, name, payload, action)
        marker = Path(os.environ["T04_COLD_ROOT"]) / "crashed.json"
        if name == "checks" and not marker.exists():
            marker.write_text(
                json.dumps(
                    {"merge_workflow": ref, "pid": os.getpid(), "stage": name, "time": time.time()}
                )
            )
            os._exit(71)
        return result

    MergeWorkflowService._step = crash

    from ai_task_manager.application.workflows.driver import WorkflowDriver

    running = WorkflowDriver.run

    def reported(self, reference):
        value = running(self, reference)
        if "detail" in value:
            (Path(os.environ["T04_COLD_ROOT"]) / "driver-result.json").write_text(json.dumps(value))
        return value

    WorkflowDriver.run = reported
