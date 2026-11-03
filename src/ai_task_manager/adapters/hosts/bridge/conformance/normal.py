"""Normal admission consumes complete historical capability facts, never identity renewal."""

from ai_task_manager.config.files import digest, read_private

from ..admission import MANAGED_TOOLS, launch_template
from .models import CASE_IDS, ProbeContext, cases, profile_digest
from .verification import require, startup, verify_case


class NormalConformance:
    def __init__(self, manifest, pins, context, journal, reference):
        self.manifest, self.pins, self.context, self.journal, self.reference = (
            manifest,
            pins,
            context,
            journal,
            reference,
        )
        self._bound = (manifest, pins, context, journal, reference)

    def require_installation(self, manifest):
        require(
            all(
                a is b
                for a, b in zip(
                    self._bound[:4],
                    (self.manifest, self.pins, self.context, self.journal),
                    strict=True,
                )
            )
            and self.reference == self._bound[4]
            and manifest is self.manifest,
            "Normal conformance belongs to another installed owner",
        )
        manifest.check()
        self.pins.check()
        c = self.context
        require(
            isinstance(c, ProbeContext) and c.profile_digest == profile_digest(manifest, self.pins),
            "Native conformance source/profile changed",
        )
        p = self.journal._read(self.reference, "trace", fresh=False)["payload"]
        require(
            p.get("effect") == "native_conformance_capture"
            and not p.get("error_type")
            and p.get("profile") == c.profile_digest
            and p.get("context_ref") == c.reference
            and p.get("manifest_digest") == manifest.digest
            and p.get("source") == self.pins.fingerprint
            and self.journal.clock() < p["not_after"]
            and p.get("confirmed_stop") is True,
            "Complete unrevoked native capability capture is unavailable",
        )
        require(
            p["not_after"] == read_private(manifest.installation.path)["deadline"],
            "The original installation lifetime changed",
        )
        require(
            not any(
                row["payload"].get("effect") == "native_conformance_revoked"
                and row["payload"].get("capture") == self.reference
                for row in self.journal._history("trace")
            ),
            "Native conformance was revoked",
        )
        stop = p.get("stop", {})
        require(
            stop.get("confirmed") is True
            and stop.get("observation", {}).get("known") is True
            and all(
                row.get("state", "").startswith("Z")
                for row in stop["observation"].get("members", [])
            ),
            "Native probe group stop was not proved",
        )
        require(
            [v.get("case_id") for v in p.get("cases", [])] == list(CASE_IDS),
            "Native capability matrix is incomplete",
        )
        profile = {k: getattr(c, k) for k in ("host", "model", "effort", "account_hash")}
        session = startup(self.journal, p["startup_ref"], p["owner"], profile)
        for case, value in zip(cases(c), p["cases"], strict=True):
            verify_case(self.journal, case, value, p["owner"], c.reference, session, c.model)
        return self.reference

    def require_native_conformance(self, manifest, policy, binding, journal):
        self.require_installation(manifest)
        d = manifest.data
        require(
            (policy.host, policy.model, policy.effort, policy.executable_sha256)
            == (d["host"], d["model"], d["effort"], d["executable_sha256"])
            and binding.host == d["host"]
            and binding.scope == self.context.scope,
            "Current native launch differs from the observed profile",
        )
        require(
            set(policy.allowed_tools) == MANAGED_TOOLS
            and policy.environment == manifest.environment,
            "Current native tool/environment surface differs",
        )
        p = self.journal._read(self.reference, "trace", fresh=False)["payload"]
        require(
            p.get("launch_fingerprint") == digest(launch_template(policy)),
            "Current native launch differs from actual captured configuration",
        )
        policy.check()
        # This is only a historical source/profile fact. NormalAdmissions still
        # requires the new worker's fresh actual account/configuration/startup.
        return self.reference

    def revoke(self):
        """Trusted installer operation; this is never a model tool."""
        return self.journal._append(
            "trace",
            {
                "effect": "native_conformance_revoked",
                "capture": self.reference,
            },
        )
