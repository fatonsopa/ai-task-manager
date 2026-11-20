"""Authored provider/native ports around the real installed cold code composition."""

import json
import os
import time
from pathlib import Path
from threading import Thread


class DeclaredMilestone:
    """Authored publication fact; real Core/source still enforce the declared edge."""

    def __init__(self, core, handle, data, fallback=None):
        self.core, self.handle, self.data, self.fallback = core, handle, data, fallback
        self.until = 0

    def prepare_merge_milestone(self, subject, reference):
        from ai_task_manager.domains.work.errors import EvidenceRequired

        task = self.core.get_task(self.handle, subject)
        if (subject != self.data["predecessor"] or reference != self.data["merge_ref"]
                or not task.delivered_once or task.human_hold or task.sync_state != "confirmed"):
            raise EvidenceRequired("Current authored predecessor publication required")
        self.until = self.core.context.clock() + 5
        return reference

    def verify_evidence(self, reference):
        from ai_task_manager.domains.work.models import Evidence, Scope

        if reference == self.data["merge_ref"]:
            return Evidence("merge", Scope(**self.data["scope"]), self.data["predecessor"],
                            self.data["merge_payload"], self.until)
        return self.fallback.verify_evidence(reference)


def declared_dependencies(core, handle):
    """Existing production collector/reader/receipts over authored GraphQL."""
    from types import SimpleNamespace
    from ai_task_manager.adapters.linear.client import LinearClient
    from ai_task_manager.adapters.linear.connections import AgentConnection
    from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from ai_task_manager.adapters.linear.models import Credentials, ExpectedBinding
    from tests.scheduling.provider_fixture import ProviderFixture

    root = Path(os.environ["T04_COLD_ROOT"])
    data = json.loads((root / "declared-provider.json").read_text())
    clock = time.time
    core.context.clock = core.context.db.clock = clock
    provider = ProviderFixture.__new__(ProviderFixture)
    provider.rig = SimpleNamespace(core=core, handle=handle)
    provider.scope = core.session_binding(handle)[1]
    provider.expected = ExpectedBinding(**data["expected"])
    provider.connection = AgentConnection(**data["connection"])
    provider.states = data["states"]
    provider.nodes, provider.edges, provider.calls = {}, data["edges"], []
    provider.callback, provider.depth, provider.disabled, provider.page_size = None, 0, False, 50
    provider.schema = json.loads(Path(__file__).parents[1].joinpath(
        "scheduling/fixtures/dependency_schema.json").read_text())
    for task in core.list_tasks(handle):
        if task.issue_id:
            provider.sync(task)
    send = provider.send

    def current_provider(query, variables, credentials):
        for current in core.list_tasks(handle):
            if current.issue_id:
                provider.sync(current)
        return send(query, variables, credentials)

    provider.send = current_provider
    client = LinearClient(provider, lambda: Credentials("authored-token"),
                          provider.expected, max_pages=5)
    client.verify_binding()
    receipts = DependencyReceipts(root / "declared-deps.sqlite", root / "declared-deps.key",
                                 clock=clock)
    core.scheduling.sources = receipts
    return DependencySource(core, handle, client, receipts,
                            milestones=((data["dependent_prose"], data["predecessor_prose"]),))


