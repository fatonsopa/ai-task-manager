"""Test-only loopback destination injection around the installed aitm script."""

import os
import runpy
import sys
from urllib.parse import urlparse

from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.service import linear

endpoint = os.environ["AITM_FIXTURE_ENDPOINT"]
parsed = urlparse(endpoint)
if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
    raise RuntimeError("Only the authored loopback fixture is permitted")
linear.HttpTransport = lambda: HttpTransport(_test_endpoint=endpoint)
# The operator setup port imports the transport class directly. This declared
# external-provider seam also covers that installed composition, not authority.
HttpTransport.ENDPOINT = endpoint
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
