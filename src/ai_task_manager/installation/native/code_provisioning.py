"""Fixed sealed provisioning of project checks for the installed code workflow."""

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import MappingProxyType

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.profile import Executable, GitProfile, physical
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.application.merge_checks.catalogs import Catalogs, ProjectCatalog
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.application.merging.candidates.store import CandidateStore
from ai_task_manager.application.merging.workflows import CandidateChecks
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.checks.catalog import (
    Capabilities,
    ResourceCapability,
    RunnerCapability,
)
from ai_task_manager.domains.checks.models import (
    Availability,
    Discovery,
    Fact,
    Unit,
    content_hash,
    fingerprint,
)
from ai_task_manager.domains.checks.results import CheckExecutionRefused, CheckTargetMoved, Execution
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.installation.commands.manifest import CommandManifest
from ai_task_manager.installation.workflows.code_registration import (
    CodeCapabilities,
    CodeRegistration,
)
from ai_task_manager.installation.workflows.spec import BoundFile
from ai_task_manager.storage.checks import CheckStore
from ai_task_manager.storage.merging.journal import Journal
from ai_task_manager.supervisor.models import ResourcePair

RUNNER = "installed-python-script"
RESOURCE = "application-database"


def sealed(pointer):
    if type(pointer) is not dict or set(pointer) != {"path", "sha256"}:
        raise ValidationError("An exact private source pointer is required")
    content_hash(pointer["sha256"])
    value = BoundFile(pointer["path"])
    if value.sha256 != pointer["sha256"]:
        raise AccessDenied("Installed code source digest differs")
    return value


def database_digest(path):
    # Logical schema/data, independent of SQLite backup page counters.
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise AccessDenied("Database exceeds the installed finite snapshot budget")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0) as conn:
        rows = tuple(conn.iterdump())
    return digest(rows)


def provisioning_digest(pointer, commands_pointer):
    """Compute installer metadata; this grants no runtime preparation capability."""
    manifest = sealed(pointer)
    data = read_private(manifest.path)
    policy, catalog = sealed(data["policy"]), sealed(data["catalog"])
    commands = sealed(commands_pointer)

    def unavailable(context):
        raise AccessDenied("Only the normal resolver can provision runtime code capabilities")

    return CodeRegistration(
        policy.path, unavailable, sources=(manifest, catalog, commands)
    ).fingerprint


