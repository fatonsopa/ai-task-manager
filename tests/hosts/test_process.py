import time

import pytest

from ai_task_manager.domains.work.errors import AccessDenied


def test_real_owned_bidirectional_frames(native):
    process, transcript, emit = native()
    assert emit({"type": "fixture", "content": "native"})["content"] == "native"
    assert process.identity.pid != process.process.pid
    stopped = process.stop()
    assert stopped.confirmed and process._closed


def test_trace_comes_from_owned_frame_and_redacts_credentials(native, journal):
    _, transcript, emit = native()
    emit({"type": "fixture", "accessToken": "PRIVATE_TOKEN"})
    record = journal._read(transcript._last, "trace")
    assert "PRIVATE_TOKEN" not in str(record)
    assert record["payload"]["process"]["pid"] > 0


def test_original_deadline_ends_resistant_descendant(native, tmp_path):
    marker = tmp_path / "child"
    program = (
        "import os,sys,subprocess,time,signal\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)'])\n"
        f"open({str(marker)!r},'w').write(str(p.pid))\n"
        "time.sleep(20)\n"
    )
    process, _, _ = native(program=program, deadline=time.time() + 1)
    end = time.monotonic() + 3
    while time.monotonic() < end:
        view = process.group.observe()
        if view.known and not view.live_members:
            break
        time.sleep(0.02)
    assert marker.exists()
    assert view.known and not view.live_members
    assert process.stop().confirmed


def test_controller_pipe_loss_escalates_its_owned_group(native, tmp_path):
    process, _, _ = native(
        program="import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)"
    )
    process.process.stdin.close()
    end = time.monotonic() + 3
    while time.monotonic() < end:
        view = process.group.observe()
        if view.known and not view.live_members:
            break
        time.sleep(0.02)
    assert view.known and not view.live_members
    assert process.stop().confirmed


@pytest.mark.parametrize(
    "payload",
    ["print('not-json',flush=True)\ntime.sleep(5)", "print('[]',flush=True)\ntime.sleep(5)"],
)
def test_malformed_frames_refuse(native, payload):
    process, transcript, _ = native(program="import time\n" + payload)
    with pytest.raises(AccessDenied):
        transcript.read(1)


def test_normal_native_exit_does_not_establish_model_success(native):
    process, _, _ = native(program="import time;time.sleep(.3)")
    time.sleep(0.5)
    assert process.stop().confirmed
    assert not hasattr(process, "model_success")


def test_unknown_group_never_returns_confirmed_stop(native, monkeypatch):
    import os

    from ai_task_manager.adapters.process.groups import GroupObservation

    process, _, _ = native()
    group = process.group
    with monkeypatch.context() as patch:
        patch.setattr(group, "observe", lambda: GroupObservation(False, reason="unavailable"))
        patch.setattr(group, "_signal", lambda *_: False)
        first = process.stop()
        assert not first.confirmed
    # An implicit second close cannot re-signal or reclassify unknown ownership.
    assert process.stop() is first
    # Separate trusted fixture cleanup uses the retained original group anchor;
    # it does not rewrite the original transport stop result.
    assert group.stop().confirmed
    process.native_input.close()
    process.native_exit.close()
    group.reap_exited_leader()
    process.process.stdout.close()
    os.close(process._relay_ready_fd)
    process._relay_ready_fd = None
    assert process.stop() is first and not first.confirmed
