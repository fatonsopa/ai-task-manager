"""Two independent authored projects use the same configured consumer and durable store."""

import json
import os
import sqlite3
from dataclasses import replace

import pytest

from ai_task_manager.application.merge_checks.catalogs import Catalogs, ProjectCatalog
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.domains.checks.catalog import (
    Capabilities,
    ResourceCapability,
    RunnerCapability,
    validate_catalog,
)
from ai_task_manager.domains.checks.models import Availability, Discovery, Fact, Scope, Unit
from ai_task_manager.domains.checks.reuse import eligible
from ai_task_manager.domains.checks.selection import select
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, SchemaError
from ai_task_manager.storage.checks import CheckStore
from tests.checks.fixtures import (
    CHILD,
    PYTHON_DIGEST,
    SOURCE,
    FixtureExecutor,
    Guard,
    RecordingExecutor,
    capabilities,
    catalog_data,
    catalogs,
    change,
    discovery,
    execution,
    fingerprint,
    plan,
    private_dir,
    proof,
    sha,
    unit,
    write_inputs,
)

SQL_CHILD = """
import hashlib,json,pathlib,sqlite3,sys
root=pathlib.Path(sys.argv[1]);names=json.loads(sys.argv[2]);failed=[];passed=[]
with sqlite3.connect('file:'+str(root/'records.sqlite')+'?mode=ro',uri=True) as db:
    for name in names:
        try:
            assert db.execute('SELECT count(*) FROM records WHERE amount < 0').fetchone()[0] == 0
        except AssertionError:
            failed.append([name,hashlib.sha256(b'nonnegative amounts').hexdigest()])
        else:
            passed.append(name)
print(json.dumps({'ran':names,'passed':passed,'failures':failed}))
raise SystemExit(1 if failed else 0)
"""


def resource_facts(root, relational):
    if relational:
        with sqlite3.connect("file:" + str(root / "records.sqlite") + "?mode=ro", uri=True) as db:
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            seed = db.execute("SELECT amount FROM records ORDER BY rowid").fetchall()
        return tuple(
            Fact("resource", "ephemeral-relations/" + key, value)
            for key, value in (
                ("schema", fingerprint(schema)),
                ("seed", fingerprint(seed)),
                ("history", sha((root / "migration.sql").read_bytes())),
            )
        )
    tree = [(name, sha((root / name).read_bytes())) for name in ("src/a.txt", "tests/a.txt")]
    return (Fact("resource", "workspace-tree/tree", fingerprint(tree)),)


def configured(root, project, *, relational=False):
    root = private_dir(root)
    script = SQL_CHILD if relational else CHILD
    category, runner, resource = (
        ("relation-invariant", "sql-assert", "ephemeral-relations")
        if relational
        else ("document-equality", "text-assert", "workspace-tree")
    )
    keys = ("schema", "seed", "history") if relational else ("tree",)
    caps = Capabilities(
        (RunnerCapability(runner, (category,), ("source", "test", "tool"), sha(script)),),
        (ResourceCapability(resource, keys, sha("owned " + resource)),),
        ("contract",),
    )
    data = {
        "format": 2,
        "groups": [
            {
                "labels": ["contract"],
                "category": category,
                "runner": runner,
                "resources": [resource],
                "required": True,
                "prerequisites": [],
            }
        ],
    }
    path = root / "checks.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    binding = ProjectCatalog(project, str(path), sha(path.read_bytes()), caps)
    facts = [
        Fact("source", "src/a.txt", sha("4")),
        Fact("test", "tests/a.txt", sha("4")),
        Fact("tool", "python", PYTHON_DIGEST),
        Fact("tool", "fixture-command", sha(script)),
    ]
    item = Unit("a", "contract", tuple(facts))
    write_inputs(root, (item,))
    if relational:
        with sqlite3.connect(root / "records.sqlite") as db:
            db.execute("CREATE TABLE records(amount INTEGER NOT NULL)")
            db.execute("INSERT INTO records VALUES(4)")
        (root / "records.sqlite").chmod(0o600)
        (root / "migration.sql").write_text("CREATE TABLE records(amount INTEGER NOT NULL);\n")
    item = replace(item, inputs=(*item.inputs, *resource_facts(root, relational)))
    observed_path = root / "records.sqlite" if relational else root
    info = observed_path.stat()
    assert info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
    physical = f"fixture-resource:{info.st_dev}:{info.st_ino}"
    (root / "resource-observation.json").write_text(
        json.dumps(
            {
                "path": str(observed_path),
                "device": info.st_dev,
                "inode": info.st_ino,
                "owner": info.st_uid,
                "mode": oct(info.st_mode & 0o777),
                "physical_ref": physical,
                "resource": resource,
            },
            indent=2,
        )
    )
    return (
        binding,
        item,
        FixtureExecutor(root, child_script=script, resource_refs=((resource, physical),)),
    )


