"""Retain bounded Git/hook output before releasing the child execution gate."""

import os

from ai_task_manager.adapters.git.publication import _Drain


class CommitOutput(_Drain):
    def __init__(self, stream, path):
        self.path = path
        self.output = path.open("xb")
        path.chmod(0o600)
        super().__init__(stream)

    def _read(self):
        try:
            while True:
                chunk = os.read(self.stream.fileno(), 65536)
                if not chunk:
                    break
                with self.lock:
                    self.count += len(chunk)
                    retained = chunk[: max(0, 1048576 - len(self.prefix))]
                    self.prefix.extend(retained)
                    self.output.write(retained)
                    self.output.flush()
        except OSError as error:
            self.error = type(error).__name__
        finally:
            self.output.close()
            self.stream.close()
            self.done.set()

    def record(self):
        info = self.path.stat()
        return {
            "path": str(self.path),
            "device": info.st_dev,
            "inode": info.st_ino,
            **self.snapshot(),
        }
