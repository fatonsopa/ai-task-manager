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

from .recovery import current_plan, restore, successor
from .store import MergeWorkflowStore


class _CandidatePublicationAuthority(CoreAuthority):
    def __init__(
        self,
        *args,
        candidate_store,
        candidate_ref,
        candidate_profile,
        source_grant,
        candidate_reviews=None,
    ):
        super().__init__(*args)
        self.candidate_store, self.candidate_ref = candidate_store, candidate_ref
        self.candidate_profile, self.source_grant = candidate_profile, source_grant
        self.candidate_reviews = candidate_reviews

    def check_current(self, binding):
        if self.candidate_reviews is not None:
            self.candidate_reviews.check_prepared(
                self.candidate_store.get(self.candidate_ref)["result"]["commit"]
            )
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
        candidate_reviews=None,
        publication_lifecycle=None,
        maximum_attempts=1,
        publication_worktree=None,
        recover_claim=None,
    ):
        self.core, self.handle, self.plan = core, handle, plan
        self.recover_claim = self._recover_claim_bound = recover_claim
        self.original_plan = plan
        if type(maximum_attempts) is not int or not 1 <= maximum_attempts <= 4:
            raise AccessDenied("Installed candidate attempts must be an integer from 1 to 4")
        self.maximum_attempts = self._maximum_attempts = maximum_attempts
        self.publication_worktree = self._publication_worktree_bound = publication_worktree
        self.candidate_reviews = candidate_reviews
        self._candidate_reviews_bound = candidate_reviews
        self.publication_lifecycle = self._publication_lifecycle_bound = publication_lifecycle
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
        if self.recover_claim is not self._recover_claim_bound:
            raise AccessDenied("Installed merge recovery owner changed")
        if self.publication_worktree != self._publication_worktree_bound:
            raise AccessDenied("Installed publication worktree changed")
        if self.maximum_attempts != self._maximum_attempts:
            raise AccessDenied("Installed candidate attempt budget changed")
        if self.publication_lifecycle is not self._publication_lifecycle_bound:
            raise AccessDenied("Installed publication lifecycle owner changed")
        if self.candidate_reviews is not self._candidate_reviews_bound:
            raise AccessDenied("Installed code-candidate review port changed")
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
        ref = self.store.create(
            self.original_plan,
            self.candidate.profile,
            operation_key,
            maximum_attempts=self.maximum_attempts,
        )
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

    def _publisher(self, journal, profile, operations):
        if self.publisher_factory is LocalGit:
            return LocalGit(
                journal, profile, operations, inherited_turn=self.candidate.profile.inherited_turn()
            )
        return self.publisher_factory(journal, profile, operations)

    def _publication_binding(self, ref):
        binding = replace(self.plan.grant.binding, operation_key=ref + ":publication")
        if self.publication_worktree is not None:
            profile = self.candidate.profile.profile
            target = profile.validate(self.publication_worktree, binding.repository_id)
            source = self.candidate.profile.source
            if (target["common"], target["common_identity"], target["target_ref"]) != (
                source["common"],
                source["common_identity"],
                source["target_ref"],
            ):
                raise AccessDenied("Publication checkout belongs to another repository or target")
            binding = replace(
                binding,
                worktree_id=self.publication_worktree,
                configuration_digest=target["configuration_digest"],
            )
        return binding

    def _publication(self, ref, candidate, check_ref, publication_context=None):
        grant = self.plan.grant
        binding = self._publication_binding(ref)
        request = Publication(
            candidate.candidate_ref,
            grant.target_head,
            candidate.commit,
            candidate.commit,
            ref + ":range",
            "fast_forward" if self.publication_worktree is not None else "cas",
        )
        lease = LeaseGrant(
            binding,
            request,
            grant.task_version,
            grant.attempt_version,
            grant.decision_revision,
            grant.decision_ref,
            publication_context,
            grant.binding if self.publication_worktree is not None else None,
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
            publisher_factory=self._publisher,
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
            candidate_reviews=self.candidate_reviews,
        )
        consumer.service.authority = consumer.authority
        self.consumers[ref] = consumer
        if self.publication_worktree is not None and not self.store.journal.current(
            consumer.registration["ref"]
        )["facts"].get("local_git"):
            record = consumer.authority.profile.validate(binding.worktree_id, binding.repository_id)
            if consumer.authority.profile.query(
                record["path"], "status", "--porcelain", "-z", "--untracked-files=all"
            ).stdout:
                raise AccessDenied("Dirty target worktree prevents code delivery")
        return consumer

    def _prepare_candidate(self, row):
        history = row.get("target_lineage", [])
        if history:
            return self.candidate.target_successor(
                restore(history[-1]["plan"]).grant, self.plan.grant
            )
        return self.candidate.prepare(self.plan.grant)

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

    def _observed_publication(self, ref, row, task):
        saved = row["steps"].get("publication_fence")
        if saved is None:
            return None
        if saved["result"] is not None:
            fact = self.core.publications.inspect(
                self.handle, task.id, PublicationContext(**saved["result"])
            )
        else:
            from ai_task_manager.storage import issue_claims, merging_fences, sessions, work

            with self.core.context.db.transaction() as conn:
                sid, scope = sessions.resolve(conn, self.handle)
                current = work.get(conn, sid, task.id)
                if (sid, scope) != (self.session_id, self.scope) or current != task:
                    raise AccessDenied("Publication readback task or scope changed")
                fact = merging_fences.current(conn, issue_claims.identity(scope, task.issue_id))
        if fact is None:
            return None
        g = self.plan.grant
        candidate = CandidateResult(**row["steps"]["candidate"]["result"])
        expected = {
            "task_id": task.id,
            "attempt_id": g.binding.attempt_id,
            "issue_id": task.issue_id,
            "prose_digest": g.binding.prose_digest,
            "task_version": g.task_version,
            "attempt_version": g.attempt_version,
            "decision_revision": g.decision_revision,
            "decision_ref": g.decision_ref,
            "operation_ref": row["steps"]["publication_intent"]["payload"]["ref"],
            "binding_digest": self._publication_binding(ref).fingerprint,
            "commit": candidate.commit,
            "old": g.target_head,
            "source_ref": g.source_ref,
            "source_head": g.source_head,
            "target_ref": self.candidate.profile.source["target_ref"],
            "candidate_ref": candidate.candidate_ref,
            "checks_ref": row["steps"]["check_plan"]["result"]["ref"],
        }
        if fact["intent"] != expected:
            raise AccessDenied("Unrecorded fence result belongs to another publication context")
        return PublicationContext(**fact["context"])

    def _prepare_lifecycle(self, ref, row):
        if self.publication_lifecycle is None:
            return
        steps = row["steps"]
        saved = steps.get("publication_fence")
        if saved is None:
            self.publication_lifecycle.start()
        elif (context := self._observed_publication(ref, row, self._scope())) is not None:
            candidate = CandidateResult(**steps["candidate"]["result"])
            binding = self._publication_binding(ref)
            self.publication_lifecycle.adopt_fence(
                self.plan,
                context,
                binding.fingerprint,
                candidate.commit,
            )
        else:
            self.publication_lifecycle.before_fence(self.plan)

    def resume(self, workflow_ref):
        task = self._scope()
        row = self.store.read(workflow_ref)
        if (row["request"]["plan"], row["request"]["grant"]) != (
            self.original_plan.digest,
            asdict(self.original_plan.grant),
        ):
            raise AccessDenied("Merge workflow is not the current installed context")
        self.plan = current_plan(self.original_plan, row, maximum_attempts=self.maximum_attempts)
        self._prepare_lifecycle(workflow_ref, row)
        if task.delivered_once:
            return self._resume(workflow_ref)
        authority = self.candidate.authority

        def current():
            task = self._scope()
            if task.human_hold or self.clock() >= self.plan.deadline:
                raise AccessDenied("Original merge authority changed while waiting")
            authority.resources.verify(authority.pair)
            context = self._observed_publication(workflow_ref, row, task)
            if context is None:
                self._ready()
            else:
                g = self.plan.grant
                fact = self.core.publications.inspect(self.handle, task.id, context)
                if fact["state"] == "pending":
                    with self.core.execution.merge_snapshot_boundary(
                        self.handle,
                        task.id,
                        g.binding.attempt_id,
                        g.task_version,
                        g.attempt_version,
                        g.decision_revision,
                        g.decision_ref,
                        publication_context=context,
                    ):
                        pass
                # A committed exact Core fact permits receipt consumption only;
                # the retained LocalGit publication cannot spawn another effect.

        with authority.resources.use(authority.pair):
            with self.candidate.profile.merge_turn(
                deadline=self.plan.deadline, clock=self.clock, check=current
            ) as waited:
                self.timings.append({"stage": "repository_wait", "seconds": waited})
                return self._resume(workflow_ref)

    def _resume(self, workflow_ref):
        task = self._scope()
        row = self.store.read(workflow_ref)
        if (row["request"]["plan"], row["request"]["grant"]) != (
            self.original_plan.digest,
            asdict(self.original_plan.grant),
        ):
            raise AccessDenied("Merge workflow is not the current installed context")
        self.plan = current_plan(self.original_plan, row, maximum_attempts=self.maximum_attempts)
        self.references.add(workflow_ref)
        if row["owner"] is not None and self.recover_claim is not None:
            self.store.release_dead_claim(workflow_ref, row["owner"], self.recover_claim)
        owner = self.store.claim(workflow_ref)
        ref = workflow_ref
        try:
            steps = row["steps"]
            if not task.delivered_once and not any(
                name in steps for name in ("publication_intent", "publication_fence", "publication")
            ):
                self._ready()
                source, target = self.candidate.profile.refs(self.plan.grant)
                if source != self.plan.grant.source_head:
                    raise AccessDenied("Completed worker source changed before target recovery")
                if target != self.plan.grant.target_head:
                    history = row.get("target_lineage", [])
                    if len(history) >= self.maximum_attempts - 1:
                        raise AccessDenied("Original merge target-advance budget exhausted")
                    previous_checks = steps.get("check_plan", {}).get("result")
                    if previous_checks:
                        b = self.plan.grant.binding
                        check_scope = Scope(b.principal_id, b.project_id, b.session_id, b.task_id)
                        snapshot = self.check_inputs.checks.store.read(
                            previous_checks["ref"], check_scope
                        )
                        if any(
                            group["state"] not in {"pending", "complete"}
                            or (
                                group["state"] == "complete"
                                and group["result"]["outcome"]
                                not in {"passed", "skipped", "assertion", "dev_red"}
                            )
                            for group in snapshot["groups"].values()
                        ):
                            raise AccessDenied("Unresolved checks retain original target ownership")
                    selected = successor(self.plan, target)
                    self.candidate.target_successor(
                        self.plan.grant, selected.grant, validate_only=True
                    )
                    # Preserve every prior step before the new candidate's first effect.
                    self.store.update(
                        ref,
                        owner,
                        active_plan=asdict(selected),
                        steps={},
                        target_lineage=[
                            *history,
                            {"plan": asdict(self.plan), "target": target, "steps": steps},
                        ],
                    )
                    self.plan = selected
                    row = self.store.read(ref)
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
                        lambda: asdict(self._prepare_candidate(row)),
                    )
                    candidate = CandidateResult(**value)
                    if candidate.status != "ready":
                        return self._envelope(
                            ref,
                            "uncertain" if candidate.status == "uncertain" else "blocked",
                            "candidate_" + candidate.status,
                        )
                    self.candidate.store.owner(candidate.operation_ref)
                    self.candidate.reconcile(candidate.operation_ref, self.plan.grant)
                    self._ready()
                    if self.candidate_reviews is not None:
                        review = self.candidate_reviews.read(task, candidate)
                        if review is None:
                            return self._envelope(
                                ref, "awaiting_review", "candidate_review_unavailable"
                            )
                        self._step(
                            ref,
                            owner,
                            "candidate_review",
                            {"commit": candidate.commit},
                            lambda: review,
                        )
                    prepared_check = self.check_inputs.prepare(
                        self.candidate.profile,
                        self.plan.grant,
                        candidate,
                        ref + ":checks:" + self.plan.grant.digest,
                    )
                    check_plan = self._step(
                        ref,
                        owner,
                        "check_plan",
                        {"candidate": candidate.commit},
                        lambda: {"ref": prepared_check},
                    )
                    check_ref = check_plan["ref"]
                    if check_ref != prepared_check:
                        raise AccessDenied(
                            "Retained check plan differs from exact candidate inputs"
                        )
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
                if fence_step is not None:
                    self.check_inputs.restore(
                        self.candidate.profile, self.plan.grant, candidate, check_ref
                    )
                g = self.plan.grant

                def begin_context():
                    if self.publication_lifecycle is not None:
                        self.publication_lifecycle.before_fence(self.plan)
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
                if self.publication_lifecycle is not None:
                    binding = self._publication_binding(ref)
                    self.publication_lifecycle.adopt_fence(
                        self.plan, context, binding.fingerprint, candidate.commit
                    )
                consumer = self._publication(ref, candidate, check_ref, context)
                pub_ref = consumer.registration["ref"]

                def publish():
                    prior_actual = (
                        self.store.journal.current(pub_ref)["facts"]
                        .get("local_git", {})
                        .get("result")
                    )
                    committed_replay = bool(
                        prior_actual
                        and prior_actual.get("publication") == "committed"
                        and prior_actual.get("quiescent") is True
                        and not prior_actual.get("repair_required")
                    )
                    # An exact saved successful publication is reconciled by the
                    # existing scoped publisher; reviewing it cannot authorize
                    # another Git effect or reinterpret the committed history.
                    if self.candidate_reviews is not None and not committed_replay:
                        review = self.candidate_reviews.read(self._scope(), candidate)
                        saved = self.store.read(ref)["steps"].get("candidate_review")
                        if review is None or saved is None or saved["result"] != review:
                            raise EvidenceRequired(
                                "Current exact-candidate review differs from saved acceptance"
                            )
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
