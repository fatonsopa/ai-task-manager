"""Trusted composition validates exact bindings before any registration write."""

from dataclasses import asdict

from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

from .contracts import Binding


class Registrations:
    def __init__(self, journal, authority):
        self.journal, self.authority = journal, authority

    def register(self, binding):
        if type(binding) is not Binding:
            raise ValidationError("An exact immutable binding is required")
        binding.validate()
        if self.authority is None:
            raise AccessDenied("Trusted registration authority is unavailable")
        self.authority.registration(binding)
        return self.journal.register(asdict(binding), binding.fingerprint)

    def resolve(self, operation_ref, binding):
        binding.validate()
        operation = self.journal.current(operation_ref)
        if operation["binding"] != asdict(binding):
            raise AccessDenied("Registered operation differs from the requested binding")
        return operation
