"""One actual installed fixed-port request/stop/restart with a local provider."""

import hashlib
import socket

from tests.linear.helpers import STATES

from .helpers import ROUTE, Fixture, request, wait_for
from .test_installed import query, start, stop


def test_rr07_installed_fixed_port_restart_and_duplicate(tmp_path):
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    port = reserved.getsockname()[1]
    fixture = None
    processes = []
    try:
        fixture = Fixture(tmp_path, port=port)
        sources = [fixture.config, fixture.receiver, fixture.root / "session.json"]
        sealed = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        reserved.close()
        first, ready = start(fixture)
        processes.append(first)
        assert ready["port"] == port
        fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        headers, body = fixture.event()
        assert request(("127.0.0.1", port), ROUTE, headers, body)[0] == 200
        final = stop(fixture, first, label="fixed-first-stop")
        assert first.returncode == 0 and final["ok"]
        assert final["failure"] is final["cleanup"]["failure"] is None
        second, ready = start(fixture)
        processes.append(second)
        assert ready["port"] == port
        assert request(("127.0.0.1", port), ROUTE, headers, body)[0] == 200
        wait_for(lambda: fixture.rig.journal.deliveries(fixture.rig.scope)[0]["state"] == "applied")
        task = query(fixture)
        assert task["observed_status"] == "Todo"
        assert request(("127.0.0.1", port), ROUTE, headers, body)[0] == 200
        assert query(fixture)["version"] == task["version"]
        assert len(fixture.rig.journal.deliveries(fixture.rig.scope)) == 1
        final = stop(fixture, second, label="fixed-second-stop")
        assert second.returncode == 0 and final["ok"]
        assert final["failure"] is final["cleanup"]["failure"] is None
        assert {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources
        } == sealed
    finally:
        reserved.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        if fixture is not None:
            fixture.close()
