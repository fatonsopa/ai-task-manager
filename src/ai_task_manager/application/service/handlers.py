"""The fixed public surface; clients cannot select arbitrary callables."""

from .actions import perform as task_action
from .actions import settle_external as task_settle_external
from .decision_publication import (
    prepare as task_decision_prepare,
)
from .decision_publication import (
    request as task_decision_request,
)
from .decision_publication import (
    resolve as task_decision_resolve,
)
from .decision_publication import (
    show as task_decision_show,
)
from .events import event_acknowledge, event_list, session_resume, session_show, sync_now
from .existing import task_admit, task_find, task_inspect, task_select
from .intake import (
    task_accept_external_prose,
    task_progress,
    task_revise,
    task_submit,
    writing_finalize,
    writing_prepare,
    writing_review,
)
from .queries import task_external_changes, task_list, task_show
from .workers import worker_execute, worker_start
from .workflows import workflow_inspect, workflow_resume, workflow_run

HANDLERS = {
    "workflow_run": workflow_run,
    "workflow_resume": workflow_resume,
    "workflow_inspect": workflow_inspect,
    "writing_prepare": writing_prepare,
    "writing_review": writing_review,
    "writing_finalize": writing_finalize,
    "task_submit": task_submit,
    "task_find": task_find,
    "task_inspect": task_inspect,
    "task_select": task_select,
    "task_admit": task_admit,
    "task_revise": task_revise,
    "task_progress": task_progress,
    "task_decision_prepare": task_decision_prepare,
    "task_decision_request": task_decision_request,
    "task_decision_resolve": task_decision_resolve,
    "task_decision_show": task_decision_show,
    "task_accept_external_prose": task_accept_external_prose,
    "task_list": task_list,
    "task_show": task_show,
    "task_external_changes": task_external_changes,
    "session_show": session_show,
    "session_resume": session_resume,
    "event_list": event_list,
    "event_acknowledge": event_acknowledge,
    "sync_now": sync_now,
    "worker_start": worker_start,
    "worker_execute": worker_execute,
    "task_action": task_action,
    "task_settle_external": task_settle_external,
}
