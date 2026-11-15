"""Consume an independent console observation bound to the existing enrollment."""

from dataclasses import asdict, replace

from ai_task_manager.adapters.hosts.bridge.human import HumanReceipts
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .observations import current_enrollment, identity

PREFIX = "session-human-receipt:"


def verify_receipt(authority, reference, *, fresh=False):
    """Local consumption only; recorded intent may outlive its capture admission window."""
    source = authority._get(reference, PREFIX)
    current_enrollment(authority, source["enrollment"], source["scope"])
    for name in ("journal", "key", "config", "capability"):
        if identity(source[name]["path"], content=name != "journal") != source[name]:
            raise AccessDenied("Original independent human source changed")
    journal = HostJournal.open_existing(
        source["journal"]["path"], source["key"]["path"], clock=authority.clock
    )
    record = journal._read(source["human_ref"], "human", fresh=fresh)
    observed = HumanReceipts(journal).verify_instruction(source["human_ref"], fresh=fresh)
    if digest(record) != source["human_hash"] or asdict(observed.scope) != source["scope"]:
        raise AccessDenied("Human instruction differs from its enrolled independent source")
    return replace(observed, source_hash=reference)
