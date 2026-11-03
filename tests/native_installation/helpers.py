"""Authored metadata/permission controls; never normal native admission evidence."""

import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.accounting import AccountingPlan
from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import digest, write_private
from ai_task_manager.installation.native.authority import NativePermission
from ai_task_manager.installation.native.manifest import installation_intent


@dataclass
class Bundle:
    config: Path
    installation: Path
    manifest: Path
    data: dict
    root: Path


def bundle(root, host="codex", model="gpt-6.1-sol", task_id="task"):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    config = root / "config.json"
    write_private(config, {"linear": {"credential_file": "MUST-NOT-READ"}})
    installation = root / "installation.json"
    manifest = root / "native.json"
    home = root / "home"
    home.mkdir(mode=0o700)
    profile = home / ".claude_roni" if host == "claude" else root / "codex-profile"
    profile.mkdir(mode=0o700)
    for name in ["auth.json", "config.toml"]:
        (profile / name).write_text("{}" if name == "auth.json" else "")
        (profile / name).chmod(0o600)
    spec = {
        "task_id": task_id,
        "deadline": time.time() + 60,
        "native_manifest": str(manifest),
        "native_ref": "normal-native",
        "native_digest": "0" * 64,
        "scope": {"principal": "owner"},
    }
    write_private(installation, spec)
    executable = Path(sys.executable).resolve()
    data = {
        "version": 1,
        "manifest_path": str(manifest),
        "installation": installation_intent(installation),
        "config": file_binding(config),
        "native_ref": "normal-native",
        "host": host,
        "model": model,
        "effort": "high",
        "account_hash": "a" * 64,
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "home": str(home),
        "profile": str(profile),
        "authorization_ref": "operator-permission",
    }
    write_private(manifest, data)
    refresh_digest(installation, manifest, data)
    return Bundle(config, installation, manifest, data, root)


def refresh_digest(installation, manifest, data):
    pins = NativeSourcePins({str(manifest): hashlib.sha256(manifest.read_bytes()).hexdigest()})
    expected = digest(
        {
            "reference": data["native_ref"],
            "host": data["host"],
            "model": data["model"],
            "effort": data["effort"],
            "account": data["account_hash"],
            "source": pins.fingerprint,
        }
    )
    import json

    spec = json.loads(installation.read_text())
    spec["native_digest"] = expected
    # Trusted fixture construction only. The runtime binds the final file.
    installation.write_text(json.dumps(spec))
    installation.chmod(0o600)


class IntentOnlyAdmissions:
    """Accepts test intent only; authentic conformance is deliberately unavailable."""

    def check_admission(self, plan):
        pass

    def verify_policy(self, *args):
        pass

    def verify_authenticated(self, *args):
        raise AssertionError("No actual account observation")

    def require_native_conformance(self, *args):
        raise AssertionError("No actual native conformance")


class AuthoredController:
    def snapshot(self):
        return {"policy": {"revision": 1}}


class PermissionSource:
    def __init__(self):
        self.calls = 0

    def resolve(self, reference, manifest):
        self.calls += 1
        import json

        spec = json.loads(manifest.installation.path.read_text())
        d = manifest.data
        plan = AccountingPlan(
            "control",
            "native",
            spec["task_id"],
            d["host"],
            d["model"],
            d["effort"],
            "authored-intent-not-billing",
            spec["deadline"],
            20,
            3,
            3,
            8,
        )
        return NativePermission(
            reference, manifest.digest, AuthoredController(), IntentOnlyAdmissions(), plan
        )