class TwoProjectGuard:
    def __init__(self, roots):
        self.roots = roots

    def verify(self, p):
        assert p.scope.project in self.roots
        root = self.roots[p.scope.project]
        for item in p.units:
            for fact in item.inputs:
                if fact.kind in {"source", "test"}:
                    if sha((root / fact.name).read_bytes()) != fact.value:
                        raise AccessDenied("Authored file changed")
        expected = {f.name: f.value for f in resource_facts(root, p.scope.project == "ledger")}
        script = SQL_CHILD if p.scope.project == "ledger" else CHILD
        for item in p.units:
            actual = {f.name: f.value for f in item.inputs if f.kind == "resource"}
            if actual != expected:
                raise AccessDenied("Actual owned resource semantics changed")
            tools = {f.name: f.value for f in item.inputs if f.kind == "tool"}
            if tools != {"python": PYTHON_DIGEST, "fixture-command": sha(script)}:
                raise AccessDenied("Fixture runner binding changed")


class TwoProjectExecutor:
    def __init__(self, executors):
        self.executors = executors

    def execute(self, p, group, names):
        return self.executors[p.scope.project].execute(p, group, names)


def run_two_projects(root, *, store_path=None):
    root = private_dir(root)
    file_binding, file_unit, file_exec = configured(root / "editorial", "editorial")
    sql_binding, sql_unit, sql_exec = configured(root / "ledger", "ledger", relational=True)
    registry = Catalogs((file_binding, sql_binding))
    store = CheckStore(store_path or root / "checks.sqlite")
    executor = TwoProjectExecutor({"editorial": file_exec, "ledger": sql_exec})
    guard = TwoProjectGuard({"editorial": root / "editorial", "ledger": root / "ledger"})
    service = MergeChecks(store, guard, executor, catalogs=registry)
    results = []
    for project, item in (("editorial", file_unit), ("ledger", sql_unit)):
        scope = Scope("principal", project, "session", "task")
        observed = Discovery(
            Availability.KNOWN, (item,), ("src/a.txt", "tests/a.txt"), sha("producer-" + project)
        )
        ref = service.prepare(scope, "physical", SOURCE, change(), observed, "first")
        first = service.run(ref, scope)
        assert first["outcome"] == "passed"
        row = first["groups"]["contract"]["result"]
        assert row["physical_count"] == 1 and row["carried_count"] == 0
        assert len(row["resource_refs"]) == 1
        restarted = MergeChecks(CheckStore(store.path), guard, executor, catalogs=registry)
        ref2 = restarted.prepare(scope, "carried", SOURCE, change(), observed, "second")
        second = restarted.run(ref2, scope)
        assert second["groups"]["contract"]["result"]["physical_count"] == 0
        assert second["groups"]["contract"]["result"]["carried_count"] == 1
        assert restarted.store.read(ref, scope)["events"] == first["events"]
        results.append(
            {
                "project": project,
                "first": ref,
                "second": ref2,
                "category": first["plan"].groups[0].category,
                "resource": row["resource_refs"],
                "physical": row["ran"],
                "carried": second["groups"]["contract"]["result"]["carried"],
            }
        )
    assert len(file_exec.records) == len(sql_exec.records) == 1
    assert results[0]["category"] != results[1]["category"]
    scope = Scope("principal", "ledger", "session", "task")
    with sqlite3.connect(root / "ledger/records.sqlite") as db:
        db.execute("UPDATE records SET amount=-1")
    # A stale declaration is refused before effect; a fresh changed declaration executes.
    old = Discovery(
        Availability.KNOWN, (sql_unit,), ("src/a.txt", "tests/a.txt"), sha("producer-ledger")
    )
    with pytest.raises(AccessDenied):
        service.prepare(scope, "stale-resource", SOURCE, change(), old, "stale")
    fresh = replace(
        sql_unit,
        inputs=tuple(f for f in sql_unit.inputs if f.kind != "resource")
        + resource_facts(root / "ledger", True),
    )
    changed = replace(old, units=(fresh,))
    failed_ref = service.prepare(scope, "changed-resource", SOURCE, change(), changed, "changed")
    failed = service.run(failed_ref, scope)
    assert failed["outcome"] == "failed"
    row = failed["groups"]["contract"]["result"]
    assert row["physical_count"] == 1 and row["carried_count"] == 0
    assert row["outcome"] == "assertion_failed"
    assert store.read(results[1]["first"], scope)["outcome"] == "passed"
    results.append(
        {
            "project": "ledger",
            "changed_resource": failed_ref,
            "outcome": row["outcome"],
            "physical": row["ran"],
        }
    )
    assert len(sql_exec.records) == 2
    for record in file_exec.records + sql_exec.records:
        group = record["completion"]["group"]
        assert group["confirmed_stop"] and group["observation"]["known"]
        assert not group["observation"]["live_pids"]
    (root / "two-project-demonstration.json").write_text(json.dumps(results, indent=2))
    return {
        "results": results,
        "processes": file_exec.records + sql_exec.records,
        "store": str(store.path),
    }


def test_two_unrelated_projects_same_consumer(tmp_path):
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("foreign owned sentinel")
    demo = run_two_projects(tmp_path / "owned")
    assert len(demo["processes"]) == 3
    assert sentinel.read_text() == "foreign owned sentinel"


