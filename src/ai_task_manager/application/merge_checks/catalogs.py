"""Trusted project bindings; public preparation never accepts catalog or runner JSON."""

from dataclasses import dataclass
from hashlib import sha256

from ai_task_manager.config.files import MAX_JSON_BYTES, FileSeal, strict_json
from ai_task_manager.domains.checks.catalog import Capabilities, validate_catalog
from ai_task_manager.domains.checks.models import content_hash, identifier
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


@dataclass(frozen=True)
class ProjectCatalog:
    project: str
    path: str
    expected_digest: str
    capabilities: Capabilities


class Catalogs:
    """Constructed once by trusted composition, not by task/model tool arguments."""

    def __init__(self, bindings: tuple[ProjectCatalog, ...]):
        if type(bindings) is not tuple or not bindings:
            raise ValidationError("Explicit configured project bindings are required")
        self._bindings = {}
        for binding in bindings:
            if type(binding) is not ProjectCatalog or binding.project in self._bindings:
                raise ValidationError("Project bindings must be unique typed values")
            identifier(binding.project)
            content_hash(binding.expected_digest)
            seal = FileSeal(binding.path)
            if seal.sha256 != binding.expected_digest:
                raise AccessDenied("Project configuration does not match its approved digest")
            with seal.path.open("rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
            if sha256(raw).hexdigest() != binding.expected_digest:
                raise AccessDenied("Configuration changed while binding project checks")
            catalog = validate_catalog(strict_json(raw), seal.sha256, binding.capabilities)
            self._bindings[binding.project] = (seal, catalog)

    def for_project(self, project):
        try:
            seal, catalog = self._bindings[project]
        except KeyError:
            raise AccessDenied("Project has no configured check capabilities") from None
        seal.check()
        return catalog

    def verify(self, plan):
        catalog = self.for_project(plan.scope.project)
        if catalog.digest != plan.catalog_digest or catalog.binding_digest != plan.binding_digest:
            raise AccessDenied("Plan capabilities or configured check policy changed")
        for group in plan.groups:
            if group != catalog.resolve(group.key):
                raise AccessDenied("Plan check requirements differ from trusted configuration")
        return catalog
