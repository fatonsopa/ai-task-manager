"""Installed CLI/MCP queue observations over explicitly authored Core facts.

These are protocol controls, not genuine native/provider execution evidence.
"""

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ai_task_manager.domains.scheduling.models import Dependency, SchedulingBlocked
from ai_task_manager.domains.work.models import IssueStatus as S
from tests.application.helpers import ServiceRig
from tests.scheduling.helpers import declare
from tests.scheduling.test_guards import reserve


@pytest.fixture
def installed():
    selected = os.environ.get("DP06_INSTALLED_VENV")
    if not selected:
        pytest.skip("DP06_INSTALLED_VENV must name the reviewed noneditable installation")
    root = Path(selected).resolve()
    support = Path(pytest.__file__).parent.parent
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(support),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    observed = subprocess.run(
        [str(root / "bin/python"), "-c",
         "import ai_task_manager;print(ai_task_manager.__file__)"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert observed.returncode == 0, observed.stderr
    assert Path(observed.stdout.strip()).is_relative_to(root)
    return root / "bin/aitm", env


def visible(rows):
    return [
        {
            "id": row["id"],
            "status": row["observed_status"],
            "scheduling": {
                key: row["scheduling"][key]
                for key in ("state", "selection_state", "priority", "bypasses",
                            "fair_turn_due", "eligible_since", "blockers")
            },
        }
        for row in rows
    ]


def protocol_views(env, installed):
    executable, environment = installed
    config = env.root / "config.json"
    if not config.exists():
        config = env.configuration()
    expected = env.service.call("task_list", {})
    assert expected["ok"]
    result = subprocess.run(
        [str(executable), "--config", str(config), "--json", "task", "list"],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert visible(json.loads(result.stdout)["data"]) == visible(expected["data"])

    async def consume():
        params = StdioServerParameters(
            command=str(executable),
            args=["--config", str(config), "mcp", "serve"],
            env=environment,
        )
        async with Client(params, read_timeout_seconds=15) as client:
            listed = await client.call_tool("task_list", {})
            assert not listed.is_error
            assert visible(listed.structured_content["data"]) == visible(expected["data"])
            for row in expected["data"]:
                shown = await client.call_tool("task_show", {"task_id": row["id"]})
                assert not shown.is_error
                assert visible([shown.structured_content["data"]]) == visible([row])

    anyio.run(consume)
    return expected["data"]


def task(env, priority="normal"):
    rig = env.rig
    value = rig.queued()
    declare(rig.core, rig.handle, value, rig.scheduling_sources, rig.now, priority=priority)
    return value


def test_DP06Q01_installed_protocols_preserve_blockers_and_release_verified_unit(
    tmp_path, installed,
):
    env = ServiceRig(tmp_path)
    rig = env.rig
    rig.now = time.time()
    predecessor = task(env)
    dependent = task(env, "urgent")
    held = task(env, "urgent")
    proof = rig.proof("human_decision", held.id, {
        "previous_revision": held.decision_revision, "hold": True,
    })
    rig.core.decisions.record(rig.handle, held.id, held.version, proof, rig.key())
    edge = Dependency(predecessor.id, "unit_verified", "complete")
    declare(rig.core, rig.handle, dependent, rig.scheduling_sources, rig.now,
            (edge,), "urgent")
    rows = protocol_views(env, installed)
    assert rows[0]["id"] == predecessor.id
    dependent_view = next(row for row in rows if row["id"] == dependent.id)
    assert dependent_view["activity"] == "blocked"
    assert dependent_view["observed_status"] == S.TODO
    held_view = next(row for row in rows if row["id"] == held.id)
    assert held_view["human_hold"] and held_view["scheduling"]["selection_state"] == "blocked"
    with pytest.raises(SchedulingBlocked):
        reserve(rig, dependent)
    predecessor, attempt = rig.running(predecessor)
    predecessor, _ = rig.settle(predecessor, attempt)
    predecessor = rig.complete(predecessor)
    # A signed authored milestone permits cold CLI/MCP verification. It remains
    # explicitly different from an observed real worker/provider completion.
    proof = env.authority._evidence(
        "work_unit_complete", rig.scope, predecessor.id,
        {"unit": "complete", "prose_digest": predecessor.prose_digest},
        "authored-unit-milestone",
    )
    edge = replace(edge, evidence_refs=(("work_unit_complete", proof),))
    declare(rig.core, rig.handle, dependent, rig.scheduling_sources, rig.now,
            (edge,), "urgent")
    rows = protocol_views(env, installed)
    assert rows[0]["id"] == dependent.id
    assert rows[0]["scheduling"]["selection_state"] == "ready"
    assert reserve(rig, dependent).task_id == dependent.id


def test_DP06Q02_installed_fair_turn_matches_atomic_admission(tmp_path, installed):
    env = ServiceRig(tmp_path)
    rig = env.rig
    rig.now = time.time()
    oldest = task(env, "low")
    for _ in range(4):
        higher = task(env, "high")
        current, attempt = rig.running(higher)
        rig.settle(current, attempt)
    later = task(env, "urgent")
    rows = protocol_views(env, installed)
    assert rows[0]["id"] == oldest.id
    assert rows[0]["scheduling"]["bypasses"] == 4
    assert rows[0]["scheduling"]["fair_turn_due"] is True
    waiting = next(row for row in rows if row["id"] == later.id)
    assert waiting["activity"] == "waiting"
    assert waiting["observed_status"] == S.TODO
    with pytest.raises(SchedulingBlocked):
        reserve(rig, later)
    attempt = reserve(rig, oldest)
    assert attempt.task_id == oldest.id
    assert rig.current(oldest).execution_state == "reserved"
    assert rig.current(oldest).observed_status == S.TODO
