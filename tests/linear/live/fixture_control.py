"""Exact owned-fixture control ports; no HTTP, key reader or live implementation."""

from dataclasses import dataclass

from .ledger import DriverRefused, uuid


@dataclass(frozen=True)
class Observation:
    kind: str
    id: str
    workspace: str
    team: str
    project: str
    owner: str
    marker: str
    state: str = "active"


class RejectProvider:
    def create(self, *args):
        raise DriverRefused("provider_not_admitted")

    read = create
    cleanup = create


class FixtureControl:
    """Injected ports in this phase are authored local fixtures, never live receipts.

    Each port call is one primitive request and is charged before delegation.
    Product transport requests use BudgetedTransport separately. Neither port
    can create writing approvals, human acceptance, workers or AI/user receipts.
    """

    def __init__(self, ledger, provider=None):
        self.ledger, self.provider = ledger, provider or RejectProvider()

    def _bound(self, observation, kind, candidate):
        expected = self.ledger.scope
        if not isinstance(observation, Observation) or (
            observation.kind,
            observation.id,
            observation.workspace,
            observation.team,
            observation.project,
            observation.owner,
            observation.marker,
        ) != (
            kind,
            candidate,
            expected.workspace,
            expected.team,
            expected.project,
            expected.owner,
            self.ledger.run_id,
        ):
            raise DriverRefused("fixture_readback_mismatch")
        uuid(observation.id)

    def create(self, key, kind, candidate, content_digest):
        if isinstance(self.provider, RejectProvider):
            raise DriverRefused("provider_not_admitted")
        if kind == "project" and candidate != self.ledger.scope.project:
            raise DriverRefused("fixture_project_identity_changed")
        row = self.ledger.begin(key, kind, candidate, content_digest)
        if row["state"] == "confirmed":
            return self.ledger.resource(candidate)
        if row["state"] != "intent":
            raise DriverRefused("uncertain_create_needs_readback")
        self.ledger.request("create")
        self.ledger.mark_sent(key)
        try:
            observation = self.provider.create(
                kind, candidate, self.ledger.scope, self.ledger.run_id, content_digest
            )
            self._bound(observation, kind, candidate)
            self.ledger.confirm(key, observation.id, kind)
            return self.ledger.resource(candidate)
        except Exception:
            self.ledger.uncertain(key)
            raise DriverRefused("create_unconfirmed") from None

    def recover(self, key):
        if isinstance(self.provider, RejectProvider):
            raise DriverRefused("provider_not_admitted")
        row = self.ledger.get(key)
        if row["state"] == "confirmed":
            return self.ledger.resource(row["candidate"])
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("no_possible_effect")
        self.ledger.request("read")
        try:
            observation = self.provider.read(row["kind"], row["candidate"])
            self._bound(observation, row["kind"], row["candidate"])
            self.ledger.confirm(key, observation.id, observation.kind)
            return self.ledger.resource(observation.id)
        except Exception:
            raise DriverRefused("recovery_unconfirmed") from None

    def cleanup(self, resource_id, action):
        if isinstance(self.provider, RejectProvider):
            raise DriverRefused("provider_not_admitted")
        row = self.ledger.resource(uuid(resource_id))
        allowed = {
            "project": {"archive"},
            "issue": {"cancel", "archive"},
            "webhook": {"disable", "delete"},
            "comment": set(),
        }
        if action not in allowed[row["kind"]]:
            raise DriverRefused("cleanup_action_forbidden")
        if row["kind"] == "project":
            self.ledger.require_closed_children()
        if row["state"] in {"archived", "canceled", "deleted"}:
            return row
        self.ledger.request("read")
        try:
            before = self.provider.read(row["kind"], resource_id)
            self._bound(before, row["kind"], resource_id)
            self.ledger.request("cleanup")
            after = self.provider.cleanup(row["kind"], resource_id, action)
            self._bound(after, row["kind"], resource_id)
            state = {
                "archive": "archived",
                "cancel": "canceled",
                "disable": "disabled",
                "delete": "deleted",
            }[action]
            if after.state != state:
                raise DriverRefused("cleanup_readback_mismatch")
            self.ledger.cleaned(resource_id, state)
            return self.ledger.resource(resource_id)
        except Exception:
            raise DriverRefused("cleanup_unconfirmed") from None
