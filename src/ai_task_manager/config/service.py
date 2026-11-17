"""Load a sealed read/write context without deriving authority from the cwd."""

from pathlib import Path

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.adapters.writing.service import WritingService
from ai_task_manager.application.core import Core
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.domains.work.models import ApprovalPolicy
from ai_task_manager.domains.work.ports import ExactRenderer

from .claim_store import open_claim_store
from .files import FileSeal, digest, private_path, read_private
from .sessions import SessionFile


def load_service(
    config_file,
    session_file=None,
    *,
    observations=None,
    provider_factory=None,
    workers_factory=None,
    publisher_factory=None,
    session_control_factory=None,
    principal_verifier=None,
    evidence_factory=None,
    renderer=None,
    inbound_factory=None,
    linear_components=None,
    existing_factory=None,
    scheduling_sources=None,
    dependencies_factory=None,
    workflow_factory=None,
    existing_only=False,
):
    """Factories are trusted installer/host arguments, never JSON import paths."""
    seal = FileSeal(config_file)
    config = read_private(config_file)
    required = {
        "version",
        "core_database",
        "authority_database",
        "authority_key",
        "session_file",
        "approval_policy",
    }
    allowed = required | {
        "writer",
        "grant_ref",
        "linear",
        "workflow_control",
        "session_observations",
        "receiver_file",
        "capacity_policies",
    }
    if (
        not isinstance(config, dict)
        or not required <= config.keys()
        or set(config) - allowed
        or type(config["version"]) is not int
        or config["version"] != 1
    ):
        raise ValidationError("Unknown or incomplete service configuration")
    if any(
        not isinstance(config[key], str) or not Path(config[key]).is_absolute()
        for key in required - {"version", "approval_policy"}
    ):
        raise ValidationError("Configuration paths must be absolute and explicitly bound")
    if (
        session_file is not None
        and Path(session_file).absolute() != Path(config["session_file"]).absolute()
    ):
        raise AccessDenied("Session override does not match the sealed configuration")
    policy = config["approval_policy"]
    if set(policy) != {"writer_hash", "reference_hash", "style_hash"} or any(
        not isinstance(x, str) or len(x) != 64 or any(c not in "0123456789abcdef" for c in x)
        for x in policy.values()
    ):
        raise ValidationError("Pinned approval policy hashes are required")
    # Version-two task authority consumes retained history, including Writer.
    # The marker is read before any constructor could initialize a missing file.
    capability_path = Path(config["session_file"])
    if capability_path.exists() and read_private(capability_path).get("version") == 2:
        existing_only = True

    from ai_task_manager.config.capacity import policies, require_installed

    capacity_policies = policies(config)
    private_path(config["core_database"])
    journal = (AuthorityJournal.open_existing if existing_only else AuthorityJournal)(
        config["authority_database"], config["authority_key"]
    )
    database, claim_store = open_claim_store(
        config["core_database"], journal, existing_only=existing_only
    )
    require_installed(database, capacity_policies)
    components = linear_components
    if config.get("linear"):
        components = components or LinearComponents(config["linear"], existing_only=existing_only)
        if components.configuration_hash != digest(config["linear"]):
            raise AccessDenied("Linear composition does not match sealed configuration")
        selected_connection = components.client.agent_connection

        def current_connection():
            seal.check()
            return selected_connection()

        components.client.agent_connection = current_connection
        principal_verifier = principal_verifier or components.journal
        evidence_factory = evidence_factory or components.evidence
        renderer = renderer or components.renderer
        provider_factory = provider_factory or components.outbound
        inbound_factory = inbound_factory or components.inbound
        existing_factory = existing_factory or components.existing
        scheduling_sources = scheduling_sources or components.scheduling_sources
        dependencies_factory = dependencies_factory or components.dependencies
        publisher_factory = publisher_factory or components.publisher
        session_control_factory = session_control_factory or (
            lambda core, handle, decisions: components.sessions(
                core, handle, decisions, config["session_file"]
            )
        )
    if config.get("session_observations"):
        from ai_task_manager.adapters.decisions.observations import SessionObservations

        if observations is not None or components is None:
            raise AccessDenied("Configured session observations require their real provider source")
        observations = SessionObservations(
            journal, components.client, config_file, config["session_observations"]
        )
    decisions = DecisionAuthority(journal, observations)
    renderer = renderer or ExactRenderer()
    writer = None
    if config.get("writer"):
        values = config["writer"]
        if set(values) != {"root", "python", "pins", "sessions_root", "journal", "key"}:
            raise ValidationError("Unknown writer configuration")
        package = WriterPackage(Path(values["root"]).resolve(), values["python"], values["pins"])
        writer = WritingService(
            package,
            (AuthorityJournal.open_existing if existing_only else AuthorityJournal)(
                values["journal"], values["key"]
            ),
            values["sessions_root"],
            renderer,
        )
    core = Core(
        database,
        ApprovalPolicy(**policy),
        approvals=writer.approvals_for_tasks() if writer else None,
        principals=principal_verifier,
        evidence=evidence_factory(journal) if evidence_factory else journal,
        renderer=renderer,
        scheduling_sources=scheduling_sources,
    )
    handle = SessionFile(config["session_file"]).load(core)
    provider = provider_factory(core, handle, decisions, writer) if provider_factory else None
    workers = workers_factory(core, handle, decisions) if workers_factory else None
    publisher = publisher_factory(core, handle, writer) if publisher_factory else None
    sessions = session_control_factory(core, handle, decisions) if session_control_factory else None
    inbound = inbound_factory(core, handle) if inbound_factory else None
    existing = existing_factory(core, handle) if existing_factory else None
    dependencies = dependencies_factory(core, handle) if dependencies_factory else None
    workflows = None
    if workflow_factory is not None:
        workflows = workflow_factory(core, handle, decisions, provider, workers)
    elif config.get("workflow_control"):
        from dataclasses import asdict

        from ai_task_manager.adapters.workflow_control.client import WorkflowClient

        workflows = WorkflowClient(config_file, asdict(core.session_binding(handle)[1]))
    service = ApplicationService(
        core,
        handle,
        writer=writer,
        decisions=decisions,
        grant_ref=config.get("grant_ref"),
        provider=provider,
        workers=workers,
        publisher=publisher,
        sessions=sessions,
        inbound=inbound,
        seal=seal,
        existing=existing,
        claim_store=claim_store,
        dependencies=dependencies,
        workflows=workflows,
    )

    if config.get("linear"):
        from ai_task_manager.application.linear_sync.inbound import InboundDrain
        from ai_task_manager.application.linear_sync.outbound import OutboundDrain
        from ai_task_manager.application.linear_sync.progress import ProgressPublisher
        from ai_task_manager.application.workflows.inspection import current_grant

        def current_provider_authority():
            seal.check()
            claim_store.check()
            current_grant(service)
            seal.check()
            claim_store.check()

        if isinstance(provider, OutboundDrain):
            provider.check_authority = current_provider_authority
        if isinstance(inbound, InboundDrain):
            inbound.check_authority = current_provider_authority
        if publisher is not None:

            def guarded_publisher(*args, **kwargs):
                current_provider_authority()
                value = publisher(*args, **kwargs)
                if isinstance(value, ProgressPublisher):
                    value.check_authority = current_provider_authority
                return value

            service.publisher = guarded_publisher
    return service
