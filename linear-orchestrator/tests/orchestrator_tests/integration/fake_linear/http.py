import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator_tests.integration.fake_linear.data import error_payload


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins the handlers: none outlives the case


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/graphql":
            return self._send(404, error_payload("fake: not found"))
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            req = None
        req = req if isinstance(req, dict) else {}
        status, body, delay = self.server.fake.answer(
            req.get("query"), req.get("variables"), self.headers.get("Authorization")
        )
        if delay and self.server.closing.wait(delay):
            return
        self._send(status, body)

    def do_GET(self):
        self._send(405, error_payload("fake: POST only"))

    do_PUT = do_PATCH = do_DELETE = do_GET

    def _send(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def serve(case, fake):
    server = _Server(("127.0.0.1", 0), _Handler)
    server.fake, server.closing = fake, threading.Event()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()

    def stop():
        server.closing.set()
        server.shutdown()
        server.server_close()
        thread.join()

    case.addCleanup(stop)
    return f"http://127.0.0.1:{server.server_address[1]}/graphql"


def seam(case, fake, url):
    fd, path = tempfile.mkstemp(prefix="fake-linear-", suffix=".json")
    case.addCleanup(os.remove, path)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "keys": fake.keys()}, f)
    return path
