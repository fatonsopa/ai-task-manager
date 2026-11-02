"""Authored, isolated real-Git fixtures; no application source or credentials."""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.git.profile import Executable, GitProfile
from ai_task_manager.adapters.git.publication import LocalGit
from ai_task_manager.application.merging.service import MergeService
from ai_task_manager.domains.merging.contracts import Binding, Observation, Publication, encode
from ai_task_manager.storage.merging.journal import Journal


class FixtureAuthority:
    """Fixture-only port. It establishes no actual Core/native authority evidence."""

    def __init__(self, project="fixture-project"):
        self.project, self.active = project, True

    def registration(self, binding):
        binding.validate()
        if binding.project_id != self.project:
            from ai_task_manager.domains.work.errors import AccessDenied

            raise AccessDenied("Foreign fixture project")

    def before_start(self, binding):
        binding.validate()

    def check_current(self, binding):
        self.registration(binding)
        if not self.active:
            from ai_task_manager.domains.work.errors import AccessDenied

            raise AccessDenied("Fixture hold")

    @contextmanager
    def authority_boundary(self, binding):
        self.check_current(binding)
        yield


class FaultOnce:
    def __init__(self, stage, action=None):
        self.stage, self.action, self.calls = stage, action, 0

    def hit(self, stage):
        if stage == self.stage and not self.calls:
            self.calls += 1
            if self.action:
                self.action()
            else:
                raise RuntimeError("authored lost observation")


class FixtureChecks:
    """Actual authored diff checks, scoped fixture receipts; no product acceptance issuer."""

    def __init__(self, rig):
        self.rig, self.records, self.calls = rig, {}, []
        self.unavailable = False

    def record(self, ref, binding, request):
        result = self.rig.git("diff", "--check", request.old, request.new)
        key = "authored-check:" + str(uuid4())
        self.records[key] = Observation(
            ref,
            binding.fingerprint,
            key,
            "checks",
            encode(
                {
                    "head": request.new,
                    "permitted": result.returncode == 0,
                    "contract_digest": "c" * 64,
                    "source_digest": binding.source_digest,
                }
            ),
        )
        return key

    def read(self, key, binding):
        from ai_task_manager.domains.work.errors import AccessDenied

        self.calls.append(key)
        if self.unavailable or key not in self.records:
            raise AccessDenied("Authored check evidence unavailable")
        return self.records[key]


