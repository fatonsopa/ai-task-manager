import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.container_admission import ContainerAdmissions
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


def test_intent_mac_refuses_changed_deadline_after_restart(journal):
    journal.intend("launch", {"deadline": 123, "owner": "original"})
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE host_intents SET data=? WHERE identity=?",
            (json.dumps({"deadline": 999, "owner": "original"}), "launch"),
        )
    restarted = HostJournal(journal.path, journal.key_path)
    with pytest.raises(AccessDenied):
        restarted.intent("launch")
    with pytest.raises(AccessDenied):
        restarted.intend("launch", {"deadline": 999, "owner": "original"})
    with restarted.transaction() as conn:
        assert (
            json.loads(conn.execute("SELECT data FROM host_intents").fetchone()[0])["deadline"]
            == 999
        )


def test_unsigned_legacy_intent_migrates_as_unresolved_without_fabricated_signature(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    key = private / "key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o600)
    path = private / "journal.sqlite"
    old = json.dumps({"deadline": 123, "stage": "launch_intent"})
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE host_version(version INTEGER NOT NULL)")
        conn.execute("INSERT INTO host_version VALUES(1)")
        conn.execute(
            "CREATE TABLE host_records(ref TEXT PRIMARY KEY,data TEXT NOT NULL,mac TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE host_intents(identity TEXT PRIMARY KEY,data TEXT NOT NULL)")
        conn.execute("INSERT INTO host_intents VALUES(?,?)", ("old", old))
    path.chmod(0o600)
    journal = HostJournal(path, key)
    with pytest.raises(AccessDenied):
        journal.intent("old")
    with journal.transaction() as conn:
        assert conn.execute(
            "SELECT data,mac FROM host_intents WHERE identity=?", ("old",)
        ).fetchone() == (old, None)
        assert conn.execute("SELECT version FROM host_version").fetchone()[0] == 2


def test_independent_process_cannot_take_current_runner_slot(journal, tmp_path):
    root = tmp_path / "slot"
    root.mkdir(mode=0o700)
    admission = ContainerAdmissions(journal, root)
    with admission.slot():
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.hosts.slot_fixture",
                str(journal.path),
                str(journal.key_path),
                str(root),
            ],
            capture_output=True,
            text=True,
            env={"PATH": os.defpath, "PYTHONPATH": str(Path("src").resolve())},
            timeout=5,
        )
        assert child.returncode == 0, (child.stdout, child.stderr)
        assert child.stdout.strip() == "BLOCKED"
    with ContainerAdmissions(HostJournal(journal.path, journal.key_path), root).slot():
        pass


def test_released_os_lock_cannot_hide_unresolved_prior_launch(journal, tmp_path):
    root = tmp_path / "slot"
    root.mkdir(mode=0o700)
    admission = ContainerAdmissions(journal, root)
    with admission.slot():
        journal.intend(
            "container:interrupted",
            {
                "directory": str(root / "owned"),
                "name": "owned",
                "attempt_id": "attempt",
                "deadline": 123,
            },
        )
    with pytest.raises(RuntimeUncertain):
        with ContainerAdmissions(HostJournal(journal.path, journal.key_path), root).slot():
            pass
    # Synthetic positive control for the admission algorithm, not real container evidence.
    journal._append(
        "trace",
        {
            "effect": "container_outcome",
            "intent": "owned",
            "confirmed_stop": True,
            "source": "synthetic_fixture_only",
        },
    )
    with admission.slot():
        pass
