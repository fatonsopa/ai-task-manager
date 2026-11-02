"""Select once from complete change and dependency observations."""

from dataclasses import asdict

from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError

from .models import Availability, Plan, content_hash, fingerprint, identifier, repo_path


def select(catalog, scope, attempt, source_digest, change, discovery, *, prerequisites=()):
    scope.validate()
    identifier(attempt)
    content_hash(source_digest)
    content_hash(discovery.producer_digest)
    if (
        type(discovery.units) is not tuple
        or type(discovery.known_paths) is not tuple
        or type(change.paths) is not tuple
    ):
        raise ValidationError("Discovery and change collections must be immutable")
    for value in (change.base, change.head):
        if (
            type(value) is not str
            or len(value) != 40
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValidationError("Immutable base and head Git identities are required")
    if change.status != Availability.KNOWN or discovery.status != Availability.KNOWN:
        raise EvidenceRequired("Change or discovery is unavailable; selection is not empty")
    changed = set(change.paths)
    if changed and not discovery.units:
        raise EvidenceRequired("A nonempty change requires a complete execution inventory")
    known = set(discovery.known_paths)
    for path in changed | known:
        repo_path(path)
    if len(discovery.units) > 10000:
        raise ValidationError("Discovery exceeds the unit limit")
    names, selected, groups, reasons, all_units = set(), [], {}, [], []
    unknown_change = bool(changed - known)
    for unit in discovery.units:
        unit.validate()
        if unit.name in names:
            raise ValidationError("Discovered units must be unique")
        names.add(unit.name)
        group = catalog.resolve(unit.group)
        if not unit.reusable:
            raise EvidenceRequired("Required input facts are unavailable")
        if group.database != "none":
            db = {f.name: f.value for f in unit.inputs if f.kind == "database"}
            if not {"schema", "seed", "history"} <= db.keys() or any(
                db[k] in {None, "none", "absent"} for k in ("schema", "seed", "history")
            ):
                raise EvidenceRequired("Database checks require schema, seed and history facts")
        groups[group.key] = group
        resolved = type(unit)(unit.name, group.key, unit.inputs, unit.mandatory)
        all_units.append(resolved)
        source_paths = {f.name for f in unit.inputs if f.kind in {"source", "test", "config"}}
        if unit.mandatory or unknown_change or changed.intersection(source_paths):
            selected.append(resolved)
        else:
            reasons.append((unit.name, "No changed declared input"))
    edges = {}
    for label, parents in prerequisites:
        child = catalog.resolve(label).key
        if child in edges:
            raise ValidationError("Duplicate group prerequisite declaration")
        edges[child] = tuple(catalog.resolve(p).key for p in parents)
        if child not in groups or any(p not in groups for p in edges[child]):
            raise ValidationError("Prerequisites must be present in discovery")
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise ValidationError("Cyclic group prerequisites")
        if key in visited:
            return
        visiting.add(key)
        for parent in edges.get(key, ()):
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in groups:
        visit(key)
    needed = {u.group for u in selected}
    while True:
        expanded = needed | {p for key in needed for p in edges.get(key, ())}
        if expanded == needed:
            break
        needed = expanded
    prerequisite_groups = {p for key in needed for p in edges.get(key, ())}
    selected_names = {u.name for u in selected}
    for unit in all_units:
        if unit.group in prerequisite_groups and unit.name not in selected_names:
            selected.append(unit)
            selected_names.add(unit.name)
    reasons = [(name, why) for name, why in reasons if name not in selected_names]
    # Preserve catalog order, then apply a stable topological ordering.
    ordered = []
    while len(ordered) < len(groups):
        for group in catalog.groups:
            if (
                group.key in groups
                and group.key not in ordered
                and all(p in ordered for p in edges.get(group.key, ()))
            ):
                ordered.append(group.key)
    return Plan(
        scope,
        attempt,
        change.base,
        change.head,
        source_digest,
        catalog.digest,
        discovery.producer_digest,
        tuple(selected),
        tuple(groups[k] for k in ordered),
        tuple((k, edges[k]) for k in ordered if k in edges),
        not unknown_change,
        tuple(reasons),
        tuple(sorted(changed)),
        fingerprint(asdict(discovery)),
        len(discovery.units),
        "known_selected" if selected else "known_empty",
    )
