"""Current operator admission selects a signed successor intent, never JSON proof."""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import (
    SessionObservations,
    bind_record,
    current_enrollment,
    intent,
)
from ai_task_manager.adapters.proofs.store import RuntimeProofs
from ai_task_manager.adapters.workflow_control.identity import file_binding, source_digest
from ai_task_manager.adapters.workflow_control.recovery import _existing, physical
from ai_task_manager.application.core import Core
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.application.service.models import result
from ai_task_manager.application.workflows.recovery import SuccessorAuthorization, admit_successor
from ai_task_manager.config.claim_store import open_claim_store
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import ApprovalPolicy, Scope, SessionHandle
from ai_task_manager.installation.sessions.recovery import history, setup_lock
from ai_task_manager.supervisor.journal import RuntimeJournal

from .cold import ColdRecovery
from .spec import Installation

PREFIX = "workflow-successor-intent:"


def _reviewer_specs(spec, config_file):
    def pointers(value):
        if isinstance(value, list):
            return [pointer for item in value for pointer in pointers(item)]
        if isinstance(value, dict):
            return (
                [value]
                if "path" in value
                else [pointer for item in value.values() for pointer in pointers(item)]
            )
        return []

    result = []
    for pointer in pointers(spec.data.get("reviewer_installation")):
        if file_binding(pointer["path"]) != pointer:
            raise AccessDenied("Original reviewer installation changed")
        selected = Installation(pointer["path"], config_file, retained=True)
        if (
            selected.data["scope"] != spec.data["scope"]
            or selected.data["task_id"] == spec.data["task_id"]
        ):
            raise AccessDenied("Reviewer belongs to another task family")
        result.append((pointer, selected))
    return result


def _context(config_file, components=None):
    config = read_private(config_file)
    journal = AuthorityJournal.open_existing(config["authority_database"], config["authority_key"])
    database, claims = open_claim_store(config["core_database"], journal, existing_only=True)
    components = components or LinearComponents(config["linear"], existing_only=True)
    observations = SessionObservations(
        journal, components.client, config_file, config["session_observations"]
    )
    grant = observations.verify_enrollment(config["grant_ref"])
    if "successor" not in grant.actions:
        raise AccessDenied("Explicit current successor delegation is required")
    components.client.check_connection(grant.scope, refresh=True)
    core = Core(
        database, ApprovalPolicy(**config["approval_policy"]), evidence=components.evidence(journal)
    )
    claims.check()
    return config, journal, core, DecisionAuthority(journal, observations), grant


def _local_context(config_file):
    """Consume the exact freshly observed enrollment without provider I/O in Core."""
    config = read_private(config_file)
    journal = AuthorityJournal.open_existing(config["authority_database"], config["authority_key"])
    enrolled = current_enrollment(journal, config["grant_ref"])
    if (
        enrolled["config_path"] != str(Path(config_file).absolute())
        or enrolled["config_intent"] != intent(config)
        or enrolled["provider"] != config["linear"]["expected"]
        or "successor" not in enrolled["actions"]
    ):
        raise AccessDenied("Fresh successor enrollment differs from the configured source")
    from ai_task_manager.adapters.decisions.observations import identity

    pointer = config["session_observations"]
    if pointer != {
        "reference": config["grant_ref"],
        "journal": identity(journal.path),
        "key": identity(journal.key_path, content=True),
    }:
        raise AccessDenied("Original successor enrollment files changed")
    completed = journal._get(
        "session-setup-result:" + digest(config["grant_ref"]), "session-setup-result:"
    )
    if completed["scope"] != enrolled["scope"] or completed["session_file"] != identity(
        config["session_file"], content=True
    ):
        raise AccessDenied("Staged successor capability changed")
    database, claims = open_claim_store(config["core_database"], journal, existing_only=True)
    claims.check()
    core = Core(database, ApprovalPolicy(**config["approval_policy"]), evidence=journal)
    return config, journal, core, Scope(**enrolled["scope"])


