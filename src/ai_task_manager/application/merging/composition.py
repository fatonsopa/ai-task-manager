"""Trusted composition fixes publication inputs; callers supply no authority or target."""

from .authority import CoreAuthority
from .check_results import PersistedChecks
from .service import MergeService


class CheckedMerge:
    def __init__(
        self,
        core,
        handle,
        grant,
        resources,
        pair,
        profile,
        journal,
        checks,
        check_ref,
        *,
        publisher_factory,
    ):
        self.grant, self.check_ref = grant, check_ref
        self.authority = CoreAuthority(core, handle, grant, resources, pair, profile)
        self.checks = PersistedChecks(checks, journal, grant)
        self.service = MergeService(
            journal,
            self.authority,
            self.checks,
            publisher_factory=lambda boundary: publisher_factory(journal, profile, boundary),
        )
        self.registration = self.service.register(grant.binding)

    def publish(self, expected_version, key):
        return self.service.publish(
            self.registration["ref"], expected_version, key, self.grant.publication, self.check_ref
        )
