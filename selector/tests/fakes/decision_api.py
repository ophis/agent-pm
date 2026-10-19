"""DecisionApi, a loopback System One endpoint: it records each request and answers with its
scripted reply, by default its one question's: a choice the first of picks in its criteria, else
its first criteria key; a score the level of the first of picks whose "<pick>: " starts a criteria
entry, else level 0; with the next of confidences, consumed one per answer, else 1.0. /elsewhere
counts hits, a redirect's target.
"""

import json
import threading
from collections.abc import Sequence
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
    def __init__(self, picks: Sequence[str] = (), *, confidences: Sequence[float] = ()):
        self.reply = Reply()
        self.picks = list(picks)
        self.confidences = list(confidences)
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
        ((name, question),) = questions.items() if isinstance(questions, dict) else (("", {}),)
        criteria = question.get("criteria", {})
        confidence = self.confidences.pop(0) if self.confidences else 1.0
        if question.get("type") == "score":
            level = next(
                (i for p in self.picks for i, c in enumerate(criteria) if c.startswith(f"{p}: ")),
                0,
            )
            legend = dict(enumerate(criteria))
            answer = {"type": "score", "score": level, "confidence": confidence, "legend": legend}
        else:
            pick = next((p for p in self.picks if p in criteria), next(iter(criteria), None))
            answer = {"type": "choice", "choice": pick, "confidence": confidence}
        body = json.dumps({"answers": {name: answer}}).encode()
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
