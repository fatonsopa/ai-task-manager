"""Explicit trusted-port controls; their observations are authored fixture evidence."""

from contextlib import contextmanager
from dataclasses import asdict

from ai_task_manager.application.merging.service import MergeService
from ai_task_manager.domains.merging.contracts import Binding, Observation, Publication, encode
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.merging.journal import Journal


class Authority:
    def __init__(self, binding):
        self.allowed, self.active = binding, True
        self.entries = 0

    def registration(self, binding):
        if binding != self.allowed:
            raise AccessDenied("Unknown scoped fixture binding")

    def before_start(self, binding):
        self.check_current(binding)

    def check_current(self, binding):
        self.registration(binding)
        if not self.active:
            raise AccessDenied("Fixture authority is revoked")

    @contextmanager
    def authority_boundary(self, binding):
        self.check_current(binding)
        self.entries += 1
        yield


class CheckPort:
    def __init__(self):
        self.fact = None

    def read(self, ref, binding):
        if self.fact is None:
            raise AccessDenied("No actual fixture observation")
        return self.fact


class Harness:
    def __init__(self, path):
        self.journal = Journal(path)
        self.binding = Binding(
            "task",
            "attempt",
            "principal",
            "session",
            "project",
            "a" * 64,
            "b" * 64,
            "tree",
            "repository",
            "resource",
            "c" * 64,
            "operation",
        )
        self.authority, self.checks = Authority(self.binding), CheckPort()
        self.service = MergeService(self.journal, self.authority, self.checks)
        self.registration = self.service.register(self.binding)
        self.ref = self.registration["ref"]
        self.request = Publication("refs/heads/worker", "d" * 40, "e" * 40, "e" * 40, "range")
        self.checks.fact = Observation(
            self.ref,
            self.binding.fingerprint,
            "checked",
            "checks",
            encode(
                {
                    "head": self.request.new,
                    "permitted": True,
                    "contract_digest": "f" * 64,
                    "source_digest": self.binding.source_digest,
                }
            ),
        )

    def prepare(self):
        return self.service.prepare(self.ref, 0, "prepare", self.request, "checked")

    def snapshot(self):
        return self.journal.current(self.ref), self.journal.events(self.ref)

    def binding_dict(self):
        return asdict(self.binding)
