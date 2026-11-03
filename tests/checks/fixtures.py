"""Authored inputs and real confined interpreter execution for checks tests."""

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, supervise
from ai_task_manager.application.merge_checks.catalogs import Catalogs, ProjectCatalog
from ai_task_manager.domains.checks.catalog import (
    Capabilities,
    ResourceCapability,
    RunnerCapability,
    validate_catalog,
)
from ai_task_manager.domains.checks.models import (
    Availability,
    Change,
    Discovery,
    Fact,
    Scope,
    Unit,
    fingerprint,
)
from ai_task_manager.domains.checks.results import Execution
from ai_task_manager.domains.checks.selection import select
from ai_task_manager.domains.work.errors import AccessDenied

SOURCE = "a" * 64
SCOPE = Scope("principal", "project", "session", "task")
BASE, HEAD = "1" * 40, "2" * 40


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


PYTHON_DIGEST = sha(Path(sys.executable).resolve().read_bytes())
FIXTURE_ENV = {"PATH": os.defpath, "LC_ALL": "C"}


def capabilities():
    return Capabilities(
        (
            RunnerCapability(
                "file-assert",
                ("text-consistency", "syntax"),
                ("source", "test", "config", "tool", "environment"),
                sha(CHILD),
            ),
        ),
        (
            ResourceCapability("scratch", ("tree",), sha("owned file tree")),
            ResourceCapability("relational", ("schema", "seed", "history"), sha("sqlite")),
        ),
    )


def catalog_data():
    return {
        "format": 2,
        "groups": [
            {
                "labels": ["Checks A", "Alias A"],
                "category": "text-consistency",
                "runner": "file-assert",
                "resources": [],
                "required": False,
                "prerequisites": [],
            },
            {
                "labels": ["Checks B"],
                "category": "text-consistency",
                "runner": "file-assert",
                "resources": [],
                "required": False,
                "prerequisites": [],
            },
            {
                "labels": ["Checks C"],
                "category": "syntax",
                "runner": "file-assert",
                "resources": [],
                "required": False,
                "prerequisites": [],
            },
        ],
    }


def catalog(*, prerequisites=()):
    data = catalog_data()
    for key, parents in prerequisites:
        next(g for g in data["groups"] if g["labels"][0].casefold() == key)["prerequisites"] = list(
            parents
        )
    return validate_catalog(data, fingerprint(data), capabilities())


def catalogs(root, *, prerequisites=(), project="project", data=None, caps=None):
    data = data or catalog_data()
    for key, parents in prerequisites:
        next(g for g in data["groups"] if g["labels"][0].casefold() == key)["prerequisites"] = list(
            parents
        )
    path = root / (project + "-checks.json")
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return Catalogs(
        (ProjectCatalog(project, str(path), sha(path.read_bytes()), caps or capabilities()),)
    )


def unit(name="a", group="checks a", *, value="4", db=None):
    facts = [
        Fact("source", f"src/{name}.txt", sha(value)),
        Fact("test", f"tests/{name}.txt", sha("4")),
        Fact("config", "config/checks.json", sha("{}")),
        Fact("tool", "python", PYTHON_DIGEST),
        Fact("tool", "fixture-command", sha(CHILD)),
        Fact("environment", "fixture", fingerprint(FIXTURE_ENV)),
    ]
    facts.extend(db or [Fact("resource", "scratch/tree", sha("authored tree"))])
    return Unit(name, group, tuple(facts))


def discovery(units=None, *, status=Availability.KNOWN):
    units = tuple(units if units is not None else (unit(),))
    paths = {"unrelated.txt"}
    paths.update(f.name for u in units for f in u.inputs if f.kind in {"source", "test", "config"})
    return Discovery(
        status, units, tuple(sorted(paths)), sha("authored dependency producer" + CHILD)
    )


def change(paths=("src/a.txt",), *, status=Availability.KNOWN, head=HEAD):
    return Change(status, tuple(paths), BASE, head)


def plan(
    units=None, *, changes=None, scope=SCOPE, attempt="attempt", source=SOURCE, prerequisites=()
):
    return select(
        catalog(prerequisites=prerequisites),
        scope,
        attempt,
        source,
        changes or change(),
        discovery(units),
    )


def execution(plan, group=None, names=None, **changes):
    group = group or plan.groups[0].key
    names = tuple(names if names is not None else (u.name for u in plan.units if u.group == group))
    value = Execution(
        plan.digest,
        group,
        names,
        names,
        names,
        (),
        0,
        "completed",
        True,
        "process:authored",
        sha("authored output"),
        10.0,
        11.0,
    )
    return replace(value, **changes)


