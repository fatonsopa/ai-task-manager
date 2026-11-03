"""A disposable gateway; it cannot delegate Core work or arbitrary commands."""

import os
import stat
from pathlib import Path

from jsonschema import Draft202012Validator

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion

from ..gateway import SCHEMAS, tool_schema
from ..workspace import Workspace


class CanaryGateway:
    schemas = {name: tool_schema(name) for name in SCHEMAS}

    def __init__(self, context, authority, journal):
        self.context, self.authority, self.journal = context, authority, journal
        self._bound = (context, authority, journal)
        self.root = Path(context.fixture_root)
        self.native = self._native = None
        self.case = None
        self.events = []
        self._physical()
        authority.require_current(context, phase="fixture")
        self.workspace = Workspace(self.root)
        self.workspace.write("input.txt", digest(context.reference), None)
        self.initial = self.snapshot()

    def _physical(self):
        info = self.root.lstat()
        if (
            self.root.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
            != self.context.fixture_identity
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise AccessDenied("The original private canary directory changed")

    def snapshot(self):
        self._physical()
        result = {}
        for name in ("input.txt", "output.txt"):
            try:
                (self.root / name).lstat()
            except FileNotFoundError:
                result[name] = None
            else:
                result[name] = self.workspace.read(name)["sha256"]
        return result

    def bind(self, native):
        if self.native is not None:
            raise AccessDenied("A probe gateway cannot change native owner")
        self.native = self._native = native

    def begin(self, case):
        if self.case is not None:
            raise AccessDenied("A probe case is already active")
        self.case, self.events = case, []
        self.before = self.snapshot()

    def end(self):
        refs = tuple(self.events)
        self.case = None
        return refs

    def call(self, name, args):
        if (self.context, self.authority, self.journal) != self._bound or (
            self.native is None or self.native is not self._native or self.case is None
        ):
            raise AccessDenied("No current owned conformance case")
        self.authority.require_current(
            self.context, policy=self.native.policy, binding=self.native.binding, phase="canary"
        )
        self.native.verify_active()
        self._physical()
        before = self.snapshot()
        outcome, result = "unknown", None
        try:
            if name not in SCHEMAS or not isinstance(args, dict):
                raise AccessDenied("Unknown conformance operation")
            Draft202012Validator(tool_schema(name)).validate(args)
            with self.native.current_invocation().effect_boundary():
                self.authority.require_current(
                    self.context,
                    policy=self.native.policy,
                    binding=self.native.binding,
                    phase="canary",
                )
                self.native.process.check_current_liveness()
                result = self._perform(name, args)
            outcome = "succeeded"
            return result
        except (AccessDenied, StaleVersion):
            outcome = "denied"
            raise
        finally:
            self.events.append(
                self.journal._append(
                    "trace",
                    {
                        "effect": "native_probe_gateway",
                        "probe": self.context.reference,
                        "case_id": self.case.case_id,
                        "tool": name,
                        "arguments_hash": digest(args),
                        "outcome": outcome,
                        "result_hash": digest(result),
                        "before": before,
                        "after": self.snapshot(),
                    },
                )
            )

    def _perform(self, name, args):
        if name == "read_file":
            result = self.workspace.read(args["path"])
        elif name == "write_file":
            result = self.workspace.write(args["path"], args["text"], args["expected_hash"])
        elif name == "run_command":
            if args["command_ref"] != "canary-check":
                raise AccessDenied("The conformance command is not registered")
            # A fixed in-process canary operation; this is not shell or
            # normal project-command execution acceptance.
            result = {"command_ref": "canary-check", "input": self.workspace.read("input.txt")}
        else:
            if (args["event_id"], args["challenge"]) != ("probe", digest(self.context.reference)):
                raise AccessDenied("The conformance challenge differs")
            result = {"acknowledged": "probe"}
        return result
