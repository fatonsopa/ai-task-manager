"""Public run/resume over installed merge intent and existing durable component ports."""

import time
from dataclasses import asdict, replace

from ai_task_manager.adapters.git.publication import LocalGit
from ai_task_manager.application.merging.authority import CoreAuthority, LeaseGrant
from ai_task_manager.application.merging.composition import CheckedMerge
from ai_task_manager.application.publication_fences import (
    PublicationContext,
    PublicationIntentUnavailable,
)
from ai_task_manager.application.service.models import result
from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.domains.checks.models import Scope
from ai_task_manager.domains.merging.candidates import CandidateResult
from ai_task_manager.domains.merging.contracts import Publication
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Cause, IssueStatus

from .store import MergeWorkflowStore


class _CandidatePublicationAuthority(CoreAuthority):
    def __init__(self, *args, candidate_store, candidate_ref, candidate_profile, source_grant):
        super().__init__(*args)
        self.candidate_store, self.candidate_ref = candidate_store, candidate_ref
        self.candidate_profile, self.source_grant = candidate_profile, source_grant

    def check_current(self, binding):
        self.candidate_store.owner(self.candidate_ref)
        if self.candidate_profile.refs(self.source_grant) != (
            self.source_grant.source_head,
            self.source_grant.target_head,
        ):
            raise AccessDenied("Completed worker source or configured target changed")
        super().check_current(binding)