class CodeProvisioning:
    def __init__(self, pointer, commands_pointer, installation, *, parent_check=None):
        self.parent_check = parent_check
        self.seal = sealed(pointer)
        self.data = d = read_private(self.seal.path)
        if (
            type(d) is not dict
            or set(d)
            != {
                "version",
                "manifest_path",
                "policy",
                "git",
                "catalog",
                "units",
                "required_hooks",
                "attempts",
            }
            or type(d["version"]) is not int
            or d["version"] != 1
            or d["manifest_path"] != str(self.seal.path)
        ):
            raise ValidationError("Closed installed code provisioning fields required")
        self.policy, self.catalog = sealed(d["policy"]), sealed(d["catalog"])
        if type(commands_pointer) is not dict or set(commands_pointer) != {"path", "sha256"}:
            raise ValidationError("An exact installed command manifest pointer is required")
        self.commands = CommandManifest(commands_pointer["path"], commands_pointer["sha256"])
        if (self.commands.data["task_id"], self.commands.data["prose_digest"]) != (
            installation["task_id"],
            installation["prose_digest"],
        ):
            raise AccessDenied("Installed verification commands belong to another task or prose")
        g = d["git"]
        if type(g) is not dict or set(g) != {"root", "executable", "sha256", "exec_manifest"}:
            raise ValidationError("Exact installed Git executable and subprogram profile required")
        content_hash(g["sha256"])
        content_hash(g["exec_manifest"])
        self.root = Path(g["root"])
        physical(self.root)
        for name in ("repository", "state_root"):
            path = Path(installation[name])
            if path != self.root and self.root not in path.parents:
                raise AccessDenied("Installed code roots differ from confined Git scope")
        home = Path(installation["state_root"]) / "code-git-home"
        self.executable = Executable.capture(g["executable"], GitProfile.environment(home))
        if (self.executable.sha256, self.executable.exec_manifest) != (
            g["sha256"],
            g["exec_manifest"],
        ):
            raise AccessDenied("Installed Git executable profile differs")
        if (
            type(d["required_hooks"]) is not list
            or len(set(d["required_hooks"])) != len(d["required_hooks"])
            or any(
                name not in {"pre-commit", "prepare-commit-msg", "commit-msg"}
                for name in d["required_hooks"]
            )
        ):
            raise ValidationError("Explicit supported commit guards required")
        if type(d["units"]) is not list or not 0 < len(d["units"]) <= 16:
            raise ValidationError("Complete bounded installed check mapping required")
        if type(d["attempts"]) is not int or not 1 <= d["attempts"] <= 4:
            raise ValidationError("Installed attempts must be an integer from 1 to 4")
        self.maximum_jobs = len(d["units"]) * d["attempts"]
        self.units = {}
        for value in d["units"]:
            if type(value) is not dict or set(value) != {"name", "group", "command_ref"}:
                raise ValidationError("Closed installed check-unit mapping required")
            Unit(value["name"], value["group"], ()).validate()
            if value["name"] in self.units:
                raise ValidationError("Installed check units must be unique")
            self.units[value["name"]] = dict(value)
        if len({v["command_ref"] for v in self.units.values()}) != len(self.units) or {
            v["command_ref"] for v in self.units.values()
        } != {v.reference for v in self.commands.definitions}:
            raise ValidationError("Every installed command needs exactly one check unit")
        self.require_coverage(installation.get("files", ()))
        self.registration = CodeRegistration(
            self.policy.path, self.prepare, sources=(self.seal, self.catalog, self.commands.seal)
        )
        if (self.registration.reference, self.registration.fingerprint) != (
            installation["code_ref"],
            installation["code_digest"],
        ):
            raise AccessDenied("Installed code reference or transitive source digest differs")
        self.units = MappingProxyType(
            {name: MappingProxyType(value) for name, value in self.units.items()}
        )
        self.installation = MappingProxyType(dict(installation))
        if (
            self.registration.data["scope"],
            self.registration.data["task_id"],
            self.registration.data["prose_digest"],
        ) != (installation["scope"], installation["task_id"], installation["prose_digest"]):
            raise AccessDenied("Code policy differs from the exact installed scope/task/prose")
        self.catalogs = self._configured_catalogs(installation["scope"]["project"])
        self._bound = self._binding()
        self.check()

    def _binding(self):
        return (
            self.seal,
            self.catalog,
            self.commands,
            self.executable,
            self.registration,
            self.parent_check,
            self.units,
            self.root,
            self.installation,
            self.catalogs,
        )

    def check(self):
        if any(a is not b for a, b in zip(self._binding(), self._bound)):
            raise AccessDenied("Installed code provisioning implementation changed")
        if self.parent_check is not None:
            self.parent_check()
        if self.maximum_jobs != len(self.units) * self.data["attempts"]:
            raise AccessDenied("Installed retained-job budget changed")
        self.registration.check()
        self.commands.check()
        self.executable.validate()
        if read_private(self.seal.path) != self.data:
            raise AccessDenied("Installed code provisioning mapping changed")

    def prepare_commands(self, context):
        self.check()
        return self.commands.prepare(context, maximum_jobs=self.maximum_jobs)

    def require_coverage(self, paths):
        covered = {path for definition in self.commands.definitions for path in definition.files}
        if not set(paths) <= covered:
            raise AccessDenied("Installed check recipes do not cover every allowed or changed file")

    def prepare(self, context):
        self.check()
        self.registration.require(context.core, context.handle, context.task)
        current = context.core.get_task(context.handle, context.task.id)
        if (
            current != context.task
            or current.human_hold
            or (
                not context.code_history
                and (
                    current.delivered_once
                    or current.observed_status != "In Progress"
                    or current.sync_state != "confirmed"
                )
            )
            or context.core.context.clock() >= context.deadline
        ):
            raise AccessDenied("Current unexpired owned code task is required")
        if context.deadline != self.installation["deadline"]:
            raise AccessDenied("Original installed code deadline changed")
        if Path(context.resources.git.repository) != Path(self.installation["repository"]):
            raise AccessDenied("Installed code repository changed")
        home = Path(self.installation["state_root"]) / "code-git-home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile = GitProfile(root=self.root, executable=self.executable, home=home)
        catalogs = self.catalogs
        catalogs.for_project(context.core.session_binding(context.handle)[1].project)
        target_id = resource_alias("publication-worktree", self.installation["repository"])
        profile.enroll(
            self.installation["repository"],
            worktree_id=target_id,
            repository_id=resource_alias("repository", self.installation["repository"]),
            target_ref=self.registration.data["target_ref"],
        )
        head = (
            profile.query(self.installation["repository"], "symbolic-ref", "-q", "HEAD", ok=(0, 1))
            .stdout.decode()
            .strip()
        )
        publication_worktree = target_id if head == self.registration.data["target_ref"] else None
        owner = CandidateCheckOwner(
            self, context, profile, catalogs, publication_worktree=publication_worktree
        )
        if context.code_history and context.code_history["checks"]["path"] != str(
            owner.root / "results.sqlite"
        ):
            raise AccessDenied("Retained check store belongs to another installation")
        checks = MergeChecks(
            CheckStore.open_existing(context.code_history["checks"])
            if context.code_history
            else CheckStore(owner.root / "results.sqlite"),
            owner,
            owner,
            catalogs=catalogs,
        )
        return CodeCapabilities(
            profile,
            CandidateChecks(checks, owner.discover, owner.restore),
            tuple(self.data["required_hooks"]),
            self.data["attempts"],
            publication_worktree,
        )

    def _configured_catalogs(self, project):
        capabilities = Capabilities(
            (
                RunnerCapability(
                    RUNNER,
                    ("verification",),
                    ("source", "tool", "environment", "resource"),
                    digest({"recipe": RUNNER, "image": self.commands.data["image_id"]}),
                ),
            ),
            (
                ResourceCapability(
                    RESOURCE, ("contents",), digest({"recipe": "sqlite-logical-dump-v1"})
                ),
            ),
        )
        catalogs = Catalogs(
            (ProjectCatalog(project, str(self.catalog.path), self.catalog.sha256, capabilities),)
        )
        catalog = catalogs.for_project(project)
        if {v["group"] for v in self.units.values()} != {g.key for g in catalog.groups}:
            raise ValidationError("Installed mapping must cover every configured check group")
        return catalogs


