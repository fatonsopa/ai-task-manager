"""Independent four-feature project using default sealed product provisioning."""

import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy
from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.installation.native import resolve
from ai_task_manager.installation.native.code_provisioning import provisioning_digest
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.workflows.composition import NativeContext
from ai_task_manager.storage.merging.journal import Journal
from tests.code_provisioning.helpers import IMAGE, pointer, sha
from tests.native_installation.helpers import PermissionSource, bundle, refresh_digest
from tests.runtime.helpers import RuntimeRig

SETTINGS = (
    '{\n"maximum": 10,\n"minimum": 0,\n"mode": "bounded",\n'
    '"padding1": 0,\n"padding2": 0,\n"padding3": 0\n}\n'
)
BROAD = (
    '{\n"maximum": 20,\n"minimum": -1,\n"mode": "wide",\n'
    '"padding1": 1,\n"padding2": 1,\n"padding3": 1\n}\n'
)


class Project:
    def __init__(self, root):
        root.mkdir(mode=0o700)
        self.root, self.repo = root, root / "project"
        self.repo.mkdir(mode=0o700)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Standalone workload fixture")
        git(self.repo, "config", "user.email", "workload@example.invalid")
        (self.repo / "settings.json").write_text(SETTINGS)
        (self.repo / "features").mkdir()
        (self.repo / "checks").mkdir()
        for i in range(4):
            (self.repo / f"features/{i}.txt").write_text("0\n" * 8)
            (self.repo / f"checks/{i}.py").write_text(
                "import json\nfrom pathlib import Path\n"
                "settings=json.loads(Path('settings.json').read_text())\n"
                f"values=[int(v) for v in Path('features/{i}.txt').read_text().splitlines()]\n"
                "assert len(values)==8\n"
                "assert all(settings['minimum'] <= v <= settings['maximum'] for v in values)\n"
                "assert settings['mode'] in {'bounded','wide'}\n"
                "assert sum(values)>=0\n"
            )
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexec /usr/bin/git diff --check --cached\n")
        hook.chmod(0o700)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "Independent four-feature baseline")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "integration", self.base)


