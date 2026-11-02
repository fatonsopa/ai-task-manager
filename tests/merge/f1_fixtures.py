"""Authored, isolated real-Git fixtures; no application source or credentials."""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.projects.ai_index.contracts import Binding
from ai_task_manager.adapters.projects.ai_index.git_profile import Executable, GitProfile
from ai_task_manager.adapters.projects.ai_index.journal import Journal
from ai_task_manager.adapters.projects.ai_index.local_git import LocalGit, Publication
from ai_task_manager.adapters.projects.ai_index.operations import Operations
from ai_task_manager.adapters.projects.ai_index.registrations import Registrations


class FixtureAuthority:
    """Fixture-only port. It establishes no actual Core/native authority evidence."""

    def registration(self, binding):
        binding.validate()

    def before_start(self, binding):
        binding.validate()

    def check_current(self, binding):
        binding.validate()

    @contextmanager
    def authority_boundary(self, binding):
        binding.validate()
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


class Rig:
    def __init__(self, root, binary, *, mode="cas", executable=None):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = GitProfile.environment(self.home)
        self.binary = str(binary)
        self.repo = self.root / "repository"
        self.repo.mkdir(mode=0o700)
        self.git("init", "-q", "-b", "dev")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "a.txt").write_text("old\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "CG-0: authored base")
        self.old = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "-qb", "candidate")
        (self.repo / "a.txt").write_text("new\n")
        self.git("commit", "-qam", "CG-0: authored checked work")
        self.new = self.git("rev-parse", "HEAD").stdout.decode().strip()
        if mode == "fast_forward":
            self.git("checkout", "-q", "dev")
        self.executable = executable or Executable.capture(self.binary, self.env)
        self.profile = GitProfile(root=self.root, executable=self.executable, home=self.home)
        self.profile.enroll(
            self.repo, worktree_id="fixture-worktree", common_git_id="fixture-common"
        )
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.journal = Journal(state / "operations.sqlite")
        self.authority = FixtureAuthority()
        self.binding = Binding(
            "fixture-task",
            str(uuid4()),
            "fixture-principal",
            "fixture-session",
            "fixture-team",
            "fixture-project",
            "fixture-issue",
            "a" * 64,
            "b" * 64,
            "fixture-worktree",
            "fixture-common",
            "fixture-resource",
            "fixture-command",
            str(uuid4()),
        )
        self.ref = Registrations(self.journal, self.authority).register(self.binding)["ref"]
        self.request = Publication(
            "refs/heads/candidate",
            "refs/heads/dev",
            self.old,
            self.new,
            self.new,
            "fixture-range",
            mode,
        )
        # Explicit preconditions for this component, not a fake live launch/check receipt.
        op = self.journal.current(self.ref)
        facts = {
            "launch_intent": True,
            "launched": {"process_ref": "fixture-outer", "children": []},
            "checks": {"head": self.new, "outcome": "pass"},
            "publication_intent": {
                "old": self.old,
                "new": self.new,
                "checked": self.new,
                "range_ref": "fixture-range",
            },
        }
        self.journal.commit(
            self.ref, op["version"], "fixture-preconditions", "fixture_preconditions", facts
        )
        self.operations = Operations(self.journal, self.authority)
        self.adapter = LocalGit(self.journal, self.profile, self.operations)
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

    def publish(self, **kwargs):
        return self.adapter.publish(
            self.ref,
            self.journal.current(self.ref)["version"],
            "publication",
            self.request,
            **kwargs,
        )

    def dev(self):
        return self.git("rev-parse", "refs/heads/dev").stdout.decode().strip()

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

        from ai_task_manager.adapters.projects.ai_index.local_git import _Drain

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
