"""One metadata-only provider preflight; this never admits remote effects."""

import hashlib
import json
import threading
import time
from dataclasses import dataclass

from .fixture_graphql import OPERATIONS, SHAPE, FixtureGraphQL
from .ledger import DriverRefused


class _ReadOnlyTransport:
    def __init__(self, transport, team):
        self.transport = transport
        self.sequence = ((SHAPE, {}), (OPERATIONS["identity"], {"team": team}))
        self.position = 0
        self.facts = []
        self.identity = None
        self.lock = threading.Lock()

    def send(self, query, variables, credentials):
        with self.lock:
            if (
                self.position >= len(self.sequence)
                or (query, variables) != self.sequence[self.position]
            ):
                raise DriverRefused("read_only_preflight_sequence_required")
            self.position += 1
        started = time.monotonic()
        wire = self.transport.send(query, variables, credentials)
        self.facts.append(
            {
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "status": wire.status,
                "body_sha256": hashlib.sha256(wire.body).hexdigest(),
                "started_at_monotonic": started,
                "completed_at_monotonic": time.monotonic(),
            }
        )
        if query == OPERATIONS["identity"]:
            try:
                self.identity = json.loads(wire.body)["data"]
            except (ValueError, KeyError, TypeError):
                # The existing client owns error parsing and its safe category.
                pass
        return wire


@dataclass(frozen=True)
class ReadOnlyPreflightResult:
    run_id: str
    schema_sha256: str
    checked_at: float
    actual_identity: dict
    requests: tuple[dict, ...]


class ReadOnlyPreflight:
    def __init__(self, ledger, transport, credentials, *, team_key="TST", clock=time.time):
        self._graphql = FixtureGraphQL(
            ledger, transport, credentials, team_key=team_key, clock=clock
        )
        # The normal transport composition/clamping stays with FixtureGraphQL.
        # This outer guard restricts this particular instance to two reads.
        self._transport = _ReadOnlyTransport(self._graphql.client.transport, ledger.scope.team)
        self._graphql.client.transport = self._transport
        self._used = False
        self._lock = threading.Lock()

    def run(self):
        with self._lock:
            if self._used:
                raise DriverRefused("read_only_preflight_already_attempted")
            self._used = True
        result = self._graphql.preflight()
        identity = self._transport.identity
        if len(self._transport.facts) != 2 or not isinstance(identity, dict):
            raise DriverRefused("read_only_preflight_observation_missing")
        # Only the selected, actually verified fields cross the result boundary.
        actual = {
            "viewer": {key: identity["viewer"][key] for key in ("id", "admin")},
            "organization": {"id": identity["organization"]["id"]},
            "team": {key: identity["team"][key] for key in ("id", "key")},
        }
        return ReadOnlyPreflightResult(
            self._graphql.ledger.run_id,
            result.schema_sha256,
            result.checked_at,
            actual,
            tuple(dict(fact) for fact in self._transport.facts),
        )