class Owner:
    def __init__(self, project, number, *, broad=False):
        started = time.monotonic()
        self.project, self.number = project, number
        self.root = project.root / f"owner-{number}"
        self.root.mkdir(mode=0o700)
        self.runtime = RuntimeRig(self.root / "runtime")
        self.runtime.repo = project.repo
        self.runtime.resources = ResourceManager(
            self.runtime.journal,
            self.root / "resources",
            project.repo,
            metadata_paths=(self.runtime.rig.db.path,),
        )
        self.runtime.resources.release_policy = DeliveryReleasePolicy(
            project.repo, "refs/heads/integration"
        )
        self.runtime.supervisor.resources = self.runtime.resources
        self.r = self.runtime.rig
        self.r.now = time.time()
        before = time.monotonic()
        self.dispatch = self.runtime.start(self.r.queued(code=True))
        self.runtime.admit(self.dispatch)
        self.database_seconds = time.monotonic() - before
        self.pair = self.dispatch.resources
        self.changed = "settings.json" if broad else f"features/{number}.txt"
        self.value = BROAD if broad else (str(number + 1) + "\n") * 8
        before = time.monotonic()
        program = (
            "from pathlib import Path; import subprocess; "
            f"Path({self.changed!r}).write_text({self.value!r}); "
            "subprocess.run(['git','add','.'],check=True); "
            "subprocess.run(['git','commit','-qm','Owned eight-line workload'],check=True)"
        )
        self.runtime.supervisor.execute(
            self.r.handle,
            self.dispatch,
            self.runtime.command(self.dispatch, program),
            operation_key=self.r.key(),
        )
        self.worker_commit_seconds = time.monotonic() - before
        self.attempt = self.r.core.get_attempt(self.r.handle, self.dispatch.attempt_id)
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter

        self.r.core.context.evidence = EvidenceRouter(
            [
                *[
                    route
                    for route in self.r.core.context.evidence.routes
                    if route.prefix != "fixture:"
                ],
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"provider_status", "decision", "human_decision", "work_unit_complete"}
                    ),
                    self.r.proofs,
                ),
            ]
        )
        self.task = self.r.complete(self.r.core.get_task(self.r.handle, self.attempt.task_id))
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.bundle = b = bundle(self.root / "native", task_id=self.task.id)
        policy = self.root / "policy.json"
        write_private(
            policy,
            {
                "reference": "code",
                "scope": asdict(self.r.scope),
                "task_id": self.task.id,
                "prose_digest": self.task.prose_digest,
                "target_ref": "refs/heads/integration",
                "require_human_acceptance": False,
            },
        )
        catalog = self.root / "checks.json"
        write_private(
            catalog,
            {
                "format": 2,
                "groups": [
                    {
                        "labels": [f"Feature {i}"],
                        "category": "verification",
                        "runner": "installed-python-script",
                        "resources": ["application-database"],
                        "required": False,
                        "prerequisites": [],
                    }
                    for i in range(4)
                ],
            },
        )
        docker = Path("/usr/local/bin/docker").resolve()
        endpoint = subprocess.check_output(
            [str(docker), "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
            text=True,
            timeout=5,
        ).strip()
        socket = Path(endpoint[7:]).resolve()
        info = socket.stat()
        commands = self.root / "commands.json"
        write_private(
            commands,
            {
                "version": 1,
                "manifest_path": str(commands),
                "task_id": self.task.id,
                "prose_digest": self.task.prose_digest,
                "docker_executable": str(docker),
                "docker_sha256": sha(docker),
                "docker_endpoint": "unix://" + str(socket),
                "endpoint_identity": {
                    "path": str(socket),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                },
                "image_id": IMAGE,
                "commands": [
                    {
                        "reference": f"check-{i}",
                        "recipe": "python-script",
                        "script": f"checks/{i}.py",
                        "script_sha256": sha(project.repo / f"checks/{i}.py"),
                        "files": [f"checks/{i}.py", f"features/{i}.txt", "settings.json"],
                        "seconds": 8,
                    }
                    for i in range(4)
                ],
            },
        )
        executable = Executable.capture(
            "/usr/bin/git", GitProfile.environment(self.root / "temporary-home")
        )
        manifest = self.root / "code.json"
        write_private(
            manifest,
            {
                "version": 1,
                "manifest_path": str(manifest),
                "policy": pointer(policy),
                "git": {
                    "root": str(project.root),
                    "executable": executable.path,
                    "sha256": executable.sha256,
                    "exec_manifest": executable.exec_manifest,
                },
                "catalog": pointer(catalog),
                "units": [
                    {"name": f"feature-{i}", "group": f"feature {i}", "command_ref": f"check-{i}"}
                    for i in range(4)
                ],
                "required_hooks": ["pre-commit"],
                "attempts": 4,
            },
        )
        spec = json.loads(b.installation.read_text())
        spec.update(
            repository=str(project.repo),
            state_root=str(self.state),
            scope=asdict(self.r.scope),
            prose_digest=self.task.prose_digest,
            code_ref="code",
            files=[self.changed],
            deadline=self.r.now + 600,
        )
        spec["code_digest"] = provisioning_digest(pointer(manifest), pointer(commands))
        write_private(b.installation, spec, replace=True)
        b.data.update(commands_manifest=pointer(commands), code_manifest=pointer(manifest))
        b.data["installation"] = installation_intent(b.installation)
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        self.context = NativeContext(
            self.r.core,
            self.r.handle,
            self.task,
            self.runtime.journal,
            HostJournal(self.state / "host.sqlite", self.state / "host.key"),
            self.runtime.resources,
            self.runtime.runtime,
            spec["deadline"],
        )
        registry = resolve(b.config, b.installation, authority_source=PermissionSource())
        self.code = registry.require_code("code")
        self.capability = self.code.prepare(self.context)
        tree_id = resource_alias("worktree", self.pair.worktree_id)
        repo_id = resource_alias("repository", str(project.repo))
        record = self.capability.profile.enroll(
            self.pair.worktree,
            worktree_id=tree_id,
            repository_id=repo_id,
            target_ref="refs/heads/integration",
        )
        binding = Binding(
            self.task.id,
            self.attempt.id,
            self.r.scope.principal,
            self.r.scope.logical_session,
            self.r.scope.project,
            self.task.prose_digest,
            self.code.fingerprint,
            tree_id,
            repo_id,
            resource_alias("database", self.pair.database_id),
            record["configuration_digest"],
            f"workload:{self.task.id}",
        )
        self.grant = CandidateGrant(
            binding,
            "refs/heads/managed/" + self.attempt.id,
            git(self.pair.worktree, "rev-parse", "HEAD"),
            project.base,
            self.task.version,
            self.attempt.version,
            self.task.decision_revision,
            self.r.decision(self.task, "In Dev"),
            "owned-workload-lead",
        )
        root = self.state / "merge"
        root.mkdir(mode=0o700)
        (root / "candidates").mkdir(mode=0o700)
        self.candidates = CandidateService(
            Journal(root / "journal.sqlite"),
            CandidateProfile(
                self.capability.profile,
                tree_id,
                repo_id,
                root / "candidates",
                required_hooks=("pre-commit",),
            ),
            CoreCandidateAuthority(self.r.core, self.r.handle, self.runtime.resources, self.pair),
            process_factory=CandidateProcesses,
        )
        self.setup_seconds = time.monotonic() - started
        self.queued_at = time.monotonic()
        self.workflow = None

    def close(self):
        if self.workflow is not None:
            self.workflow.close()
        for reference in tuple(self.candidates.runner.live):
            self.candidates.runner.reconcile(reference)
        self.runtime.close()
