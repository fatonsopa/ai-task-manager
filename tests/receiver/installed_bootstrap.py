"""Test-side transport substitution around the unmodified installed CLI script.

This is deliberately outside the runtime package. It sends only to one authored
loopback provider. Operator configuration cannot select this substitution.
"""

import runpy
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ai_task_manager.adapters.linear.transport import HttpTransport

endpoint, entry, *arguments = sys.argv[1:]
assert urlsplit(endpoint).hostname == "127.0.0.1"
assert "site-packages" in str(Path(sys.modules[HttpTransport.__module__].__file__).resolve())
original = HttpTransport.__init__


def fixture_transport(self, **kwargs):
    assert "_test_endpoint" not in kwargs
    original(self, _test_endpoint=endpoint, **kwargs)


HttpTransport.__init__ = fixture_transport
sys.argv = [entry, *arguments]
runpy.run_path(entry, run_name="__main__")
