"""Actual framed fixture RPCs; no Codex account or model operation."""

from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.transports.codex import CodexSession
from ai_task_manager.domains.work.errors import AccessDenied


def session(native, extra):
    program = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        " q=json.loads(line)\n"
        f" extra={extra!r}\n"
        " if q['id']==1:\n"
        "  for identity in extra:\n"
        "   print(json.dumps({'id':identity,'result':{'poison':True}}),flush=True)\n"
        " print(json.dumps({'id':q['id'],'result':{'accepted':q['id']}}),flush=True)\n"
    )
    process, transcript, _ = native(program=program)
    return CodexSession(process, transcript, SimpleNamespace(native_session="fixture"), None)


@pytest.mark.parametrize("identity", [0, 2, 999, True, "1"])
def test_unrequested_or_wrong_type_native_reply_refuses(native, identity):
    value = session(native, [identity])
    with pytest.raises(AccessDenied, match="response identity"):
        value.request("fixture", {})


def test_completed_request_id_cannot_satisfy_or_poison_a_later_request(native):
    program = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        " q=json.loads(line)\n"
        " print(json.dumps({'id':q['id'],'result':{'accepted':True}}),flush=True)\n"
        " if q['id']==1: print(json.dumps({'id':1,'result':{'stale':True}}),flush=True)\n"
    )
    process, transcript, _ = native(program=program)
    value = CodexSession(process, transcript, SimpleNamespace(native_session="fixture"), None)
    assert value.request("first", {}) == {"accepted": True}
    with pytest.raises(AccessDenied, match="response identity"):
        value.request("second", {})


def test_matching_native_response_is_returned_and_no_longer_outstanding(native):
    value = session(native, [])
    assert value.request("first", {}) == {"accepted": 1}
    assert value.request("second", {}) == {"accepted": 2}
