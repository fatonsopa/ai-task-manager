"""Physical turn controls; authored subprocess boundaries do not prove native authority."""

import array
import fcntl
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from tests.merge.candidates.helpers import CandidateRig


def test_existing_directory_turn_has_bounded_contention(tmp_path):
    owned = CandidateRig(tmp_path / "owned")
    try:
        profile = owned.candidate_profile
        calls = []
        with profile.merge_turn(
            deadline=time.time() + 10, clock=time.time, check=lambda: calls.append(1)
        ):
            descriptor = profile.inherited_turn()[0]
            with pytest.raises(AccessDenied, match="wait budget"):
                with profile.merge_turn(
                    deadline=time.time() + 0.05, clock=time.time, check=lambda: calls.append(1)
                ):
                    pytest.fail("A second contender entered the held physical turn")
            assert profile.inherited_turn()[0] == descriptor
        assert profile.inherited_turn() is None and len(calls) >= 3
    finally:
        owned.runtime.close()


CONTROLLER = r"""
import array,json,os,socket,sys,time
from pathlib import Path
from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.publication import LocalGit
from tests.merge.f1_fixtures import Rig,FaultOnce
root=Path(sys.argv[1]); wire=socket.socket(fileno=int(sys.argv[2]))
r=Rig(root, '/usr/bin/git')
directory=root/'candidates';directory.mkdir(mode=0o700)
p=CandidateProfile(r.profile,'fixture-worktree','fixture-common',directory)
with p.merge_turn(deadline=time.time()+30,clock=time.time,check=lambda:None):
    publisher=LocalGit(r.journal,r.profile,r.operations,inherited_turn=p.inherited_turn(),faults=FaultOnce('after_intent'))
    try: publisher.publish(r.ref,r.journal.current(r.ref)['version'],'crash-control',r.request)
    except RuntimeError: pass
    publisher.faults=None
    child=publisher._spawn(r.ref,'publication',['update-ref','--stdin'],interactive=True)
    publisher._send(child,'start\n')
    assert child.stdout.line(b'start: ok',2)
    payload=json.dumps({'pid':child.process.pid,'ref':r.ref,'old':r.old,'common':str(r.repo/'.git')}).encode()
    writer=array.array('i',[child.process.stdin.fileno()])
    wire.sendmsg([payload],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,writer)])
    os._exit(0)
"""


def test_actual_publication_child_retains_turn_after_controller_exit(tmp_path):
    parent, child = socket.socketpair()
    controller = subprocess.Popen(
        [sys.executable, "-c", CONTROLLER, str(tmp_path / "owned"), str(child.fileno())],
        pass_fds=(child.fileno(),),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child.close()
    writer = None
    probe = None
    parent.settimeout(15)
    try:
        raw, ancillary, _, _ = parent.recvmsg(4096, socket.CMSG_SPACE(array.array("i").itemsize))
        record = json.loads(raw)
        descriptors = array.array("i")
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors.frombytes(value[: len(value) - (len(value) % descriptors.itemsize)])
        assert len(descriptors) == 1
        writer = descriptors[0]
        output, error = controller.communicate(timeout=5)
        assert controller.returncode == 0, (output, error)
        probe = os.open(record["common"], os.O_RDONLY)
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The test observer releases only its duplicated owned stdin writer.
        # The original controller is dead; EOF lets the real Git child settle.
        os.close(writer)
        writer = None
        until = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.monotonic() < until
                time.sleep(0.02)
        from ai_task_manager.storage.merging.journal import Journal

        row = Journal(tmp_path / "owned/state/operations.sqlite").current(record["ref"])
        assert row["facts"]["local_git"].get("result") is None
        assert (tmp_path / "owned-foreign.txt").read_bytes() == b"independent foreign sentinel\n"
        (tmp_path / "controller-exit.json").write_text(
            json.dumps(
                dict(
                    record,
                    controller_exit=0,
                    child_held_turn_after_controller_exit=True,
                    logical_publication_unknown_retained=True,
                ),
                indent=2,
            )
        )
    finally:
        if writer is not None:
            os.close(writer)
        if probe is not None:
            os.close(probe)
        parent.close()
        if controller.poll() is None:
            controller.terminate()
            controller.communicate(timeout=5)
