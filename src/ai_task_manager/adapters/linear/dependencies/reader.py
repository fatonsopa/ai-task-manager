"""Complete metadata-first directional scans; no task-state writes or proof issuance."""

import hashlib
import json
import time
from dataclasses import asdict, dataclass

from ai_task_manager.adapters.linear.client import IDENTITY, PROJECT_TEAMS, STATES, LinearClient
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.existing import ExistingReader
from ai_task_manager.adapters.linear.models import canonical_id, revision
from ai_task_manager.domains.scheduling.policy import check_cycles
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

from .schema import DETAILS, METADATA, RELATIONS, SCHEMA, DependencySchema


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Observation:
    complete: bool
    priority: str
    predecessors: tuple[str, ...]
    schema_hash: str | None
    raw: dict


class _BudgetTransport:
    def __init__(self, transport):
        self.transport, self.deadline, self.requests = transport, time.monotonic() + 20, 0
        self.response, self.recovered = None, False
        self.entered, self.failures = False, []

    def send(self, query, variables, credentials):
        if self.requests >= 2048 or time.monotonic() >= self.deadline:
            raise LinearError("dependency_observation_limit")
        self.requests += 1
        self.entered = True
        result = self.transport.send(query, variables, credentials)
        self.response = result
        if time.monotonic() >= self.deadline:
            raise LinearError("dependency_observation_limit")
        return result


class _ReadClient(LinearClient):
    _queries = frozenset(
        (IDENTITY, PROJECT_TEAMS, STATES, SCHEMA, METADATA, DETAILS, *RELATIONS.values())
    )

    def request(self, query, variables, *, mutation=False, **kwargs):
        self.transport.response = None
        self.transport.entered = False
        started = time.monotonic()
        try:
            return super().request(query, variables, mutation=mutation, **kwargs)
        except LinearError as error:
            response = self.transport.response
            recover = (
                not mutation and query in self._queries and not self.transport.recovered
                and error.retry_after is None
                and (
                    (error.category == "transport_failure" and response is None)
                    or (error.category == "http_failure" and response is not None
                        and response.status == 503
                        and not any(key.lower() == "retry-after" for key in response.headers))
                )
            )
            self._failure(query, variables, error, started, recover)
            if not recover:
                raise
            # Both failure categories share one recovery and the original budget.
            self.transport.recovered = True
            self.transport.response = None
            self.transport.entered = False
            started = time.monotonic()
            try:
                return super().request(query, variables, **kwargs)
            except LinearError as recovery_error:
                self._failure(query, variables, recovery_error, started, False)
                raise

    def _failure(self, query, variables, error, started, recovery_selected):
        finished, chain, cause = time.monotonic(), [], error
        while cause is not None and len(chain) < 4:
            fact = {"module": type(cause).__module__, "type": type(cause).__name__}
            if isinstance(cause, OSError) and type(cause.errno) is int:
                fact["errno"] = cause.errno
            chain.append(fact)
            cause = cause.__cause__
        response = self.transport.response
        self.transport.failures.append({
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "variables_sha256": digest(variables),
            "category": error.category,
            "exception_chain": chain,
            "started_monotonic": started,
            "finished_monotonic": finished,
            "elapsed_seconds": finished - started,
            "transport_entered": self.transport.entered,
            "response_returned": response is not None,
            "response_status": response.status if response is not None else None,
            "provider_receipt": "unknown",
            "recovery_selected": recovery_selected,
        })