@pytest.mark.parametrize("missing", ["runner_input", "resource_key", "unknown_value"])
def test_configured_input_requirements_refuse(missing):
    data = catalog_data()
    data["groups"][0]["resources"] = ["scratch"]
    cat = validate_catalog(data, fingerprint(data), capabilities())
    item = unit()
    if missing == "runner_input":
        item = replace(item, inputs=tuple(f for f in item.inputs if f.kind != "tool"))
    elif missing == "resource_key":
        item = replace(item, inputs=tuple(f for f in item.inputs if f.kind != "resource"))
    else:
        item = replace(
            item,
            inputs=tuple(
                replace(f, value=None) if f.kind == "resource" else f for f in item.inputs
            ),
        )
    with pytest.raises(EvidenceRequired):
        select(cat, Scope("p", "project", "s", "t"), "a", SOURCE, change(), discovery((item,)))


def test_required_inventory_and_selection():
    data = catalog_data()
    data["groups"][0]["required"] = True
    caps = replace(capabilities(), required_groups=("checks a",))
    cat = validate_catalog(data, fingerprint(data), caps)
    with pytest.raises(EvidenceRequired):
        select(cat, Scope("p", "project", "s", "t"), "a", SOURCE, change(()), discovery(()))
    p = select(
        cat, Scope("p", "project", "s", "t"), "a", SOURCE, change(("unrelated.txt",)), discovery()
    )
    assert [u.name for u in p.units] == ["a"]


@pytest.mark.parametrize("override", ["catalog", "prerequisites", "runner"])
def test_public_prepare_cannot_override_configuration(tmp_path, override):
    root = private_dir(tmp_path)
    service = MergeChecks(
        CheckStore(root / "checks.sqlite"), Guard(), RecordingExecutor(), catalogs=catalogs(root)
    )
    with pytest.raises(TypeError):
        service.prepare(
            Scope("p", "project", "s", "t"),
            "a",
            SOURCE,
            change(),
            discovery(),
            "key",
            **{override: {}},
        )
    with pytest.raises(AccessDenied):
        service.prepare(Scope("p", "foreign", "s", "t"), "a", SOURCE, change(), discovery(), "key")


def test_capability_digest_invalidates_reuse():
    p = plan()
    item = p.units[0]
    assert not eligible(replace(p, binding_digest=sha("other capability")), item, proof(p, item))


def test_post_effect_configuration_change_is_uncertain(tmp_path):
    root = private_dir(tmp_path)
    registry = catalogs(root)

    def mutate(p, g, n):
        path = root / "project-checks.json"
        path.write_text(path.read_text() + " ")
        return execution(p, g, n)

    store = CheckStore(root / "checks.sqlite")
    service = MergeChecks(store, Guard(), RecordingExecutor(mutate), catalogs=registry)
    scope = Scope("p", "project", "s", "t")
    ref = service.prepare(scope, "a", SOURCE, change(), discovery(), "key")
    result = service.run(ref, scope)
    assert result["outcome"] == "blocked"
    assert result["groups"]["checks a"]["result"]["physical_count"] == 1
    assert store.proofs(result["plan"], result["plan"].units[0]) == []


def test_old_draft_store_refused_without_mutation(tmp_path):
    root = private_dir(tmp_path)
    path = root / "checks.sqlite"
    store = CheckStore(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE check_meta SET version=1")
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        CheckStore(path)
    assert path.read_bytes() == before
    with pytest.raises(AccessDenied):
        store.read("missing", Scope("p", "project", "s", "t"))


@pytest.mark.parametrize("mutation", ["omitted", "foreign", "duplicate"])
def test_physical_resource_observations_must_match(tmp_path, mutation):
    from ai_task_manager.domains.checks.results import judge
    from ai_task_manager.domains.work.errors import ValidationError

    binding, item, _ = configured(tmp_path / "owned", "editorial")
    cat = Catalogs((binding,)).for_project("editorial")
    p = select(cat, Scope("p", "editorial", "s", "t"), "a", SOURCE, change(), discovery((item,)))
    refs = (
        ()
        if mutation == "omitted"
        else (
            (("foreign", "ref"),)
            if mutation == "foreign"
            else (("workspace-tree", "ref"), ("workspace-tree", "ref"))
        )
    )
    with pytest.raises(ValidationError):
        judge(p, "contract", ("a",), execution(p, "contract", ("a",), resource_refs=refs))


def test_missing_actual_runner_cannot_claim_green(tmp_path):
    root = private_dir(tmp_path)

    def missing(*args):
        raise AccessDenied("Configured runner unavailable")

    executor = RecordingExecutor(missing)
    store = CheckStore(root / "checks.sqlite")
    service = MergeChecks(store, Guard(), executor, catalogs=catalogs(root))
    scope = Scope("p", "project", "s", "t")
    ref = service.prepare(scope, "a", SOURCE, change(), discovery(), "key")
    result = service.run(ref, scope)
    assert result["outcome"] == "blocked"
    assert result["groups"]["checks a"]["result"]["physical_count"] is None
    assert not store.proofs(result["plan"], result["plan"].units[0])