class CandidateCheckOwner:
    def __init__(self, source, context, profile, catalogs, *, publication_worktree=None):
        self.source, self.context, self.profile, self.catalogs = source, context, profile, catalogs
        self.publication_worktree = publication_worktree
        self.root = Path(context.host_journal.path).parent / "candidate-checks"
        if context.code_history:
            if not self.root.is_dir() or not (self.root / "containers").is_dir():
                raise AccessDenied("Original candidate check resources are missing")
        else:
            self.root.mkdir(mode=0o700, exist_ok=True)
        command = source.commands.data
        self.runner = DockerCommands(
            context.host_journal,
            self.root / "containers",
            command["docker_executable"],
            command["docker_endpoint"],
            {
                definition.reference: definition.plan(command["image_id"])
                for definition in source.commands.definitions
            },
            deadline=context.deadline,
            maximum_jobs=source.maximum_jobs,
        )
        self._roots = (physical(self.root), physical(self.runner.root))
        self.selected = None
        self._bound = (source, context, profile, catalogs, self.runner, publication_worktree)
        self._plans = dict(self.runner.plans)
        self._environment = dict(self.runner.environment)
        self._transport = (
            self.runner.docker,
            self.runner.docker_hash,
            self.runner.deadline,
            self.runner.root,
            self.runner.maximum_jobs,
        )

    def _current(self, *, retained=False):
        if (
            self.source,
            self.context,
            self.profile,
            self.catalogs,
            self.runner,
            self.publication_worktree,
        ) != self._bound or (
            self.runner.plans != self._plans
            or self.runner.environment != self._environment
            or (
                self.runner.docker,
                self.runner.docker_hash,
                self.runner.deadline,
                self.runner.root,
                self.runner.maximum_jobs,
            )
            != self._transport
            or (physical(self.root), physical(self.runner.root)) != self._roots
        ):
            raise AccessDenied("Installed candidate-check runner binding changed")
        self.source.check()
        c = self.context
        if c.core.context.clock() >= c.deadline:
            raise AccessDenied("Original installed code-check deadline expired")
        task = c.core.get_task(c.handle, c.task.id)
        self.source.registration.require(c.core, c.handle, task)
        if task.human_hold or (
            not retained
            and (
                task.delivered_once
                or task.observed_status != "In Progress"
                or task.sync_state != "confirmed"
            )
        ):
            raise AccessDenied("Current owned code task is not available for checks")
        return task

    def discover(self, candidate):
        self._select(candidate)
        self._require_new_work()
        if not self._verify_source():
            raise AccessDenied("Published candidate permits only retained evidence consumption")
        return self._discovery(self._units())

    def restore(self, candidate):
        self._select(candidate, retained=True)
        self._verify_source()

    def _select(self, candidate, *, retained=False):
        self._current(retained=retained)
        c = self.context
        path = Path(c.host_journal.path).parent / "merge/journal.sqlite"
        if not path.is_file():
            raise AccessDenied("Original candidate journal is required for check discovery")
        store = CandidateStore(
            Journal.open_existing(c.code_history["merge"]) if c.code_history else Journal(path)
        )
        data = store.get(candidate.operation_ref)
        store.owner(candidate.operation_ref)
        if data.get("result") != asdict(candidate) or candidate.status != "ready":
            raise AccessDenied("Exact current prepared candidate is required")
        grant = dict(data["request"]["grant"])
        grant["binding"] = Binding(**grant["binding"])
        grant = CandidateGrant(**grant)
        facts = c.runtime_journal.observations(grant.binding.attempt_id, "resources_ready")
        if not facts:
            raise AccessDenied("Original candidate resources are unavailable")
        fact = facts[-1]
        pair = ResourcePair(
            grant.binding.attempt_id,
            Path(fact["worktree"]["path"]),
            Path(fact["database"]["path"]),
            fact["worktree_id"],
            fact["database_id"],
            fact["id"],
        )
        c.resources.verify(pair)
        cp = CandidateProfile(
            self.profile,
            grant.binding.worktree_id,
            grant.binding.repository_id,
            path.parent / "candidates",
            required_hooks=self.source.data["required_hooks"],
        )
        if data["request"]["profile"] != cp.digest:
            raise AccessDenied("Candidate check Git profile differs from durable preparation")
        cp.verify_candidate(
            Path(candidate.path), candidate.candidate_ref, data["resource"]["physical"]
        )
        self.selected = (candidate, grant, pair, cp, store)

    def _discovery(self, units):
        return Discovery(
            Availability.KNOWN,
            units,
            tuple(sorted({f.name for u in units for f in u.inputs if f.kind == "source"})),
            self.source.registration.fingerprint,
        )

    def _verify_source(self, *, defer_target=False):
        task = self._current(retained=True)
        candidate, grant, pair, cp, store = self.selected
        c = self.context
        attempt = c.core.get_attempt(c.handle, pair.attempt_id)
        scope = c.core.session_binding(c.handle)[1]
        published = self._published(candidate, grant, cp, store)
        delivered = (
            published
            and task.delivered_once
            and any(
                event.task_id == task.id
                and event.kind == "transition_requested"
                and event.payload.get("decision_ref") == grant.decision_ref
                and event.payload.get("cause") == "merge"
                for event in c.core.notifications.list_events(c.handle)
            )
        )
        if (
            (
                grant.binding.task_id,
                grant.binding.principal_id,
                grant.binding.project_id,
                grant.binding.session_id,
                grant.binding.prose_digest,
                grant.binding.source_digest,
            )
            != (
                task.id,
                scope.principal,
                scope.project,
                scope.logical_session,
                task.prose_digest,
                self.source.registration.fingerprint,
            )
            or (attempt.version, task.decision_revision)
            != (grant.attempt_version, grant.decision_revision)
            or (task.version != grant.task_version and not delivered)
            or (task.delivered_once and not delivered)
            or (
                not task.delivered_once
                and (task.observed_status != "In Progress" or task.sync_state != "confirmed")
            )
            or attempt.state != "done"
        ):
            raise AccessDenied("Candidate check authority or version changed")
        c.resources.verify(pair)
        store.owner(candidate.operation_ref)
        saved = store.get(candidate.operation_ref)
        if saved.get("result") != asdict(candidate) or saved["request"] != {
            "grant": asdict(grant),
            "profile": cp.digest,
        }:
            raise AccessDenied("Durable candidate check context changed")
        cp.verify_candidate(
            Path(candidate.path),
            candidate.candidate_ref,
            store.get(candidate.operation_ref)["resource"]["physical"],
        )
        source_head, target_head = cp.refs(grant)
        published = self._published(candidate, grant, cp, store)
        expected_target = candidate.commit if published else grant.target_head
        retained_descendant = (
            delivered
            and target_head != expected_target
            and cp.query(
                candidate.path,
                "merge-base",
                "--is-ancestor",
                candidate.commit,
                target_head,
                ok=(0, 1),
            ).returncode
            == 0
        )
        if source_head != grant.source_head:
            raise AccessDenied("Candidate check source or target changed")
        if (
            cp.query(candidate.path, "rev-parse", "HEAD").stdout.decode().strip()
            != candidate.commit
            or cp.query(candidate.path, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
            != candidate.tree
            or cp.query(
                candidate.path, "status", "--porcelain", "-z", "--untracked-files=all"
            ).stdout
        ):
            raise AccessDenied("Candidate check source or target changed")
        changed = cp.query(
            candidate.path,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            grant.target_head,
            candidate.commit,
            "--",
        ).stdout
        try:
            paths = tuple(path.decode("utf-8") for path in changed.split(b"\0") if path)
        except UnicodeDecodeError:
            raise AccessDenied("Changed candidate paths require supported UTF-8 names") from None
        self.source.require_coverage(paths)
        if target_head != expected_target and not retained_descendant:
            moved = CheckTargetMoved(
                candidate_commit=candidate.commit, candidate_tree=candidate.tree,
                source_head=source_head, expected_target=expected_target,
                observed_target=target_head, grant_digest=grant.digest,
                binding_digest=grant.binding.fingerprint,
                producer_digest=grant.binding.source_digest,
                attempt_id=grant.binding.attempt_id, task_id=grant.binding.task_id,
            )
            if defer_target:
                return moved
            raise moved
        return not published

    def _publication_started(self):
        candidate, grant, _, cp, store = self.selected
        if self.publication_worktree is not None:
            selected = self._publication_selection(candidate, grant, cp, store)
            if selected is None:
                return False
            expected, request, _ = selected
            return any(
                op["binding"] == asdict(expected)
                and op["fingerprint"] == expected.fingerprint
                and op["facts"].get("publication_intent") == request
                for op in store.journal.for_attempt(grant.binding.attempt_id)
            )
        for op in store.journal.for_attempt(grant.binding.attempt_id):
            selected = op["binding"]
            if {key: value for key, value in selected.items() if key != "operation_key"} == {
                key: value for key, value in asdict(grant.binding).items() if key != "operation_key"
            } and op["facts"].get("publication_intent", {}).get("new") == candidate.commit:
                return True
        return False

    def _require_new_work(self):
        if self._publication_started():
            raise AccessDenied(
                "Frozen publication intent permits evidence consumption, not new checks"
            )

    def _publication_selection(self, candidate, grant, cp, store):
        """Share exact canonical author/candidate and enrolled publication identity."""
        operations = store.journal.for_attempt(grant.binding.attempt_id)

        def current_grant(op):
            row = op["facts"].get("merge_workflow", {})
            if row.get("target_lineage"):
                from ai_task_manager.application.merging.workflows.recovery import recorded_plan

                return asdict(
                    recorded_plan(row, maximum_attempts=self.source.data["attempts"]).grant
                )
            return row.get("request", {}).get("grant")

        workflows = [
            op
            for op in operations
            if (
                current_grant(op) == asdict(grant)
                and op["facts"]["merge_workflow"]["request"]["profile"] == cp.digest
                and op["facts"]["merge_workflow"]["steps"].get("candidate", {}).get("result")
                == asdict(candidate)
            )
        ]
        if len(workflows) != 1:
            return None
        workflow = workflows[0]
        expected_binding = replace(grant.binding, operation_key=workflow["ref"] + ":publication")
        mode = "cas"
        publication_repository = cp.source
        if self.publication_worktree is not None:
            target = self.profile.validate(self.publication_worktree, grant.binding.repository_id)
            expected_binding = replace(
                expected_binding,
                worktree_id=self.publication_worktree,
                configuration_digest=target["configuration_digest"],
            )
            mode = "fast_forward"
            publication_repository = target
        request = {
            "source_ref": candidate.candidate_ref,
            "old": grant.target_head,
            "new": candidate.commit,
            "checked": candidate.commit,
            "range_ref": workflow["ref"] + ":range",
            "mode": mode,
        }
        return expected_binding, request, publication_repository

    def _published(self, candidate, grant, cp, store):
        """Observe only exact canonical recorded quiescent publication."""
        selected = self._publication_selection(candidate, grant, cp, store)
        if selected is None:
            return False
        expected_binding, request, publication_repository = selected
        matches = []
        for op in store.journal.for_attempt(grant.binding.attempt_id):
            data = op["facts"].get("local_git", {})
            result = data.get("result", {})
            if (
                op["binding"] == asdict(expected_binding)
                and op["fingerprint"] == expected_binding.fingerprint
                and op["facts"].get("publication_intent") == request
                and data.get("request") == request
                and digest(data.get("repository")) == digest(publication_repository)
                and result.get("publication") == "committed"
                and result.get("quiescent") is True
                and result.get("repair_required") is False
                and result.get("old") == grant.target_head
                and result.get("new") == candidate.commit
                and result.get("current_target") == candidate.commit
                and result.get("target_ref") == cp.source["target_ref"]
                and result.get("repository_id") == grant.binding.repository_id
                and result.get("configuration_digest") == expected_binding.configuration_digest
                and result.get("evidence_ref") == data.get("evidence_ref")
                and result.get("process_observation", {}).get("known") is True
                and result["process_observation"].get("quiescent") is True
                and result["process_observation"].get("returncode") == 0
            ):
                matches.append(op)
        return len(matches) == 1

    def _units(self):
        candidate, _, pair, _, _ = self.selected
        workspace = Workspace(candidate.path)
        definitions = {d.reference: d for d in self.source.commands.definitions}
        database = database_digest(pair.database)
        units = []
        for name, value in self.source.units.items():
            definition = definitions[value["command_ref"]]
            files = {p: workspace.read(p)["sha256"] for p in definition.files}
            if files[definition.script] != definition.script_sha256:
                raise AccessDenied("Installed verification script changed")
            inputs = tuple(Fact("source", p, h) for p, h in sorted(files.items())) + (
                Fact("tool", "container-image", self.source.commands.data["image_id"][7:]),
                Fact("environment", "installed-code", self.source.registration.fingerprint),
                Fact("resource", RESOURCE + "/contents", database),
            )
            units.append(Unit(name, value["group"], inputs))
        return tuple(units)

    def verify(self, plan):
        if self.selected is None:
            raise AccessDenied("Current candidate discovery must precede check consumption")
        moved = self._verify_source(defer_target=True)
        candidate, grant, _, _, _ = self.selected
        all_units = self._units()
        expected = {u.name: u for u in all_units}
        expected_scope = {
            "principal": grant.binding.principal_id,
            "project": grant.binding.project_id,
            "session": grant.binding.session_id,
            "task": grant.binding.task_id,
        }
        if (
            asdict(plan.scope) != expected_scope
            or plan.producer_digest != self.source.registration.fingerprint
            or plan.inventory_digest != fingerprint(asdict(self._discovery(all_units)))
            or plan.inventory_size != len(all_units)
            or plan.head != candidate.commit
            or plan.base != grant.target_head
            or plan.source_digest != self.source.registration.fingerprint
            or plan.attempt != grant.binding.attempt_id
            or any(expected.get(unit.name) != unit for unit in plan.units)
        ):
            raise AccessDenied("Prepared check inputs differ from current candidate")
        self.catalogs.verify(plan)
        if type(moved) is CheckTargetMoved:
            raise moved

    def execute(self, plan, group, names):
        self.verify(plan)
        self._require_new_work()
        if not self._verify_source():
            raise AccessDenied(
                "Published candidate permits evidence consumption, not new execution"
            )
        candidate, _, pair, _, _ = self.selected
        selected = {u.name: u for u in plan.units}
        outputs, refs = [], []
        started = time.monotonic()
        for name in names:
            value = self.source.units[name]
            if selected[name].group != group:
                raise AccessDenied("Dispatched unit belongs to another installed group")
            key = "candidate-check:" + plan.digest + ":" + name

            def boundary():
                self.verify(plan)
                self._require_new_work()
                job = self.runner.jobs.get("container:" + key)
                if job is not None:
                    snapshot = job["directory"] / "input.sqlite"
                    expected = next(f.value for f in selected[name].inputs if f.kind == "resource")
                    if database_digest(snapshot) != expected:
                        raise AccessDenied(
                            "Actual command database snapshot differs from planned input"
                        )

            @contextmanager
            def effect():
                boundary()
                yield
                boundary()

            result = self.runner.execute_registered(
                value["command_ref"],
                replace(pair, worktree=Path(candidate.path)),
                boundary,
                effect,
                key,
            )
            proof = self.runner.verified_result(
                pair.attempt_id, value["command_ref"], operation_key=key
            )
            expected_files = tuple(
                sorted((f.name, f.value) for f in selected[name].inputs if f.kind == "source")
            )
            if proof.files != expected_files:
                raise AccessDenied("Actual confined command inputs differ from selected unit")
            outputs.append(result)
            refs.append(proof.trace_ref)
        resources = next(g.resources for g in plan.groups if g.key == group)
        execution = Execution(
            plan.digest,
            group,
            names,
            names,
            names,
            (),
            0,
            "completed",
            True,
            digest(refs),
            digest(outputs),
            started,
            time.monotonic(),
            resource_refs=tuple((r, pair.database_id) for r in resources),
        )
        try:
            self.verify(plan)
        except Exception as error:
            raise CheckExecutionRefused(execution, error) from error
        return execution
