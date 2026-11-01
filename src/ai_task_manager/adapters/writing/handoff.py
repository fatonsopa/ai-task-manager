"""Verify the actual document handoff without creating another authority."""

import hashlib
import json
import os
import stat
from pathlib import Path

from ai_task_manager.config.files import MAX_JSON_BYTES, digest, read_private
from ai_task_manager.domains.work.errors import ApprovalRequired, WorkError

FIELDS = frozenset(
    {
        "status",
        "message",
        "message_sha256",
        "destination",
        "output_sha256",
        "objective_sha256",
        "reviewer",
        "document_status",
    }
)
REVIEWER_FIELDS = {"kind", "identity", "model", "method"}


def _bytes(path, folder):
    path, folder = Path(path), Path(folder)
    if not path.is_absolute() or ".." in path.parts:
        raise ApprovalRequired("Writer delivery location changed")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ApprovalRequired("Writer delivery cannot use symbolic links")
        if part == folder:
            break
    else:
        raise ApprovalRequired("Writer delivery location changed")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ApprovalRequired("Writer delivery must remain an owned regular file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ApprovalRequired("Writer delivery exceeds the configured size limit")
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def check_handoff(writer, folder, record, delivered, *, run_verifier=False):
    """Checks are repeated when consuming a sealed approval, without re-delivery."""
    try:
        return _check(writer, folder, record, delivered, run_verifier=run_verifier)
    except ApprovalRequired:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, WorkError) as exc:
        raise ApprovalRequired("Writer handoff is missing, malformed or changed") from exc


def _check(writer, folder, record, delivered, *, run_verifier):
    if not isinstance(delivered, dict) or set(delivered) != FIELDS | {"review"}:
        raise ApprovalRequired("Current writer delivery needs its complete checked handoff")
    if delivered["status"] != "delivered" or any(
        not _hash(delivered[name])
        for name in ("message_sha256", "output_sha256", "objective_sha256")
    ):
        raise ApprovalRequired("Writer delivery status or digest is invalid")
    for name in ("message", "destination", "review", "document_status"):
        if not isinstance(delivered[name], str) or not delivered[name].strip():
            raise ApprovalRequired("Writer handoff text is missing or malformed")
    reviewer = delivered["reviewer"]
    if (
        not isinstance(reviewer, dict)
        or set(reviewer) != REVIEWER_FIELDS
        or reviewer["kind"] != "current_host_self_review"
        or reviewer["identity"] != writer.reviewer_identity
        or reviewer["model"] != writer.reviewer_model
        or not isinstance(reviewer["method"], str)
        or not reviewer["method"].strip()
    ):
        raise ApprovalRequired("Writer handoff reviewer differs from this actual host")
    handoff = {name: delivered[name] for name in sorted(FIELDS)}
    if len(json.dumps(handoff, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ApprovalRequired("Writer handoff exceeds the configured size limit")

    packet = read_private(folder / "packet.json")
    if (
        digest(packet) != record["packet_sha256"]
        or packet["objective_sha256"] != record["objective_sha256"]
        or delivered["objective_sha256"] != record["objective_sha256"]
        or delivered["output_sha256"] != record["document_sha256"]
        or packet["output_sha256"] != record["document_sha256"]
        or delivered["destination"] != str(folder / "accepted.md")
    ):
        raise ApprovalRequired("Writer handoff belongs to different content or objective")
    result = read_private(folder / "review-result.json")
    review_path = Path(delivered["review"])
    if (
        result.get("accepted") is not True
        or str(review_path) != result["review"]
        or not review_path.is_relative_to(folder / "validator/reviews")
    ):
        raise ApprovalRequired("Writer handoff lacks this session's accepted review")
    review_raw = _bytes(review_path, folder)
    review = read_private(review_path)
    if (
        review.get("accepted") is not True
        or review.get("mechanical_passed") is not True
        or review["reviewer"] != reviewer
        or review["output_sha256"] != delivered["output_sha256"]
        or review["objective_sha256"] != delivered["objective_sha256"]
    ):
        raise ApprovalRequired("Writer handoff review changed")

    state_raw = _bytes(folder / "validator/state.json", folder)
    receipt_raw = _bytes(folder / "validator/delivery.json", folder)
    state = read_private(folder / "validator/state.json")
    receipt = read_private(folder / "validator/delivery.json")
    if (
        receipt != {key: value for key, value in delivered.items() if key != "status"}
        or state.get("delivery") != receipt
        or state["objective_sha256"] != record["objective_sha256"]
        or receipt["document_status"]
        != state["frozen"]["request"]["context"].get("discovery", {}).get("status", "unspecified")
    ):
        raise ApprovalRequired("Writer handoff receipt differs from the actual delivery")
    document_raw = _bytes(folder / "accepted.md", folder)
    message_raw = _bytes(folder / "validator/message.md", folder)
    # The source verify-message command permits trailing newline normalization.
    # The product contract requires the exact original UTF-8 bytes as well.
    if (
        _sha(document_raw) != delivered["output_sha256"]
        or message_raw != delivered["message"].encode("utf-8")
        or _sha(message_raw) != delivered["message_sha256"]
    ):
        raise ApprovalRequired("Writer document or public message bytes changed")
    seals = {
        "state_sha256": _sha(state_raw),
        "receipt_sha256": _sha(receipt_raw),
        "review_sha256": _sha(review_raw),
    }
    if run_verifier:
        verified = writer.runner.run(
            folder,
            "verify-message",
            "--session",
            folder / "validator",
            "--message",
            folder / "validator/message.md",
        )
        if (
            not isinstance(verified, dict)
            or verified.get("passed") is not True
            or verified.get("message_sha256") != delivered["message_sha256"]
        ):
            raise ApprovalRequired("Actual writer public-message verification did not pass")
    return handoff, seals
