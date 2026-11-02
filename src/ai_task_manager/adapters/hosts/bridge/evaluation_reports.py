"""Narrow report-completion facts derive from bytes and an actual registered verifier."""

import os
import stat
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.config.evaluation_records import EvaluationRecords
from ai_task_manager.config.files import FileSeal, digest, private_path, strict_json
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Evidence

from .containers import DockerCommands
from .evaluation_enrollment import EvaluationGrant
from .workspace import Workspace

REPORT_KINDS = frozenset({"work_unit_complete", "non_code_completion"})
PREFIX = "evaluation-report:"


def report_metadata(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise AccessDenied("The evaluation report is not an owned unshared regular file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class EvaluationReports:
    def __init__(self, grant, records, runner):
        if (
            not isinstance(grant, EvaluationGrant)
            or not isinstance(records, EvaluationRecords)
            or not isinstance(runner, DockerCommands)
            or runner.journal is not grant.journal
        ):
            raise AccessDenied("Report facts need the actual scoped grant and confined runner")
        self.grant, self.records, self.runner = grant, records, runner
        self._bound = (grant, records, runner)
        self._prepared = {}

    def _current(self):
        if any(a is not b for a, b in zip((self.grant, self.records, self.runner), self._bound)):
            raise AccessDenied("The evaluation report issuer changed")

    def observe(self, oracle_ref, resources):
        self._current()
        grant = self.grant
        grant.require_active()
        task = grant.core.get_task(grant._handle, grant._task_id)
        grant.require_task(task)
        attempt = grant.core.get_attempt(grant._handle, resources.attempt_id)
        record = self.records.resolve(oracle_ref, "evaluation_report_oracle", grant.run_id)
        oracle = record.data
        fields = {
            "grant_ref",
            "case_id",
            "scope",
            "claim_store",
            "task_id",
            "issue_id",
            "prose_digest",
            "unit",
            "report_name",
            "canonical_sha256",
            "command_ref",
            "expires_at",
        }
        if (
            set(oracle) != fields
            or oracle["grant_ref"] != grant.ref
            or oracle["case_id"] != grant.data["case_id"]
            or oracle["scope"] != asdict(grant.scope)
            or oracle["claim_store"] != grant.claim_store.context
            or (oracle["task_id"], oracle["issue_id"], oracle["prose_digest"])
            != (task.id, task.issue_id, task.prose_digest)
            or oracle["unit"] not in task.required_work
            or oracle["report_name"] != "report.json"
            or oracle["command_ref"] != "verify-report"
            or oracle["expires_at"] > grant.data["expires_at"]
            or oracle["expires_at"] <= grant.journal.clock()
            or attempt.task_id != task.id
            or attempt.state != "done"
            or (attempt.worktree_id, attempt.database_id)
            != (resources.worktree_id, resources.database_id)
        ):
            raise AccessDenied("The completed report attempt/oracle does not match the exact case")
        verified = self.runner.verified_result(attempt.id, oracle["command_ref"])
        path = resources.worktree / oracle["report_name"]
        before = report_metadata(path)
        actual = Workspace(resources.worktree).read(oracle["report_name"])
        if (
            report_metadata(path) != before
            or dict(verified.files).get(oracle["report_name"]) != actual["sha256"]
            or digest(strict_json(actual["text"])) != oracle["canonical_sha256"]
        ):
            raise AccessDenied("Report bytes differ from the verified input or expected result")
        source = FileSeal(private_path(self.records.bindings[oracle_ref]["path"]))
        if source.sha256 != record.sha256:
            raise AccessDenied("The report oracle changed during verification")
        prepared_grant = grant.prepare_current()
        expiry = min(oracle["expires_at"], grant.data["expires_at"], verified.expires_at)
        if not grant.journal.intend(
            "evaluation-report:" + oracle_ref,
            {"task_id": task.id, "report_sha256": actual["sha256"]},
        ):
            raise AccessDenied("This report was already observed or requires reconciliation")
        output = {}
        for kind in sorted(REPORT_KINDS):
            ref = PREFIX + str(uuid4())
            payload = {
                "prose_digest": task.prose_digest,
                "unit": oracle["unit"],
                "artifact": actual["sha256"],
                "case_id": oracle["case_id"],
                "command_ref": verified.command_ref,
                "command_input_digest": verified.input_digest,
            }
            trace = grant.journal._append(
                "trace",
                {
                    "effect": "evaluation_report_verified",
                    "proof_ref": ref,
                    "kind": kind,
                    "scope": asdict(grant.scope),
                    "task_id": task.id,
                    "grant_ref": grant.ref,
                    "oracle_ref": oracle_ref,
                    "report_sha256": actual["sha256"],
                    "command_trace": verified.trace_ref,
                    "payload": payload,
                },
                sources=(verified.trace_ref, grant.data["bootstrap_ref"]),
                expires_at=expiry,
            )
            fact = Evidence(kind, grant.scope, task.id, payload, expiry)
            self._prepared[ref] = (
                fact,
                prepared_grant,
                path,
                before,
                source,
                source.metadata(),
                trace,
            )
            output[kind] = ref
        return output

    def verify_evidence(self, ref):
        """Resolve only prepared typed facts; Core performs no report-content reads."""
        self._current()
        value = self._prepared.get(ref)
        if value is None:
            raise AccessDenied("This issuer did not independently verify that report")
        fact, grant, path, metadata, source, original_source, _ = value
        grant.check()
        if (
            report_metadata(path) != metadata
            or source.metadata() != original_source
            or fact.expires_at <= self.grant.journal.clock()
            or fact.kind not in REPORT_KINDS
        ):
            raise AccessDenied("The prepared evaluation report changed or expired")
        return fact