def prepare(
    config_file,
    installation_file,
    successor_config,
    successor_installation,
    *,
    workflow_ref,
    original_key,
    operation_key,
    components=None,
):
    """Trusted installer binds an already staged fresh enrollment and installation.

    Session successor setup verifies the current account before this call. The
    public recovery request receives only the returned opaque signed reference.
    No native launch, provider mutation or old authority renewal occurs here.
    """
    original = read_private(config_file)
    spec = Installation(successor_installation, successor_config)
    fresh, journal, core, decisions, grant = _context(successor_config, components)
    old_spec = Installation(installation_file, config_file, retained=True)
    for field in ("core_database", "authority_database", "authority_key", "linear"):
        if fresh[field] != original[field]:
            raise AccessDenied("Successor cannot change original physical history")
    intent_ref = PREFIX + digest(
        {
            "config": str(Path(successor_config).absolute()),
            "predecessor": workflow_ref,
            "key": operation_key,
        }
    )
    prior = history(journal, intent_ref)
    if prior is not None:
        expected = {
            "config": file_binding(config_file),
            "installation": file_binding(installation_file),
            "successor_config": file_binding(successor_config),
            "successor_installation": file_binding(successor_installation),
        }
        if (
            any(prior.get(k) != v for k, v in expected.items())
            or prior["original_key"] != original_key
        ):
            raise AccessDenied("Successor setup key has different immutable inputs")
        return intent_ref
    if any(
        spec.data[field] != old_spec.data[field]
        for field in ("state_root", "repository", "source_head", "files", "artifacts")
    ):
        raise AccessDenied("Successor must retain original work and repository scope")
    old_reviewers = _reviewer_specs(old_spec, config_file)
    fresh_reviewers = _reviewer_specs(spec, successor_config)
    if [(s.data["task_id"], s.data["prose_digest"]) for _, s in old_reviewers] != [
        (s.data["task_id"], s.data["prose_digest"]) for _, s in fresh_reviewers
    ]:
        raise AccessDenied("Successor must preserve its required reviewer task identities")
    task_id = old_spec.data["task_id"]
    old_handle = SessionHandle(read_private(original["session_file"])["capability"])
    observed = journal._evidence(
        "workflow_successor_observation",
        grant.scope,
        task_id,
        {"predecessor": workflow_ref},
        grant.source_ref,
        grant.expires_at,
    )
    task = core.successor_subject(old_handle, observed)
    if (spec.data["task_id"], spec.data["scope"], spec.data["prose_digest"]) != (
        task.id,
        asdict(grant.scope),
        task.prose_digest,
    ):
        raise AccessDenied("Successor installation changes the approved task")
    # The host name is retained from the original plan, never supplied by JSON.
    from ai_task_manager.adapters.workflow_control.lineage import resolve_control

    from .bootstrap import activation_reference

    control = resolve_control(config_file)
    activation = history(journal, activation_reference(config_file, control))
    if activation is None:
        raise EvidenceRequired("Original installation activation is unavailable")
    cold = ColdRecovery.admit(
        config_file,
        installation_file,
        {
            "action": "inspect",
            "workflow_ref": workflow_ref,
            "operation_key": original_key,
        },
        control,
        activation,
    )
    store = cold.store()
    if store.original_key(workflow_ref, grant.scope) != original_key:
        raise AccessDenied("Original operation key differs")
    row = store.read(workflow_ref, grant.scope)
    plan = spec.plan(row["plan"]["host"])
    if row["plan"].get("commit_checks"):
        from dataclasses import replace

        plan = replace(plan, commit_checks=tuple(row["plan"]["commit_checks"]))
    decision = decisions.decision(
        grant.scope, task, "In Progress", "start", grant_ref=grant.source_ref
    )
    ref = "decision-proof:" + str(uuid4())
    a = SuccessorAuthorization(
        ref,
        grant.scope,
        workflow_ref,
        task.id,
        task.version,
        task.decision_revision,
        plan.id,
        plan.digest,
        operation_key,
        grant.expires_at,
        decision,
        tuple(unit for unit in task.required_work if unit not in task.completed_work),
        tuple(selected.data["task_id"] for _, selected in _reviewer_specs(old_spec, config_file)),
    )
    bind_record(
        journal,
        ref,
        {
            "kind": "workflow_successor",
            "scope": asdict(grant.scope),
            "subject": task.id,
            "payload": asdict(a),
            "source_ref": grant.source_ref,
            "expires_at": a.expires_at,
        },
    )
    bind_record(
        journal,
        "workflow-successor-store:" + digest(grant.source_ref),
        {
            "config": file_binding(successor_config),
            "workflow": cold.witness["workflow"],
            "files": cold.witness["files"],
            "source": source_digest(),
            "intent": intent_ref,
        },
    )
    bind_record(
        journal,
        intent_ref,
        {
            "config": file_binding(config_file),
            "installation": file_binding(installation_file),
            "successor_config": file_binding(successor_config),
            "successor_installation": file_binding(successor_installation),
            "old_session": file_binding(original["session_file"]),
            "new_session": file_binding(fresh["session_file"]),
            "source": source_digest(),
            "authorization": asdict(a),
            "original_key": original_key,
            "plan": asdict(plan),
            "runtime_digest": spec.data["native_digest"] + ":" + plan.digest,
        },
    )
    return intent_ref