class DependencyReader:
    def __init__(self, client, scope, *, max_nodes=128, max_relations=2048):
        if (
            type(max_nodes) is not int
            or not 1 <= max_nodes <= 256
            or type(max_relations) is not int
            or not 1 <= max_relations <= 4096
        ):
            raise ValueError("Finite dependency graph limits are required")
        self.client, self.scope = client, scope
        self.expected = client.expected
        self.max_nodes, self.max_relations = max_nodes, max_relations
        self._check_scope()

    def _check_scope(self):
        e = self.expected
        if self.client.expected != e or (
            self.scope.provider,
            self.scope.workspace,
            self.scope.principal,
            self.scope.project,
        ) != ("linear", e.workspace_id, e.viewer_id, e.project_id):
            raise AccessDenied("Dependency reader belongs to another provider scope")

    def metadata(self, raw, issue):
        # Reuse the established metadata-first ownership/state validator.
        required = {
            "id",
            "identifier",
            "createdAt",
            "updatedAt",
            "archivedAt",
            "trashed",
            "priority",
            "assignee",
            "team",
            "project",
            "state",
        }
        if not isinstance(raw, dict) or not required <= raw.keys():
            raise LinearError("incomplete_dependency_metadata")
        meta = ExistingReader.metadata(self, raw)
        if meta["issue_id"] != issue:
            raise LinearError("dependency_issue_mismatch")
        priority = raw.get("priority")
        if type(priority) not in (int, float) or priority not in (0, 1, 2, 3, 4):
            raise LinearError("unsupported_dependency_priority")
        return dict(
            meta, priority={0: "none", 1: "urgent", 2: "high", 3: "normal", 4: "low"}[priority]
        )

    def _read(self, query, issue, **variables):
        raw = self.client.request(query, {"id": issue, **variables}).get("issue")
        return self.metadata(raw, issue), raw

    def _scan(self, task, local):
        pending, seen, graph, relations, nodes = [task.issue_id], set(), {}, {}, {}
        placements = {}
        while pending:
            issue = pending.pop()
            if issue in seen:
                continue
            if len(seen) >= self.max_nodes:
                raise LinearError("dependency_node_limit")
            seen.add(issue)
            current = local.get(issue)
            if current is None:
                raise LinearError("dependency_predecessor_unresolved")
            meta, _ = self._read(METADATA, issue)
            prose_meta, prose = self._read(DETAILS, issue)
            if (
                meta != prose_meta
                or prose.get("title") != current.title
                or prose.get("description") != current.body
            ):
                raise LinearError("dependency_prose_or_revision_changed")
            if current.sync_state != "confirmed" or current.observed_status != meta["status"]:
                raise LinearError("dependency_local_provider_status_mismatch")
            nodes[issue] = dict(meta, prose_digest=current.prose_digest)
            predecessors = set()
            for direction, query in RELATIONS.items():
                after, cursors, page_ids = None, set(), set()
                for page_number in range(self.client.max_pages):
                    if page_number == 0:
                        # The owned prose response also contains both first pages.
                        raw = prose
                    else:
                        page_meta, raw = self._read(query, issue, after=after)
                        if page_meta != meta:
                            raise LinearError("dependency_revision_changed")
                    try:
                        connection = raw[direction]
                        page, items = connection["pageInfo"], connection["nodes"]
                        if type(page["hasNextPage"]) is not bool or not isinstance(items, list):
                            raise ValueError()
                        for item in items:
                            rid = canonical_id(item["id"])
                            source = canonical_id(item["issue"]["id"])
                            target = canonical_id(item["relatedIssue"]["id"])
                            if (
                                rid in page_ids
                                or source == target
                                or (source if direction == "relations" else target) != issue
                            ):
                                raise ValueError()
                            page_ids.add(rid)
                            row = {
                                "id": rid,
                                "source": source,
                                "target": target,
                                "type": item["type"],
                                "revision": revision(item["updatedAt"]),
                                "archived_at": revision(item["archivedAt"])
                                if item["archivedAt"] is not None
                                else None,
                            }
                            if row["type"] not in {"blocks", "related", "similar"}:
                                raise LinearError("unsupported_dependency_relation")
                            if rid in relations and relations[rid] != row:
                                raise LinearError("dependency_relation_changed")
                            relations[rid] = row
                            placements.setdefault(rid, set()).add((issue, direction))
                            if len(relations) > self.max_relations:
                                raise LinearError("dependency_relation_limit")
                            if (
                                direction == "inverseRelations"
                                and row["type"] == "blocks"
                                and not row["archived_at"]
                            ):
                                predecessors.add(source)
                        if not page["hasNextPage"]:
                            break
                        after = page["endCursor"]
                        if (
                            not isinstance(after, str)
                            or not after
                            or len(after) > 4096
                            or after in cursors
                        ):
                            raise ValueError()
                        cursors.add(after)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LinearError("incomplete_dependency_pagination") from exc
                else:
                    raise LinearError("dependency_pagination_limit")
            ending, _ = self._read(METADATA, issue)
            if ending != meta:
                raise LinearError("dependency_revision_changed")
            graph[issue] = tuple(sorted(predecessors))
            # Foreign/unclaimed issues are never read for prose or graph detail.
            if any(predecessor not in local for predecessor in predecessors):
                raise LinearError("dependency_predecessor_unresolved")
            pending.extend(predecessors - seen)
        for rid, relation in relations.items():
            if relation["source"] in nodes and relation["target"] in nodes:
                if (
                    not {
                        (relation["source"], "relations"),
                        (relation["target"], "inverseRelations"),
                    }
                    <= placements[rid]
                ):
                    raise LinearError("incomplete_dependency_direction")
        try:
            check_cycles(graph)
        except ValidationError as exc:
            raise LinearError("dependency_cycle") from exc
        return {"nodes": nodes, "relations": relations, "graph": graph}

    def observe(self, task, local):
        schema_hash = None
        try:
            self._check_scope()
            original = self.client
            self.client = _ReadClient(
                _BudgetTransport(original.transport),
                original.credentials,
                self.expected,
                max_pages=min(original.max_pages, 100),
            )
            initial_binding = self.client.verify_binding()
            schema_hash = DependencySchema.verify(self.client.request(SCHEMA, {})).sha256
            first = self._scan(task, local)
            second = self._scan(task, local)
            if digest(first) != digest(second):
                raise LinearError("dependency_graph_changed_between_scans")
            if self.client.verify_binding() != initial_binding:
                raise LinearError("dependency_binding_changed")
            self._check_scope()
            raw = {
                "first": first,
                "second_digest": digest(second),
                "binding": asdict(self.expected),
                "semantics": (
                    "blocks source -> target; "
                    "authored direction acceptance pending live verification"
                ),
            }
            if self.client.transport.failures:
                raw["read_failures"] = self.client.transport.failures
            return Observation(
                True,
                first["nodes"][task.issue_id]["priority"],
                tuple(first["graph"][task.issue_id]),
                schema_hash,
                raw,
            )
        except (LinearError, AccessDenied, ValidationError) as error:
            raw = {"unavailable": error.category if isinstance(error, LinearError)
                   else type(error).__name__}
            if isinstance(self.client, _ReadClient) and self.client.transport.failures:
                raw["read_failures"] = self.client.transport.failures
            return Observation(
                False,
                "normal",
                (),
                schema_hash,
                raw,
            )
