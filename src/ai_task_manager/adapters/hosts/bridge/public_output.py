"""Installer-bound terminal delivery; terminal capability is not human authority."""

import hashlib
import os
import select
import stat
import time
import unicodedata
from dataclasses import asdict

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from .records import PublicOutput


def rendered(text):
    result = []
    for character in text:
        if character not in {"\n", "\t"} and unicodedata.category(character) in {"Cc", "Cf"}:
            value = ord(character)
            result.append(f"\\x{value:02x}" if value <= 255 else f"\\u{value:04x}")
        else:
            result.append(character)
    return "".join(result).encode("utf-8")


class TerminalOutput:
    def __init__(self, journal, terminal_fd):
        self.journal, self.fd = journal, None
        try:
            original = os.fstat(terminal_fd)
            if not stat.S_ISCHR(original.st_mode) or not os.isatty(terminal_fd):
                raise AccessDenied("Public output requires a configured actual terminal")
            # A fresh open avoids changing O_NONBLOCK on the caller's shared file
            # description. It does not change termios or the normal host profile.
            self.fd = os.open(
                os.ttyname(terminal_fd),
                os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
            self.identity = (original.st_dev, original.st_ino, original.st_rdev)
            self._check()
        except (OSError, ValueError) as error:
            self.close()
            raise AccessDenied("Configured public terminal is unavailable") from error
        except BaseException:
            self.close()
            raise

    def _check(self):
        if self.fd is None:
            raise AccessDenied("Public terminal is closed")
        current = os.fstat(self.fd)
        if (current.st_dev, current.st_ino, current.st_rdev) != self.identity or not os.isatty(
            self.fd
        ):
            raise AccessDenied("Configured public terminal identity changed")

    def publish(self, output):
        if not isinstance(output, PublicOutput) or not isinstance(output.text, str):
            raise AccessDenied("An observed final public output is required")
        raw = rendered(output.text)
        if not raw or len(raw) > 262144:
            raise AccessDenied("Public output exceeds the bounded terminal frame")
        self.journal._read(output.trace_ref, "trace")
        output_hash = digest(asdict(output))
        for row in self.journal._history("trace"):
            value = row["payload"]
            if value.get("effect") == "public_output" and value.get("output_hash") == output_hash:
                return row["ref"]
        self._check()
        if not self.journal.intend("public-output:" + output_hash, {"output_hash": output_hash}):
            raise RuntimeUncertain("Unresolved public-output intent cannot be printed twice")
        written, deadline = 0, time.monotonic() + 1
        try:
            while written < len(raw):
                self._check()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                    raise RuntimeUncertain("Public terminal did not accept the complete output")
                try:
                    count = os.write(self.fd, raw[written:])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise RuntimeUncertain("Public terminal output stopped before completion")
                written += count
        except OSError as error:
            raise RuntimeUncertain("Public terminal delivery is incomplete or unknown") from error
        return self.journal._append(
            "trace",
            {
                "effect": "public_output",
                "output_hash": output_hash,
                "native_session": output.native_session,
                "rendered_sha256": hashlib.sha256(raw).hexdigest(),
                "accepted_bytes": written,
                "surface": "configured_terminal",
                "human_acceptance": "not_established",
            },
            sources=(output.trace_ref,),
        )

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