def proof(plan, item, **changes):
    value = {
        "ref": "proof:authored",
        "scope": {
            "principal": plan.scope.principal,
            "project": plan.scope.project,
            "session": plan.scope.session,
            "task": plan.scope.task,
        },
        "unit": item.name,
        "group": item.group,
        "head": plan.head,
        "input_digest": item.input_digest,
        "catalog_digest": plan.catalog_digest,
        "binding_digest": plan.binding_digest,
        "producer_digest": plan.producer_digest,
        "source_digest": plan.source_digest,
        "physical_origin": "result:authored",
        "result_ref": "result:authored",
        "passed": True,
    }
    return dict(value, **changes)


class Guard:
    def __init__(self, *, source=SOURCE, root=None):
        self.source, self.root, self.calls = source, root, 0

    def verify(self, plan):
        self.calls += 1
        if plan.source_digest != self.source:
            raise AccessDenied("Configured check source changed")
        if self.root is not None:
            for item in plan.units:
                for fact in item.inputs:
                    if fact.kind in {"source", "test", "config"}:
                        path = self.root / fact.name
                        actual = sha(path.read_bytes()) if path.exists() else "absent"
                        if actual != fact.value:
                            raise AccessDenied("Fixture input changed")
                    elif fact.kind == "tool" and fact.name == "python":
                        if sha(Path(sys.executable).resolve().read_bytes()) != fact.value:
                            raise AccessDenied("Fixture interpreter changed")
                    elif fact.kind == "tool" and fact.name == "fixture-command":
                        if sha(CHILD) != fact.value:
                            raise AccessDenied("Fixture command changed")
                    elif fact.kind == "environment" and fact.name == "fixture":
                        if fingerprint(FIXTURE_ENV) != fact.value:
                            raise AccessDenied("Fixture environment changed")


class RecordingExecutor:
    def __init__(self, callback=None):
        self.calls, self.callback = [], callback

    def execute(self, plan, group, names):
        self.calls.append((group, names))
        if self.callback:
            return self.callback(plan, group, names)
        return execution(plan, group, names)


CHILD = """
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); names=json.loads(sys.argv[2]); failed=[]; passed=[]
for name in names:
    actual=(root/'src'/f'{name}.txt').read_text()
    expected=(root/'tests'/f'{name}.txt').read_text()
    try:
        assert actual == expected
    except AssertionError:
        failed.append([name,hashlib.sha256(b'fixture equality assertion').hexdigest()])
    else:
        passed.append(name)
print(json.dumps({'ran':names,'passed':passed,'failures':failed}))
raise SystemExit(1 if failed else 0)
"""


class FixtureExecutor:
    """Real authored file assertions; these fixtures grant no application runner authority."""

    def __init__(self, root, *, child_script=CHILD, resource_refs=()):
        self.root, self.records = root, []
        self.child_script, self.resource_refs = child_script, resource_refs

    def execute(self, plan, group, names):
        output = self.root / ("output-" + str(uuid4()) + ".json")
        intent = output.with_suffix(".intent.json")
        intent.write_text(
            json.dumps(
                {"plan_digest": plan.digest, "group": group, "units": names, "state": "prepared"}
            )
        )
        started = time.monotonic()
        with output.open("xb") as stream:
            output.chmod(0o600)
            child = subprocess.Popen(
                [sys.executable, "-I", "-c", self.child_script, str(self.root), json.dumps(names)],
                cwd=self.root,
                env=dict(FIXTURE_ENV),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
            owned = OwnedProcessGroup(child)
            initial = owned.capture()
            output.with_suffix(".started.json").write_text(
                json.dumps(
                    {
                        "pid": child.pid,
                        "group_id": owned.group_id,
                        "start_identity": owned.start_identity,
                        "initial": initial.record(),
                    }
                )
            )
            completion = supervise(owned, timeout_seconds=5)
        ended = time.monotonic()
        raw = output.read_bytes()
        if len(raw) > 262144:
            raise AssertionError("Authored output exceeded its frozen limit")
        record = {
            "pid": child.pid,
            "completion": completion,
            "output": str(output),
            "sha256": sha(raw),
            "bytes": len(raw),
        }
        self.records.append(record)
        output.with_suffix(".observation.json").write_text(json.dumps(record, indent=2))
        (self.root / "process-observations.json").write_text(json.dumps(self.records, indent=2))
        summary = json.loads(raw)
        return Execution(
            plan.digest,
            group,
            names,
            tuple(summary["ran"]),
            tuple(summary["passed"]),
            tuple(tuple(v) for v in summary["failures"]),
            completion["returncode"],
            "completed" if completion["outcome"] in {"succeeded", "failed"} else "unknown",
            True,
            f"process:{child.pid}",
            sha(raw),
            started,
            ended,
            resource_refs=self.resource_refs,
        )


def write_inputs(root, units):
    for item in units:
        for fact in item.inputs:
            if fact.kind in {"source", "test", "config"}:
                path = root / fact.name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text("{}" if fact.kind == "config" else "4")


def private_dir(root):
    Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(root).chmod(0o700)
    return Path(root)
