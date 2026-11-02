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
    allowed = required | {"writer", "grant_ref", "linear"}
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
    private_path(config["core_database"])
    journal = AuthorityJournal(config["authority_database"], config["authority_key"])
    database, claim_store = open_claim_store(config["core_database"], journal)
    decisions = DecisionAuthority(journal, observations)
    components = linear_components
    if config.get("linear"):
        components = components or LinearComponents(config["linear"])
        if components.configuration_hash != digest(config["linear"]):
            raise AccessDenied("Linear composition does not match sealed configuration")
        principal_verifier = principal_verifier or components.journal
        evidence_factory = evidence_factory or components.evidence
        renderer = renderer or components.renderer
        provider_factory = provider_factory or components.outbound
        inbound_factory = inbound_factory or components.inbound
        existing_factory = existing_factory or components.existing
        publisher_factory = publisher_factory or components.publisher
        session_control_factory = session_control_factory or (
            lambda core, handle, decisions: components.sessions(
                core, handle, decisions, config["session_file"]
            )
        )
    renderer = renderer or ExactRenderer()
    writer = None
    if config.get("writer"):
        values = config["writer"]
        if set(values) != {"root", "python", "pins", "sessions_root", "journal", "key"}:
            raise ValidationError("Unknown writer configuration")
        package = WriterPackage(Path(values["root"]).resolve(), values["python"], values["pins"])
        writer = WritingService(
            package,
            AuthorityJournal(values["journal"], values["key"]),
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
    )
    handle = SessionFile(config["session_file"]).load(core)
    provider = provider_factory(core, handle, decisions, writer) if provider_factory else None
    workers = workers_factory(core, handle, decisions) if workers_factory else None
    publisher = publisher_factory(core, handle, writer) if publisher_factory else None
    sessions = session_control_factory(core, handle, decisions) if session_control_factory else None
    inbound = inbound_factory(core, handle) if inbound_factory else None
    existing = existing_factory(core, handle) if existing_factory else None
    return ApplicationService(
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
    )
