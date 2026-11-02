"""Independent operator endpoint. Its inherited descriptor is never given to a model."""

import hashlib
import json
import os
import socket
import stat
import sys


def main():
    channel = socket.socket(fileno=int(sys.argv[1]))
    request = json.loads(channel.makefile("r").readline())
    # No redirected model input or an arbitrary path can answer this prompt.
    with open("/dev/tty") as terminal, open("/dev/tty", "w") as output:
        info = os.fstat(terminal.fileno())
        if not stat.S_ISCHR(info.st_mode) or not os.isatty(terminal.fileno()):
            raise RuntimeError("Independent operator terminal is unavailable")
        output.write("\nManaged task authorization\n")
        output.write(json.dumps(request["instruction"], sort_keys=True, indent=2) + "\n")
        output.write(
            "To approve this exact instruction, type APPROVE " + request["challenge"] + ": "
        )
        output.flush()
        response = terminal.readline().strip()
        accepted = response == "APPROVE " + request["challenge"]
    reply = {
        "request_id": request["request_id"],
        "challenge": request["challenge"],
        "instruction_hash": hashlib.sha256(
            json.dumps(
                request["instruction"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest(),
        "accepted": accepted,
        "operator_pid": os.getpid(),
    }
    channel.sendall((json.dumps(reply) + "\n").encode())
    channel.close()


if __name__ == "__main__":
    main()
