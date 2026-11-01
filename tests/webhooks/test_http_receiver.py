from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

from tests.linear.helpers import LinearRig, ProviderFixture


def test_l14_l27_actual_loopback_http_receiver_persists_before_200(tmp_path):
    provider = ProviderFixture()
    rig = LinearRig(tmp_path, provider)
    task = rig.task()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            response = rig.receiver.receive(
                self.command,
                list(self.headers.items()),
                self.rfile.read(int(self.headers["Content-Length"])),
            )
            assert rig.journal.deliveries(rig.scope) and rig.core.sync.inbound(rig.handle)
            self.send_response(response.status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        headers, body = rig.signed(provider.event(task.issue_id))
        request = Request(
            f"http://127.0.0.1:{server.server_port}/webhook",
            data=body,
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert rig.journal.deliveries(rig.scope)[0]["state"] == "received"
        assert rig.inbound.run_once(rig.handle)["classification"] == "outbound_echo"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        provider.close()
