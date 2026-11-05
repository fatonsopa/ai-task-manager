"""Owned local Git/SQLite and sealed production inputs; authored launch permission only."""

import hashlib
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
from tests.native_installation.helpers import PermissionSource, bundle, refresh_digest
from tests.runtime.helpers import RuntimeRig

IMAGE = "sha256:4206078324d29825cb9d98b342b9535623fc052a818a3a4ed270b648e055e279"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pointer(path):
    return {"path": str(path), "sha256": sha(path)}


class Provisioned:
    def __init__(self, root, *, new_file=None, cover_new_file=False):
        root.mkdir(mode=0o700)
        self.root = root
        self.runtime = RuntimeRig(root / "runtime")
        self.r = self.runtime.rig
        self.r.now = time.time()
        repo = self.runtime.repo
        self.script = "from pathlib import Path\nassert Path('src/a.txt').read_text() == '4'\n"
        if cover_new_file:
            self.script += f"assert Path({new_file!r}).read_text() == 'new input'\n"
        for name, text in [("verify.py", self.script), ("src/a.txt", "0")]:
            path = repo / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Authored baseline with pinned verifier")
        self.base = git(repo, "rev-parse", "HEAD")
        git(repo, "branch", "integration", self.base)
        self.dispatch = self.runtime.start(self.r.queued(code=True))
        self.runtime.admit(self.dispatch)
        self.runtime.supervisor.execute(
            self.r.handle,
            self.dispatch,
            self.runtime.command(
                self.dispatch,
                "from pathlib import Path; import subprocess; Path('src/a.txt').write_text('4'); "
                + (f"Path({new_file!r}).write_text('new input'); " if new_file else "")
                + "subprocess.run(['git','add','.'],check=True); "
                "subprocess.run(['git','commit','-qm','Owned authored change'],check=True)",
            ),
            operation_key=self.r.key(),
        )
        self.attempt = self.r.core.get_attempt(self.r.handle, self.dispatch.attempt_id)
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter

        routes = [
            route for route in self.r.core.context.evidence.routes if route.prefix != "fixture:"
        ]
        self.r.core.context.evidence = EvidenceRouter(
            [
                *routes,
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
        self.pair = self.dispatch.resources
        self.bundle = b = bundle(root / "native", task_id=self.task.id)
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.policy = root / "code-policy.json"
        write_private(
            self.policy,
            {
                "reference": "code",
                "scope": asdict(self.r.scope),
                "task_id": self.task.id,
                "prose_digest": self.task.prose_digest,
                "target_ref": "refs/heads/integration",
                "require_human_acceptance": False,
            },
        )
        self.catalog = root / "checks.json"
        write_private(
            self.catalog,
            {
                "format": 2,
                "groups": [
                    {
                        "labels": ["Checks"],
                        "category": "verification",
                        "runner": "installed-python-script",
                        "resources": ["application-database"],
                        "required": True,
                        "prerequisites": [],
                    }
                ],
            },
        )
        self.docker = Path("/usr/local/bin/docker").resolve()
        endpoint = subprocess.check_output(
            [
                str(self.docker),
                "context",
                "inspect",
                "--format",
                '{{(index .Endpoints "docker").Host}}',
            ],
            text=True,
            timeout=5,
        ).strip()
        endpoint_path = Path(endpoint[7:]).resolve()
        info = endpoint_path.stat()
        self.commands = root / "commands.json"
        write_private(
            self.commands,
            {
                "version": 1,
                "manifest_path": str(self.commands),
                "task_id": self.task.id,
                "prose_digest": self.task.prose_digest,
                "docker_executable": str(self.docker),
                "docker_sha256": sha(self.docker),
                "docker_endpoint": "unix://" + str(endpoint_path),
                "endpoint_identity": {
                    "path": str(endpoint_path),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                },
                "image_id": IMAGE,
                "commands": [
                    {
                        "reference": "verify",
                        "recipe": "python-script",
                        "script": "verify.py",
                        "script_sha256": hashlib.sha256(self.script.encode()).hexdigest(),
                        "files": ["verify.py", "src/a.txt"]
                        + ([new_file] if cover_new_file else []),
                        "seconds": 8,
                    }
                ],
            },
        )
        selected = Executable.capture(
            "/usr/bin/git", GitProfile.environment(root / "temporary-home")
        )
        self.manifest = root / "code.json"
        write_private(
            self.manifest,
            {
                "version": 1,
                "manifest_path": str(self.manifest),
                "policy": pointer(self.policy),
                "git": {
                    "root": str(root),
                    "executable": selected.path,
                    "sha256": selected.sha256,
                    "exec_manifest": selected.exec_manifest,
                },
                "catalog": pointer(self.catalog),
                "units": [{"name": "verify", "group": "checks", "command_ref": "verify"}],
                "required_hooks": [],
                "attempts": 1,
            },
        )
        spec = json.loads(b.installation.read_text())
        spec.update(
            repository=str(repo),
            state_root=str(self.state),
            scope=asdict(self.r.scope),
            prose_digest=self.task.prose_digest,
            code_ref="code",
            files=["src/a.txt"] + ([new_file] if cover_new_file else []),
            deadline=time.time() + 300,
        )
        spec["code_digest"] = provisioning_digest(pointer(self.manifest), pointer(self.commands))
        write_private(b.installation, spec, replace=True)
        b.data.update(
            commands_manifest=pointer(self.commands), code_manifest=pointer(self.manifest)
        )
        b.data["installation"] = installation_intent(b.installation)
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        self.journal = HostJournal(self.state / "host.sqlite", self.state / "host.key")
        self.context = NativeContext(
            self.r.core,
            self.r.handle,
            self.task,
            self.runtime.journal,
            self.journal,
            self.runtime.resources,
            self.runtime.runtime,
            spec["deadline"],
        )
        self.code = None

    def resolve(self):
        self.registry = resolve(
            self.bundle.config, self.bundle.installation, authority_source=PermissionSource()
        )
        self.code = self.registry.require_code("code")
        return self.code

    def reseal(self):
        b = self.bundle
        spec = json.loads(b.installation.read_text())
        spec["code_digest"] = provisioning_digest(pointer(self.manifest), pointer(self.commands))
        write_private(b.installation, spec, replace=True)
        b.data.update(
            commands_manifest=pointer(self.commands), code_manifest=pointer(self.manifest)
        )
        b.data["installation"] = installation_intent(b.installation)
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)

    def prepare(self):
        code = self.resolve()
        self.capability = cap = code.prepare(self.context)
        tree_id = resource_alias("worktree", self.pair.worktree_id)
        repo_id = resource_alias("repository", str(self.runtime.repo))
        record = cap.profile.enroll(
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
            code.fingerprint,
            tree_id,
            repo_id,
            resource_alias("database", self.pair.database_id),
            record["configuration_digest"],
            "provisioned-candidate",
        )
        self.grant = CandidateGrant(
            binding,
            "refs/heads/managed/" + self.attempt.id,
            git(self.pair.worktree, "rev-parse", "HEAD"),
            self.base,
            self.task.version,
            self.attempt.version,
            self.task.decision_revision,
            self.r.decision(self.task, "In Dev"),
            "owned-lead",
        )
        root = self.state / "merge"
        root.mkdir(mode=0o700)
        (root / "candidates").mkdir(mode=0o700)
        self.candidate_profile = CandidateProfile(
            cap.profile, tree_id, repo_id, root / "candidates"
        )
        self.candidates = CandidateService(
            Journal(root / "journal.sqlite"),
            self.candidate_profile,
            CoreCandidateAuthority(self.r.core, self.r.handle, self.runtime.resources, self.pair),
            process_factory=CandidateProcesses,
        )
        self.candidate = self.candidates.prepare(self.grant)
        assert self.candidate.status == "ready", self.candidate
        return cap

    def close(self):
        if hasattr(self, "candidates"):
            for ref in tuple(self.candidates.runner.live):
                self.candidates.runner.reconcile(ref)
            assert not self.candidates.runner.live
        self.runtime.close()
        assert all(w.process.returncode is not None for w in self.runtime.runtime.workers.values())