def install():
    from ai_task_manager.adapters.decisions.authority import HostGrant
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.linear.rate_limits import QuotaStore
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.config import service
    from ai_task_manager.domains.work.models import Scope
    from ai_task_manager.installation.native import normal
    from tests.application.helpers import Observations
    from tests.hosts.test_continuity import AuthoredDependencyCollection
    from tests.linear.helpers import observed_quota
    from tests.native_installation.test_normal import AuthoredRestrictions
    from tests.scheduling.helpers import SnapshotSource

    quota_home = Path(os.environ["T04_COLD_ROOT"]) / "quota-user-home"
    QuotaStore._user_home = staticmethod(lambda: quota_home)

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
        observed_quota(quota_home / ".config" / "ai-task-manager", scope.principal)
        grants = Observations()
        prepared = Path(path).parent / "author/installation.json"
        deadline = json.loads(prepared.read_text())["deadline"]
        grants.grants[config["grant_ref"]] = HostGrant(
            scope, scope.logical_session, ("start", "merge"), "authored-cold-code", deadline
        )
        defaults = {
            "observations": grants,
            "scheduling_sources": SnapshotSource(),
            "dependencies_factory": (declared_dependencies if os.environ.get("T04_DECLARED_COLD")
                                     else lambda c, h: AuthoredDependencyCollection(c, h)),
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
        if os.environ.get("T04_DECLARED_COLD"):
            from ai_task_manager.application.service.linear import DECISION_KINDS
            data = json.loads((Path(os.environ["T04_COLD_ROOT"]) / "declared-provider.json").read_text())
            def evidence(journal):
                # The owner binds Core/handle after loading, before source consumption.
                issuer = DeclaredMilestone(None, None, data)
                return EvidenceRouter([
                    EvidenceRoute("fixture:", frozenset({"external_observation"}), issuer),
                    EvidenceRoute("decision-proof:", DECISION_KINDS | {"provider_status"}, journal),
                ])
            defaults["evidence_factory"] = evidence
        result = loading(path, *args, **{**defaults, **kwargs})
        if os.environ.get("T04_DECLARED_COLD"):
            for route in result.core.context.evidence.routes:
                if type(route.verifier) is DeclaredMilestone:
                    route.verifier.core, route.verifier.handle = result.core, result.handle
        return result

    from ai_task_manager.installation import native

    resolve = native.resolve

    def composed(*args, **kwargs):
        options = {"dependencies_factory": (declared_dependencies if os.environ.get("T04_DECLARED_COLD")
                                           else lambda c, h: AuthoredDependencyCollection(c, h))}
        if kwargs.get("service_options") is None:
            kwargs["service_options"] = options
        return resolve(*args, **kwargs)

    native.resolve = composed
    if os.environ.get("T04_DECLARED_COLD"):
        verify = EvidenceRouter.verify_evidence

        def verify_declared(self, reference):
            issuer = next((route.verifier for route in self.routes
                           if type(route.verifier) is DeclaredMilestone), None)
            if issuer is not None and reference == issuer.data["merge_ref"]:
                return issuer.verify_evidence(reference)
            return verify(self, reference)

        EvidenceRouter.verify_evidence = verify_declared
        def prepare_milestone(self, subject, reference):
            return next(route.verifier for route in self.routes
                        if type(route.verifier) is DeclaredMilestone).prepare_merge_milestone(
                            subject, reference)
        EvidenceRouter.prepare_merge_milestone = prepare_milestone
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
                def stop_owned():
                    end = time.monotonic() + 120
                    marker = Path(os.environ["T04_COLD_ROOT"]) / "stop-cold-owner"
                    while not kwargs["stop"].wait(0.1):
                        if marker.exists() or time.monotonic() >= end:
                            kwargs["stop"].set()
                            return

                Thread(target=stop_owned, daemon=True).start()
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
        if (name == "checks" and os.environ.get("T04_COLD_STAGE", "checks") == "checks"
                and not marker.exists()):
            marker.write_text(
                json.dumps(
                    {"merge_workflow": ref, "pid": os.getpid(), "stage": name, "time": time.time()}
                )
            )
            os._exit(71)
        return result

    MergeWorkflowService._step = crash

    from ai_task_manager.application.commits import WorkerCommits

    commit_run = WorkerCommits.run

    def committed(self, row, plan, intent):
        receipt = commit_run(self, row, plan, intent)
        marker = Path(os.environ["T04_COLD_ROOT"]) / "crashed.json"
        if os.environ.get("T04_COLD_STAGE") == "worker-commit" and not marker.exists():
            marker.write_text(json.dumps({"workflow": row["id"], "pid": os.getpid(),
                                          "stage": "worker-commit", "receipt": receipt,
                                          "time": time.time()}))
            os._exit(71)
        return receipt

    WorkerCommits.run = committed

    from ai_task_manager.application.workflows.driver import WorkflowDriver

    running = WorkflowDriver.run

    def reported(self, reference):
        value = running(self, reference)
        if "detail" in value:
            (Path(os.environ["T04_COLD_ROOT"]) / "driver-result.json").write_text(json.dumps(value))
        return value

    WorkflowDriver.run = reported
