"""DecisionApi, a loopback System One endpoint: it records each request and answers with its
scripted reply, by default the first criteria key; /elsewhere counts hits, a redirect's target.
"""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = "/v1/systemone"
ELSEWHERE = "/elsewhere"


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: object


@dataclass
class Reply:
    status: int = 200
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


class DecisionApi:
    def __init__(self):
        self.reply = Reply()
        self.requests: list[Request] = []
        self.elsewhere = 0
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.api, self._closing = self, threading.Event()
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}{PATH}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._closing.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def answer(self, request):
        self.requests.append(request)
        if request.path == ELSEWHERE:
            self.elsewhere += 1
            return Reply(404, b"{}")
        if self.reply.delay and self._closing.wait(self.reply.delay):
            return None
        if self.reply.body is not None:
            return self.reply
        questions = request.body.get("questions") if isinstance(request.body, dict) else None
        criteria = questions["choice"]["criteria"] if isinstance(questions, dict) else {}
        pick = next(iter(criteria), None)
        body = json.dumps({"answers": {"choice": {"type": "choice", "choice": pick}}}).encode()
        return Reply(self.reply.status, body, self.reply.headers)


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins the handlers: none outlives the case


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self._answer(body)

    def do_GET(self):
        self._answer(None)

    def _answer(self, body):
        request = Request(self.command, self.path, dict(self.headers.items()), body)
        reply = self.server.api.answer(request)
        if reply is None:
            return
        try:
            self.send_response(reply.status)
            for name, value in {"Content-Type": "application/json", **reply.headers}.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body or b"")))
            self.end_headers()
            self.wfile.write(reply.body or b"")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass
