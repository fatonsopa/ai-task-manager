"""Exact owned-fixture control ports; no HTTP, key reader or live implementation."""

from dataclasses import asdict, dataclass

from ai_task_manager.config.files import digest

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

    def __init__(self, ledger, provider=None, *, quiescence=None):
        self.ledger, self.provider = ledger, provider or RejectProvider()
        # Trusted in-process composition only. An unattached authored setup port
        # has no runtime handles and cannot establish live receiver/tunnel readiness.
        self.quiescence = quiescence

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

    def cleanup(self, resource_id, action, *, operation_key=None):
        if isinstance(self.provider, RejectProvider):
            raise DriverRefused("provider_not_admitted")
        row = self.ledger.resource(uuid(resource_id))
        key = operation_key or f"cleanup:{row['kind']}:{resource_id}:{action}"
        # A committed replay is historical and performs no new provider effect.
        try:
            prior = self.ledger.cleanup_action(key)
        except DriverRefused as error:
            if str(error) != "cleanup_intent_missing":
                raise
        else:
            checked = self.ledger.begin_cleanup(key, resource_id, action)
            if checked["state"] == "confirmed":
                return {**row, "state": checked["target"]}
            if prior["state"] in {"sent", "uncertain"}:
                return self.recover_cleanup(key)
        if row["kind"] == "project":
            if self.quiescence is not None:
                self.quiescence()
            self.ledger.require_closed_children(exclude_cleanup_key=key)
        self.ledger.begin_cleanup(key, resource_id, action)
        self.ledger.cleanup_read(key)
        try:
            before = self.provider.read(row["kind"], resource_id)
            self._bound(before, row["kind"], resource_id)
            self.ledger.cleanup_prepared(key, digest(asdict(before)))
            self.ledger.cleanup_sent(key)
        except Exception:
            raise DriverRefused("cleanup_unconfirmed") from None
        try:
            after = self.provider.cleanup(row["kind"], resource_id, action)
            self._bound(after, row["kind"], resource_id)
            if after.state != self.ledger.cleanup_action(key)["target"]:
                raise DriverRefused("cleanup_readback_mismatch")
            self.ledger.cleanup_confirm(key, digest(asdict(after)))
            return self.ledger.resource(resource_id)
        except Exception:
            self.ledger.cleanup_uncertain(key)
            raise DriverRefused("cleanup_unconfirmed") from None

    def recover_cleanup(self, operation_key):
        if isinstance(self.provider, RejectProvider):
            raise DriverRefused("provider_not_admitted")
        row = self.ledger.cleanup_action(operation_key)
        if row["state"] == "confirmed":
            return {**self.ledger.resource(row["resource_id"]), "state": row["target"]}
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("cleanup_no_possible_effect")
        if row["kind"] == "project":
            if self.quiescence is not None:
                self.quiescence()
            self.ledger.require_closed_children(exclude_cleanup_key=operation_key)
        self.ledger.cleanup_read(operation_key)
        try:
            current = self.provider.read(row["kind"], row["resource_id"])
            self._bound(current, row["kind"], row["resource_id"])
            if current.state != row["target"]:
                raise DriverRefused("cleanup_observation_incomplete")
            self.ledger.cleanup_confirm(operation_key, digest(asdict(current)))
            return self.ledger.resource(row["resource_id"])
        except Exception:
            raise DriverRefused("cleanup_recovery_unconfirmed") from None
