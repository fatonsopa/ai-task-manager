"""Sealed selection and resource drift controls independent of semantic model acceptance."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows.code_delivery import (
    ResourceExecutor,
    ResourceReads,
    ResourceReviews,
)
from ai_task_manager.installation.workflows.code_registration import CodeRegistration
from ai_task_manager.installation.workflows.registry import NativeRegistry
from tests.domains.helpers import Rig


def test_sealed_code_registration_refuses_scope_policy_and_port_replacement(tmp_path):
    tmp_path.chmod(0o700)
    r = Rig(tmp_path / "core.sqlite")
    task = r.task()
    path = tmp_path / "policy.json"
    data = {
        "reference": "code",
        "scope": asdict(r.scope),
        "task_id": task.id,
        "prose_digest": task.prose_digest,
        "target_ref": "refs/heads/integration",
        "require_human_acceptance": False,
    }
    write_private(path, data)
    registration = CodeRegistration(path, lambda context: None)
    registry = NativeRegistry(code_registrations=(registration,))
    assert registry.require_code("code") is registration
    registration.require(r.core, r.handle, task)
    with pytest.raises(AccessDenied):
        registration.require(r.core, r.handle, replace(task, code_bearing=False))
    foreign = r.session(replace(r.scope, logical_session="foreign"))
    with pytest.raises(AccessDenied):
        registration.require(r.core, foreign, task)
    registration.prepare = lambda context: "replacement"
    with pytest.raises(AccessDenied):
        registration.check()
    with pytest.raises(AccessDenied):
        NativeRegistry(code_registrations=(registration, registration))
    extra = tmp_path / "extra.json"
    write_private(extra, dict(data, passed=True))
    with pytest.raises(AccessDenied):
        CodeRegistration(extra, lambda context: None)


@pytest.mark.parametrize("consumer", ["review", "read", "proof", "execute"])
def test_waiting_consumer_revalidates_before_returning_evidence(consumer):
    class Resources:
        valid = True
        calls = 0

        def verify(self, pair):
            self.calls += 1
            if not self.valid:
                raise EvidenceRequired("Authored resource replacement while waiting")

    resources, pair = Resources(), object()

    def wait(*args, **kwargs):
        resources.valid = False
        return "unusable-evidence"

    source = SimpleNamespace(
        resolve=wait,
        execute=wait,
        store=object(),
        ensure_parent=lambda: None,
        read=wait,
        proofs=wait,
    )
    with pytest.raises(EvidenceRequired, match="replacement"):
        if consumer == "review":
            ResourceReviews(source, resources, pair).resolve(object())
        elif consumer == "execute":
            ResourceExecutor(source, resources, pair).execute(object(), "group", ("unit",))
        else:
            reads = ResourceReads(source, resources, pair)
            method = reads.read if consumer == "read" else reads.proofs
            method(object(), object(), after_wait=lambda: None)
    assert resources.calls >= 2


def test_resource_reads_preserve_original_finite_wait_recheck_callback():
    order = []
    resources = SimpleNamespace(verify=lambda pair: order.append("resource"))

    class Reads:
        store = object()

        def read(self, *args, after_wait):
            order.append("wait")
            after_wait()
            return "record"

    wrapper = ResourceReads(Reads(), resources, object())
    assert wrapper.read("ref", "scope", after_wait=lambda: order.append("source")) == "record"
    assert order == ["resource", "wait", "resource", "source", "resource", "resource"]