class SettledHistory:
    """Recheck original owners and completed local effects before publication."""

    def __init__(self, cold, core, old_handle):
        self.cold, self.core, self.old_handle = cold, core, old_handle
        self.connections = None

    def check(self, row, authority):
        self.cold.check()
        self.cold.validate_stores()
        controller = row.get("claim_controller")
        if row["owner"] is not None:
            if not isinstance(controller, dict):
                raise EvidenceRequired("Original workflow controller is unresolved")
            self.cold.verify_controller(
                {k: v for k, v in controller.items() if k != "instance"},
                instance=controller.get("instance"),
            )
        # Eligibility needs expiry or actual revocation; this read never renews it.
        with self.core.context.db.transaction() as conn:
            from ai_task_manager.storage.sessions import token_hash

            old = conn.execute(
                "SELECT revoked FROM sessions WHERE token_hash=?",
                (token_hash(self.old_handle.token),),
            ).fetchone()
            if old is None or (not old[0] and row["plan"]["deadline"] > self.core.context.clock()):
                raise AccessDenied("Original execution is neither expired nor revoked")
        files = self.cold.witness["files"]
        retained = row.get("recovery_history")
        if not isinstance(retained, dict) or any(
            retained.get("files", {}).get(name) != files[name]
            for name in ("core", "workflow", "runtime", "host", "provider")
        ):
            raise EvidenceRequired("Original workflow forward ownership history is unavailable")
        runtime = RuntimeJournal.open_existing(files["runtime"]["path"])
        proofs = RuntimeProofs(runtime, files["runtime_key"]["path"], existing_only=True)
        selected = (authority.task_id, *authority.consumers)
        with runtime.transaction() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM runtime_attempts WHERE task_id IN ("
                    + ",".join("?" for _ in selected)
                    + ")",
                    selected,
                )
            ]
        if row["attempt_id"] and row["attempt_id"] not in ids:
            raise EvidenceRequired("Original attempt has no matching runtime ownership")
        for attempt_id in ids:
            with runtime.transaction() as conn:
                receipts = [
                    r[0]
                    for r in conn.execute(
                        "SELECT ref FROM runtime_receipts WHERE json_extract(data,'$.subject')=? "
                        "AND json_extract(data,'$.kind')='attempt_outcome' ORDER BY rowid",
                        (attempt_id,),
                    )
                ]
            if not receipts:
                raise EvidenceRequired("Retained runtime has no signed terminal settlement")
            proof = proofs.verify_evidence(receipts[-1], historical=True)
            if (
                proof.scope != authority.scope
                or proof.payload.get("quiescent") is not True
                or proof.payload.get("state") not in {"done", "failed", "paused", "canceled"}
            ):
                raise EvidenceRequired("Original runtime is not completely settled")

        if row["attempt_id"]:
            from ai_task_manager.adapters.resources.manager import ResourceManager
            from ai_task_manager.supervisor.models import ResourcePair

            resources = ResourceManager(
                runtime,
                Path(self.cold.spec.data["state_root"]) / "resources",
                self.cold.spec.data["repository"],
                existing_only=True,
            )
            ready = runtime.observations(row["attempt_id"], "resources_ready")
            if not ready:
                raise EvidenceRequired("Original owned worktree and database facts are unavailable")
            fact = ready[-1]
            pair = ResourcePair(
                row["attempt_id"],
                Path(fact["worktree"]["path"]),
                Path(fact["database"]["path"]),
                fact["worktree_id"],
                fact["database_id"],
                fact["id"],
            )
            resources.verify(pair)
        saved = row["steps"].get("code-plan", {}).get("payload")
        if saved:
            from ai_task_manager.application.merging.workflows.store import MergeWorkflowStore
            from ai_task_manager.installation.workflows.code_delivery import restored_plan
            from ai_task_manager.storage.merging.journal import Journal

            bindings = saved.get("history")
            if not bindings:
                raise EvidenceRequired("Original merge and check ownership facts are unavailable")
            journal = Journal.open_existing(bindings["merge"])
            original_plan = restored_plan(saved["plan"])
            matches = [
                op
                for op in journal.for_attempt(row["attempt_id"])
                if op["facts"].get("merge_workflow", {}).get("request", {}).get("plan")
                == original_plan.digest
                and op["facts"]["merge_workflow"]["request"]["operation_key"]
                == row["id"] + ":merge"
            ]
            if len(matches) > 1:
                raise EvidenceRequired("Original merge lineage is ambiguous")
            for operation in matches:
                workflow = operation["facts"]["merge_workflow"]
                if any(
                    name in workflow["steps"]
                    for name in (
                        "publication_intent",
                        "publication_fence",
                        "publication",
                        "delivery",
                    )
                ):
                    raise EvidenceRequired(
                        "Frozen publication requires original effect reconciliation"
                    )
                if any(step["result"] is None for step in workflow["steps"].values()):
                    raise EvidenceRequired(
                        "Unresolved merge stages retain their original authority"
                    )
                if workflow["owner"] is not None:
                    MergeWorkflowStore(journal).release_dead_claim(
                        operation["ref"],
                        workflow["owner"],
                        lambda owner: self.cold.verify_controller(
                            {"pid": owner["pid"], "start": owner["birth"]}
                        ),
                    )
        from ai_task_manager.adapters.hosts.bridge.journal import HostJournal

        parent = HostJournal.open_existing(files["host"]["path"], files["host_key"]["path"])
        traces = [record["payload"] for record in parent._history("trace")]
        for pointer, spec in _reviewer_specs(self.cold.spec, self.cold.config_file):
            with self.core.context.db.transaction() as conn:
                attempt_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM attempts WHERE task_id=?", (spec.data["task_id"],)
                    )
                ]
            if not attempt_ids:
                continue
            facts = [
                trace
                for trace in traces
                if trace.get("effect") == "reviewer_state_created"
                and trace.get("installation") == pointer
                and trace.get("task_id") == spec.data["task_id"]
                and trace.get("scope") == asdict(authority.scope)
            ]
            if not facts:
                raise EvidenceRequired("Original reviewer forward ownership is unavailable")
            reviewer = facts[-1]["files"]
            for name, binding in reviewer.items():
                if (
                    file_binding(binding["path"])
                    if name.endswith("_key")
                    else physical(binding["path"])
                ) != binding:
                    raise AccessDenied("Original reviewer history changed")
            child = RuntimeJournal.open_existing(reviewer["runtime"]["path"])
            proofs = RuntimeProofs(child, reviewer["runtime_key"]["path"], existing_only=True)
            hosts = HostJournal.open_existing(
                reviewer["host"]["path"], reviewer["host_key"]["path"]
            )
            for attempt_id in attempt_ids:
                managed = hosts.intent("managed-owner:" + attempt_id)
                if managed is None:
                    raise EvidenceRequired("Original reviewer managed ownership is unresolved")
                self.cold.verify_controller(managed.get("controller"))
                with child.transaction() as conn:
                    refs = [
                        r[0]
                        for r in conn.execute(
                            "SELECT ref FROM runtime_receipts "
                            "WHERE json_extract(data,'$.subject')=? "
                            "AND json_extract(data,'$.kind')='attempt_outcome' ORDER BY rowid",
                            (attempt_id,),
                        )
                    ]
                if (
                    not refs
                    or proofs.verify_evidence(refs[-1], historical=True).payload.get("quiescent")
                    is not True
                ):
                    raise EvidenceRequired("Original reviewer is not provably settled")
        with self.core.context.db.transaction() as conn:
            selected = (authority.task_id, *authority.consumers)
            marks = ",".join("?" for _ in selected)
            self.operation_ids = tuple(
                r[0]
                for r in conn.execute(f"SELECT id FROM outbox WHERE task_id IN ({marks})", selected)
            )
            self.issue_ids = tuple(
                r[0]
                for r in conn.execute(f"SELECT issue_id FROM tasks WHERE id IN ({marks})", selected)
            )

    @contextmanager
    def boundary(self, row, authority):
        with ExitStack() as stack:
            self.connections = {
                name: stack.enter_context(_existing(binding, name))
                for name, binding in self.cold.witness["files"].items()
                if name in {"runtime", "host", "provider"}
            }
            self.check_locked(row, authority)
            try:
                yield
            finally:
                self.connections = None

    def check_locked(self, row, authority):
        if self.connections is None:
            raise EvidenceRequired("Original local reconciliation boundary is unavailable")
        # Validate every forward file without process waits or network I/O here.
        for binding in self.cold.witness["files"].values():
            if (
                file_binding(binding["path"]) if "sha256" in binding else physical(binding["path"])
            ) != binding:
                raise AccessDenied("Original successor history was replaced")
        provider = self.connections.get("provider")
        if provider is not None:
            from ai_task_manager.adapters.linear.receipts import encoded

            scoped = encoded(asdict(authority.scope))
            operations = self.operation_ids or ("__no_original_operation__",)
            marks = ",".join("?" for _ in operations)
            if provider.execute(
                "SELECT 1 FROM sends s WHERE scope=? AND state NOT IN ('confirmed','failed') "
                f"AND operation_id IN ({marks}) AND attempt=(SELECT MAX(t.attempt) FROM sends t "
                "WHERE t.scope=s.scope AND t.operation_id=s.operation_id) LIMIT 1",
                (scoped, *operations),
            ).fetchone():
                raise EvidenceRequired("Original provider effects are unresolved")
            issues = self.issue_ids or ("__no_original_issue__",)
            marks = ",".join("?" for _ in issues)
            if provider.execute(
                "SELECT 1 FROM deliveries WHERE scope=? AND state NOT IN ('applied','ignored') "
                f"AND json_extract(payload,'$.data.id') IN ({marks}) LIMIT 1",
                (scoped, *issues),
            ).fetchone():
                raise EvidenceRequired("Original inbound observations are unresolved")
            tables = {
                row[0]
                for row in provider.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "progress_publications" in tables:
                tasks = (authority.task_id, *authority.consumers)
                marks = ",".join("?" for _ in tasks)
                if provider.execute(
                    "SELECT 1 FROM progress_publications WHERE scope=? "
                    f"AND task_id IN ({marks}) AND state NOT IN ('confirmed','failed') LIMIT 1",
                    (scoped, *tasks),
                ).fetchone():
                    raise EvidenceRequired("Original progress publication is unresolved")


def recover(config_file, installation_file, value):
    original = read_private(config_file)
    journal = AuthorityJournal.open_existing(
        original["authority_database"], original["authority_key"]
    )
    data = history(journal, value["authorization_ref"])
    if data is None or not value["authorization_ref"].startswith(PREFIX):
        raise AccessDenied("Original signed successor intent is unavailable")
    for field, path in (
        ("config", config_file),
        ("installation", installation_file),
        ("successor_config", data["successor_config"]["path"]),
        ("successor_installation", data["successor_installation"]["path"]),
        ("old_session", original["session_file"]),
        ("new_session", data["new_session"]["path"]),
    ):
        if file_binding(path) != data[field]:
            raise AccessDenied("Signed successor input is missing or changed")
    a = dict(data["authorization"])
    a["scope"] = Scope(**a["scope"])
    a["remaining_work"], a["consumers"] = tuple(a["remaining_work"]), tuple(a["consumers"])
    a = SuccessorAuthorization(**a)
    if (a.predecessor, data["original_key"], data["source"]) != (
        value["workflow_ref"],
        value["operation_key"],
        source_digest(),
    ):
        raise AccessDenied("Successor selection differs from original signed intent")
    with setup_lock(config_file):
        if journal.clock() >= a.expires_at:
            from ai_task_manager.adapters.workflow_control.lineage import resolve_control

            from .bootstrap import activation_reference

            control = resolve_control(config_file)
            activation = history(journal, activation_reference(config_file, control))
            observed = ColdRecovery.admit(
                config_file,
                installation_file,
                {
                    "action": "inspect",
                    "workflow_ref": a.predecessor,
                    "operation_key": value["operation_key"],
                },
                control,
                activation,
            )
            observed.validate_stores()
            recorded = observed.store().recovery_result(a.scope, a.predecessor, asdict(a))
            if recorded is None:
                raise AccessDenied("Expired authority cannot admit an unrecorded successor")
            return result(
                {
                    "workflow_ref": recorded,
                    "predecessor": a.predecessor,
                    "state": "successor_recorded",
                    "execution": "authority_expired",
                    "operation_key": a.operation_key,
                }
            )
        fresh, _, core, scope = _local_context(data["successor_config"]["path"])
        if scope != a.scope:
            raise AccessDenied("Current successor enrollment changed scope")
        from ai_task_manager.adapters.workflow_control.lineage import resolve_control

        from .bootstrap import activation_reference

        control = resolve_control(config_file)
        activation = history(journal, activation_reference(config_file, control))
        cold = ColdRecovery.admit(
            config_file,
            installation_file,
            {
                "action": "inspect",
                "workflow_ref": a.predecessor,
                "operation_key": value["operation_key"],
            },
            control,
            activation,
        )
        plan = dict(data["plan"])
        from ai_task_manager.application.workflows.models import WorkflowPlan

        for field in ("allowed_files", "artifacts", "commit_checks"):
            if field in plan:
                plan[field] = tuple(plan[field])
        old_handle = SessionHandle(read_private(original["session_file"])["capability"])
        new_handle = SessionHandle(read_private(fresh["session_file"])["capability"])
        reference = admit_successor(
            core,
            old_handle,
            new_handle,
            cold.store(),
            a,
            WorkflowPlan(**plan),
            data["runtime_digest"],
            SettledHistory(cold, core, old_handle),
        )
        return result(
            {
                "workflow_ref": reference,
                "predecessor": a.predecessor,
                "state": "successor_admitted",
                "operation_key": a.operation_key,
            }
        )
