"""Owned installed code controls with synthetic host/provider and native review frames."""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGitContainer
from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.application.merging.workflows import CandidateChecks
from ai_task_manager.config.files import write_private
from ai_task_manager.installation.commands import CommandDefinition, build
from ai_task_manager.installation.workflows import NativeAuthorities
from ai_task_manager.installation.workflows.code_registration import (
    CodeCapabilities,
    CodeRegistration,
)
from ai_task_manager.storage.checks import CheckStore
from tests.checks.fixtures import Guard, catalogs, discovery, sha, unit
from tests.hosts.test_composition import AuthoredPermits
from tests.merge.workflows.helpers import OutsideExecutor

IMAGE = "sha256:4206078324d29825cb9d98b342b9535623fc052a818a3a4ed270b648e055e279"


def initialize(repo):
    import subprocess

    for name, text in [
        ("tests/a.txt", "4"),
        ("config/checks.json", "{}"),
        ("verify.py", "from pathlib import Path\nassert Path('src/a.txt').read_text() == '4'\n"),
    ]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "Independent code fixture"],
        check=True,
        capture_output=True,
    )


def worker_authority(context, policy, permits, accounting):
    docker = Path("/usr/local/bin/docker").resolve()
    endpoint = "unix://" + str(Path.home().joinpath(".docker/run/docker.sock").resolve())
    script = context.resources.git.repository / "verify.py"
    definitions = (
        CommandDefinition(
            "verify", "verify.py", sha(script.read_bytes()), ("verify.py", "src/a.txt")
        ),
    )
    commands = build(
        context,
        docker_executable=str(docker),
        docker_endpoint=endpoint,
        image_id=IMAGE,
        definitions=definitions,
        check_sources=lambda: None,
    )
    container = WorkerGitContainer(
        context.runtime_journal,
        context.runtime_journal.path.parent / "git-containers",
        docker=docker,
        docker_sha256=sha(docker.read_bytes()),
        endpoint=endpoint,
        image=IMAGE,
        resource_root=context.resources.root,
        deadline=context.deadline,
        run_id="authored-installed-code-control",
    )
    return NativeAuthorities(
        policy,
        permits,
        accounting,
        "synthetic-accounting",
        confined_commands=commands,
        command_refs=("verify",),
        worker_git=ConfinedWorkerGit(context.runtime_journal, container),
    )


def registration(root, rig):
    task = rig.task
    path = root / "code-policy.json"
    write_private(
        path,
        {
            "reference": "installed-code",
            "scope": asdict(rig.rig.scope),
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "target_ref": "refs/heads/integration",
            "require_human_acceptance": False,
        },
    )
    home = root / "merge-git-home"
    home.mkdir(mode=0o700)
    profile = GitProfile(
        root=root,
        executable=Executable.capture("/usr/bin/git", GitProfile.environment(home)),
        home=home,
    )
    executor = OutsideExecutor(root / "merge-check-evidence")
    code = None

    def prepare(context):
        guard = Guard(source=code.fingerprint)
        checks = MergeChecks(
            CheckStore(root / "merge-checks/results.sqlite"),
            guard,
            executor,
            catalogs=catalogs(root, project=rig.rig.scope.project),
        )

        def discover(candidate):
            guard.root = executor.root = Path(candidate.path)
            return discovery((unit(),))

        return CodeCapabilities(profile, CandidateChecks(checks, discover))

    code = CodeRegistration(path, prepare)
    return code


def review_factory(runtime, root):
    from tests.workflow_review_capture.helpers import reviewer

    producers = []

    def factory(inspection):
        class Artifacts:
            def prepare(self, *args):
                return inspection

        task = runtime.rig.core.get_task(runtime.rig.handle, inspection.context["task_id"])
        author = SimpleNamespace(
            r=runtime.rig,
            runtime=runtime,
            artifacts=Artifacts(),
            task=task,
            scope=runtime.rig.scope,
            saved={
                "digest": inspection.context["inspection"]["digest"]
                if "digest" in inspection.context["inspection"]
                else "unused"
            },
        )
        verdict = {
            "context_digest": inspection.digest,
            "criteria": [
                {"id": c[0], "judgment": "accept", "reason": "Authored synthetic reviewer control"}
                for c in inspection.context["criteria"]
            ],
            "reason": "Synthetic protocol control, not genuine model acceptance",
        }
        _, producer = reviewer(
            author,
            root / ("reviewer-" + str(len(producers))),
            verdict,
            host="claude",
            model="claude-opus-5-5",
        )
        producers.append(producer)
        return producer

    factory.producers = producers
    return factory


class ClaudePermits(AuthoredPermits):
    def check_admission(self, host):
        assert host == "claude" and not self.deny

    def authorize_neutral_start(self, policy, binding):
        return self.accounting.claim(
            policy,
            binding,
            "neutral",
            parent=self.accounting.hosts[binding.attempt_id][0].binding.call_id,
            maximum=8,
        )

    def record_neutral_start(self, permit, *, outcome, result, observation_ref):
        self.accounting.complete(permit, outcome)