class MergeWorkflowService(WorkflowService):
    """Installed single-plan code handler compatible with the retained WorkflowOwner."""

    def __init__(
        self,
        core,
        handle,
        plan,
        candidate,
        checks,
        provider,
        evidence,
        *,
        publisher_factory=LocalGit,
        clock=time.time,
        faults=None,
    ):
        self.core, self.handle, self.plan = core, handle, plan
        self.candidate, self.check_inputs, self.provider, self.evidence = (
            candidate,
            checks,
            provider,
            evidence,
        )
        self.clock, self.faults, self.publisher_factory = clock, faults, publisher_factory
        self.store = MergeWorkflowStore(candidate.store.journal)
        self.session_id, self.scope = core.session_binding(handle)
        self.references, self.consumers, self.timings = set(), {}, []

    def check_owner_deadline(self, deadline):
        if self.plan.deadline > deadline:
            raise AccessDenied("Installed merge plan outlives the current owner admission")

    def _scope(self):
        if self.core.session_binding(self.handle) != (self.session_id, self.scope):
            raise AccessDenied("Merge controller session binding changed")
        b = self.plan.grant.binding
        if (b.principal_id, b.project_id, b.session_id) != (
            self.scope.principal,
            self.scope.project,
            self.scope.logical_session,
        ):
            raise AccessDenied("Installed merge plan belongs to another scope")
        return self.core.get_task(self.handle, b.task_id)

    def _ready(self):
        task = self._scope()
        self.candidate.authority.check_current(self.plan.grant)
        if not set(task.required_work) <= set(task.completed_work):
            raise EvidenceRequired("Verified semantic completion of all issue work is required")
        if self.plan.require_human_acceptance:
            decision = self.core.context.proof(
                self.plan.grant.decision_ref, "decision", self.scope, task.id
            )
            if decision.payload.get("actor") != "human":
                raise EvidenceRequired("Configured explicit human acceptance is required")
        if self.clock() >= self.plan.deadline:
            raise AccessDenied("Original merge workflow deadline expired")
        return task

    def run(self, task_id, plan_ref, operation_key):
        self._scope()
        if (task_id, plan_ref) != (self.plan.grant.binding.task_id, self.plan.id):
            raise AccessDenied("The installed merge plan is not assigned to this task")
        ref = self.store.create(self.plan, self.candidate.profile, operation_key)
        return self.resume(ref)

    def _step(self, ref, owner, name, payload, action):
        row = self.store.read(ref)
        prior = row["steps"].get(name)
        if prior is not None and prior["payload"] != payload:
            raise AccessDenied("Persisted merge step context differs")
        if prior is not None and prior["result"] is not None:
            if name != "publication" or prior["result"]["publication"] == "committed":
                return prior["result"]
        self.store.intend(ref, owner, name, payload)
        started = time.monotonic()
        observed = action()
        self.timings.append({"stage": name, "seconds": time.monotonic() - started})
        if self.faults is not None:
            self.faults.hit("after_" + name)
        self.store.finish(ref, owner, name, observed)
        return observed

    def _publication(self, ref, candidate, check_ref, publication_context=None):
        grant = self.plan.grant
        binding = replace(grant.binding, operation_key=ref + ":publication")
        request = Publication(
            candidate.candidate_ref,
            grant.target_head,
            candidate.commit,
            candidate.commit,
            ref + ":range",
        )
        lease = LeaseGrant(
            binding,
            request,
            grant.task_version,
            grant.attempt_version,
            grant.decision_revision,
            grant.decision_ref,
            publication_context,
        )
        authority = self.candidate.authority
        consumer = CheckedMerge(
            self.core,
            self.handle,
            lease,
            authority.resources,
            authority.pair,
            self.candidate.profile.profile,
            self.store.journal,
            self.check_inputs.checks,
            check_ref,
            publisher_factory=self.publisher_factory,
        )
        consumer.authority = _CandidatePublicationAuthority(
            self.core,
            self.handle,
            lease,
            authority.resources,
            authority.pair,
            self.candidate.profile.profile,
            candidate_store=self.candidate.store,
            candidate_ref=self._candidate_ref(ref),
            candidate_profile=self.candidate.profile,
            source_grant=self.plan.grant,
        )
        consumer.service.authority = consumer.authority
        self.consumers[ref] = consumer
        return consumer

    def _candidate_ref(self, ref):
        return self.store.read(ref)["steps"]["candidate"]["result"]["operation_ref"]

    def _envelope(self, ref, state, reason=None):
        body = {
            "workflow": ref,
            "task_id": self.plan.grant.binding.task_id,
            "state": state,
            "cleanup": "pending_independent_retirement",
        }
        if reason:
            body["reason"] = reason
        envelope = result(
            body,
            "confirmed" if state == "done" else "uncertain" if state == "uncertain" else "blocked",
        )
        if state != "done":
            envelope["exit_code"] = 6 if state == "uncertain" else 4
        return envelope

    def _delivery_event(self, task, intent):
        return any(
            event.task_id == task.id
            and event.kind == "transition_requested"
            and event.payload.get("decision_ref") == intent["payload"]["decision_ref"]
            and event.payload.get("evidence_refs", {}).get("merge")
            == intent["payload"]["merge_ref"]
            and event.payload.get("cause") == Cause.MERGE
            for event in self.core.notifications.list_events(self.handle)
        )

    def resume(self, workflow_ref):
        task = self._scope()
        row = self.store.read(workflow_ref)
        if (row["request"]["plan"], row["request"]["grant"]) != (
            self.plan.digest,
            asdict(self.plan.grant),
        ):
            raise AccessDenied("Merge workflow is not the current installed context")
        self.references.add(workflow_ref)
        owner = self.store.claim(workflow_ref)
        ref = workflow_ref
        try:
            steps = row["steps"]
            if (
                task.delivered_once
                and task.observed_status == IssueStatus.IN_DEV
                and task.sync_state == "confirmed"
                and "delivery" in steps
            ):
                if not self._delivery_event(task, steps["delivery"]):
                    raise EvidenceRequired("Delivered task has no exact owned merge transition")
                for name in ("delivery", "confirmation"):
                    prior = steps.get(name)
                    if prior is not None and prior["result"] is None:
                        self.store.finish(
                            ref, owner, name, {"observed": "In Dev", "reconciled": True}
                        )
                return self._envelope(ref, "done")
            delivered = steps.get("delivery")
            if delivered and task.desired_status == IssueStatus.IN_DEV:
                if not self._delivery_event(task, delivered):
                    raise EvidenceRequired("Pending delivery has no exact owned Core transition")
                if delivered["result"] is None:
                    self.store.finish(
                        ref, owner, "delivery", {"task_id": task.id, "desired": task.desired_status}
                    )
            else:
                fence_step = steps.get("publication_fence")
                if fence_step is None:
                    self._ready()
                    value = self._step(
                        ref,
                        owner,
                        "candidate",
                        {"grant": self.plan.digest},
                        lambda: asdict(self.candidate.prepare(self.plan.grant)),
                    )
                    candidate = CandidateResult(**value)
                    if candidate.status != "ready":
                        return self._envelope(
                            ref,
                            "uncertain" if candidate.status == "uncertain" else "blocked",
                            "candidate_" + candidate.status,
                        )
                    self.candidate.store.owner(candidate.operation_ref)
                    self.candidate.reconcile(candidate.operation_ref)
                    self._ready()
                    check_plan = self._step(
                        ref,
                        owner,
                        "check_plan",
                        {"candidate": candidate.commit},
                        lambda: {
                            "ref": self.check_inputs.prepare(
                                self.candidate.profile, self.plan.grant, candidate, ref + ":checks"
                            )
                        },
                    )
                    check_ref = check_plan["ref"]
                    b = self.plan.grant.binding
                    check_scope = Scope(b.principal_id, b.project_id, b.session_id, b.task_id)

                    def run_checks():
                        self._ready()
                        snap = self.check_inputs.checks.run(check_ref, check_scope)
                        return {
                            "ref": check_ref,
                            "outcome": snap["outcome"],
                            "plan": snap["plan"].digest,
                        }

                    checked = self._step(ref, owner, "checks", {"ref": check_ref}, run_checks)
                    if checked["outcome"] != "passed":
                        return self._envelope(ref, "blocked", "checks_" + checked["outcome"])
                    before = self._publication(ref, candidate, check_ref)
                    pub_ref = before.registration["ref"]

                    def prepare_intent():
                        before.service.prepare(
                            pub_ref,
                            self.store.journal.current(pub_ref)["version"],
                            ref + ":prepare-publication",
                            before.grant.publication,
                            check_ref,
                        )
                        return {
                            "ref": self.evidence.prepare_intent(
                                self.core, self.handle, self.plan, candidate, before, check_ref
                            )
                        }

                    intent = self._step(
                        ref,
                        owner,
                        "publication_intent",
                        {"ref": pub_ref, "checks": check_ref},
                        prepare_intent,
                    )
                else:
                    candidate = CandidateResult(**steps["candidate"]["result"])
                    check_ref = steps["check_plan"]["result"]["ref"]
                    intent = steps["publication_intent"]["result"]
                g = self.plan.grant

                def begin_context():
                    args = (
                        self.handle,
                        g.binding.task_id,
                        g.binding.attempt_id,
                        g.task_version,
                        g.attempt_version,
                        g.decision_revision,
                        g.decision_ref,
                        intent["ref"],
                        ref + ":fence",
                    )
                    try:
                        return asdict(self.core.publications.begin(*args))
                    except PublicationIntentUnavailable:
                        # Core has already ruled out a committed same-key effect
                        # and rolled back this unavailable-proof attempt. Refresh
                        # exactly this intent once, outside every Core transaction.
                        before = self._publication(ref, candidate, check_ref)
                        refreshed = self.evidence.prepare_intent(
                            self.core, self.handle, self.plan, candidate, before, check_ref
                        )
                        if refreshed != intent["ref"]:
                            raise AccessDenied(
                                "Refreshed publication differs from the saved immutable intent"
                            )
                        return asdict(self.core.publications.begin(*args))

                context = self._step(
                    ref, owner, "publication_fence", {"intent_ref": intent["ref"]}, begin_context
                )
                context = PublicationContext(**context)
                consumer = self._publication(ref, candidate, check_ref, context)
                pub_ref = consumer.registration["ref"]

                def publish():
                    observed = consumer.publish(
                        self.store.journal.current(pub_ref)["version"], ref + ":publish"
                    )
                    if (
                        observed["publication"] == "committed"
                        and observed["quiescent"]
                        and not observed["repair_required"]
                    ):
                        proof = self.evidence.prepare(
                            self.core,
                            self.handle,
                            self.plan,
                            candidate,
                            observed,
                            consumer,
                            check_ref,
                        )
                        self.core.publications.confirm(
                            self.handle,
                            g.binding.task_id,
                            context,
                            proof,
                            ref + ":publication-confirmed",
                        )
                    return observed

                published = self._step(
                    ref, owner, "publication", {"ref": pub_ref, "commit": candidate.commit}, publish
                )
                if (
                    published["publication"] != "committed"
                    or not published["quiescent"]
                    or published["repair_required"]
                ):
                    return self._envelope(
                        ref, "uncertain", "publication_" + published["publication"]
                    )
                merge_ref = self.evidence.prepare(
                    self.core, self.handle, self.plan, candidate, published, consumer, check_ref
                )
                payload = {
                    "decision_ref": g.decision_ref,
                    "commit": candidate.commit,
                    "merge_ref": merge_ref,
                }

                def deliver():
                    current = self._scope()
                    self.core.context.check(current, g.task_version, g.decision_revision)
                    updated = self.core.transitions.transition(
                        self.handle,
                        current.id,
                        IssueStatus.IN_DEV,
                        Cause.MERGE,
                        current.version,
                        current.decision_revision,
                        g.decision_ref,
                        {"merge": merge_ref},
                        ref + ":deliver",
                    )
                    return {"task_id": updated.id, "desired": updated.desired_status}

                self._step(ref, owner, "delivery", payload, deliver)
            if self.provider is None:
                return self._envelope(ref, "blocked", "provider_confirmation_unavailable")
            confirmed = self._step(
                ref,
                owner,
                "confirmation",
                {"target": "In Dev"},
                lambda: {
                    "observed": self.provider.confirm_pending(self.handle, task.id).observed_status
                },
            )
            current = self._scope()
            if (
                confirmed["observed"] == "In Dev"
                and current.observed_status == "In Dev"
                and current.sync_state == "confirmed"
            ):
                return self._envelope(ref, "done")
            return self._envelope(ref, "uncertain", "provider_confirmation_pending")
        finally:
            self.store.release(ref, owner)

    def close(self):
        observations = []
        for ref in self.references:
            consumer = self.consumers.get(ref)
            if consumer and consumer.registration["ref"] in consumer.service.publisher._live:
                consumer.service.publisher.reconcile(consumer.registration["ref"])
            for candidate_ref in list(self.candidate.runner.live):
                self.candidate.runner.reconcile(candidate_ref)
            observations.append(
                {
                    "workflow": ref,
                    "candidate_children": len(self.candidate.runner.live),
                    "publication_children": len(consumer.service.publisher._live)
                    if consumer
                    else 0,
                    "cleanup": "pending_independent_retirement",
                }
            )
        return observations