class Rig:
    def __init__(
        self, root, binary, *, mode="cas", executable=None, target="dev", project="fixture-project"
    ):
        self.target, self.project = target, project
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = GitProfile.environment(self.home)
        self.binary = str(binary)
        self.repo = self.root / "repository"
        self.repo.mkdir(mode=0o700)
        self.git("init", "-q", "-b", self.target)
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "a.txt").write_text("old\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "authored base")
        self.old = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "-qb", "candidate")
        (self.repo / "a.txt").write_text("new\n")
        self.git("commit", "-qam", "authored checked work")
        self.new = self.git("rev-parse", "HEAD").stdout.decode().strip()
        if mode == "fast_forward":
            self.git("checkout", "-q", self.target)
        self.executable = executable or Executable.capture(self.binary, self.env)
        self.profile = GitProfile(root=self.root, executable=self.executable, home=self.home)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.journal = Journal(state / "operations.sqlite")
        self.authority = FixtureAuthority(self.project)
        self.request = Publication(
            "refs/heads/candidate", self.old, self.new, self.new, "fixture-range", mode
        )
        self.checks = FixtureChecks(self)
        self.rebind(self.repo, initial=True)
        self.foreign = self.root.parent / (self.root.name + "-foreign.txt")
        self.foreign.write_bytes(b"independent foreign sentinel\n")
        self.initial_foreign = self.foreign.read_bytes()

    def git(self, *args, ok=(0,)):
        r = subprocess.run(
            [self.binary, "-C", str(self.repo), *args],
            env=self.env,
            capture_output=True,
            timeout=10,
        )
        if r.returncode not in ok:
            raise AssertionError((args, r.returncode, r.stderr.decode("utf-8", "replace")))
        return r

    def rebind(self, path, *, initial=False):
        if not initial:
            self.profile = GitProfile(root=self.root, executable=self.executable, home=self.home)
        record = self.profile.enroll(
            path,
            worktree_id="fixture-worktree",
            repository_id="fixture-common",
            target_ref="refs/heads/" + self.target,
        )
        self.binding = Binding(
            "fixture-task",
            str(uuid4()),
            "fixture-principal",
            "fixture-session",
            self.project,
            "a" * 64,
            "b" * 64,
            "fixture-worktree",
            "fixture-common",
            "fixture-resource",
            record["configuration_digest"],
            str(uuid4()),
        )
        self.operations = MergeService(
            self.journal,
            self.authority,
            self.checks,
            publisher_factory=lambda boundary: LocalGit(self.journal, self.profile, boundary),
        )
        self.adapter = self.operations.publisher
        self.ref = self.operations.register(self.binding)["ref"]
        self.check_ref = self.checks.record(self.ref, self.binding, self.request)
        self.operations.prepare(self.ref, 0, "fixture-prepare", self.request, self.check_ref)

    def publish(self, **kwargs):
        return self.adapter.publish(
            self.ref,
            self.journal.current(self.ref)["version"],
            "publication",
            self.request,
            **kwargs,
        )

    def publish_through_service(self):
        return self.operations.publish(
            self.ref,
            self.journal.current(self.ref)["version"],
            "publication",
            self.request,
            self.check_ref,
        )

    def dev(self):
        return self.git("rev-parse", "refs/heads/" + self.target).stdout.decode().strip()

    def verify_closed(self):
        assert self.foreign.read_bytes() == self.initial_foreign
        for ref in list(self.adapter._live):
            result = self.adapter.reconcile(ref)
            assert result["quiescent"], result
        assert not self.adapter._live
        op = self.journal.current(self.ref)
        data = op["facts"].get("local_git")
        if data and data.get("process"):
            result = data.get("result")
            assert result and result["quiescent"], data
            for child in data.get("children", [data["process"]]):
                assert child.get("quiescence", {}).get("quiescent"), child
                ps = subprocess.run(
                    ["/bin/ps", "-p", str(child["pid"]), "-o", "lstart="],
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                    capture_output=True,
                    timeout=2,
                )
                assert not ps.stdout.strip(), ps.stdout

    def drain_control(self, args):
        """Owned real-Git output control, separate from publication receipts."""
        import time

        from ai_task_manager.adapters.git.publication import _Drain

        process = subprocess.Popen(
            self.profile.argv(self.repo, *args),
            env=self.profile.environment_for(self.repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        initial = LocalGit._inventory(process.pid)
        record = {
            "pid": process.pid,
            "group": process.pid,
            "initial": initial,
            "purpose": "real Git output drain control, not publication evidence",
        }
        path = self.root / "drain-control-process.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        path.chmod(0o600)
        stdout, stderr = _Drain(process.stdout), _Drain(process.stderr)
        deadline = time.monotonic() + 10
        while True:
            observation = LocalGit._quiescence(process, initial)
            if (
                observation.get("quiescent")
                or not observation["known"]
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        record["quiescence"] = observation
        path.write_text(json.dumps(record, indent=2) + "\n")
        assert observation.get("quiescent"), record
        process.wait(timeout=0.5)
        stdout.thread.join(1)
        stderr.thread.join(1)
        record.update(stdout=stdout.snapshot(), stderr=stderr.snapshot())
        path.write_text(json.dumps(record, indent=2) + "\n")
        assert stdout.done.is_set() and stderr.done.is_set()
        ps = subprocess.run(
            ["/bin/ps", "-p", str(process.pid), "-o", "lstart="],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            capture_output=True,
            timeout=2,
        )
        assert not ps.stdout.strip()
        return record

    def events(self):
        return self.journal.events(self.ref)


def observe_profile_calls(profile, monkeypatch):
    """Record actual Git/config calls without replacing their results or effects."""
    query, run = profile.query, subprocess.run
    observed = {"queries": [], "commands": []}

    def record_query(repo, *args, **kwargs):
        observed["queries"].append(list(args))
        return query(repo, *args, **kwargs)

    def record_run(argv, *args, **kwargs):
        if argv[0] == profile.executable.path:
            observed["commands"].append({"argv": list(argv), "cwd": str(kwargs.get("cwd"))})
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(profile, "query", record_query)
    monkeypatch.setattr(subprocess, "run", record_run)
    return observed
