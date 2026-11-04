"""Supported human actions retain Backlog holds and require a new release decision."""

from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.work.models import IssueStatus as S
from tests.application.helpers import ServiceRig
from tests.application.test_retry_human_workflows import observe, request


def test_hb08_public_human_dequeue_holds_until_new_explicit_queue_authority(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.queued()
    ref = observe(env, task, "Backlog", "human-dequeue")
    data = request(task, "dequeue", "observed-dequeue", human_ref=ref)
    accepted = env.service.call("task_action", data)
    assert accepted["ok"], accepted
    held = env.rig.current(task)
    assert held.human_hold and held.decision_revision == 1
    assert held.observed_status == S.BACKLOG
    assert env.service.call("task_action", data)["data"] == accepted["data"]
    calls = len(env.provider.calls)
    denied = env.service.call("task_action", request(held, "enqueue", "no-human-release"))
    assert not denied["ok"] and env.rig.current(held) == held
    assert len(env.provider.calls) == calls
    env.service = ApplicationService(
        env.rig.core,
        env.rig.handle,
        decisions=env.decisions,
        provider=env.provider,
        grant_ref="host-grant",
    )
    # A newer explicit same-status human decision retains the hold.
    retained = env.service.call(
        "task_action",
        request(
            held,
            "dequeue",
            "retain-backlog",
            human_ref=observe(env, held, "Backlog", "human-retain"),
        ),
    )
    assert retained["ok"] and retained["data"]["human_hold"]
    held = env.rig.current(held)
    assert held.decision_revision == 2
    released = env.service.call(
        "task_action",
        request(
            held,
            "enqueue",
            "new-human-release",
            human_ref=observe(env, held, "Todo", "human-release"),
        ),
    )
    assert released["ok"] and not released["data"]["human_hold"]
    assert released["data"]["observed_status"] == S.TODO
    assert released["data"]["decision_revision"] == 3
    assert len(env.provider.calls) == calls + 1
    assert not env.service.call(
        "task_action", request(env.rig.current(held), "dequeue", "reuse-old-human", human_ref=ref)
    )["ok"]
