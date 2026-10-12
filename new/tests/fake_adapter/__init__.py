"""The test-only Adapter, runtime `fake`, importing only adapters.base: a test registers it in-process
(sys.modules["adapters.fake"]) or copies it to src/adapters/fake/. Config keys: scenario, log, tui (true: a TuiRun).
Its runtime is runtime.py (RUNTIME), run by this Python; a limit record {resets_at} parses to LimitHit, an exit
record to Exited(99), which the Driver drops."""
import os
import shlex
import sys

from adapters.base import (Activity, Adapter, Blocked, Exited, HeadlessRun, Launch, LimitHit, Reply, SessionBound,
                           TuiRun, TurnEnd)

RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.py")


class FakeAdapter(Adapter):
    keys = frozenset({"scenario", "log", "tui"})
    runtime_note = "FAKE NOTE"

    def headless_run(self):
        return FakeHeadlessRun(self)

    def tui_run(self):
        return FakeTuiRun(self) if self.config.get("tui") else None


def _argv(adapter, prompt, spec, resume) -> tuple[str, ...]:
    return (sys.executable, RUNTIME, "--scenario", adapter.config["scenario"], "--log", adapter.config["log"],
            "--sid", spec.sid, *(["--resume"] if resume else []), "--", prompt)


class FakeHeadlessRun(HeadlessRun):
    def launch(self, prompt, spec, resume):
        return Launch(_argv(self.adapter, prompt, spec, resume),
                      transcript=os.path.join(spec.workdir, f"fake-{spec.sid}.jsonl"),
                      resume=f"fake --resume {shlex.quote(spec.sid)}")

    def parse(self, record):
        kind = record.get("type")
        if kind == "session":
            yield SessionBound(record["id"])
        elif kind == "activity":
            yield Activity(record.get("name", ""))
        elif kind == "message":
            yield Reply(record["text"])
        elif kind == "blocked":
            yield Blocked(record.get("reason", ""))
        elif kind == "result":
            yield TurnEnd(record.get("text", ""), record.get("pending", 0))
        elif kind == "limit":
            yield LimitHit(record.get("resets_at"))
        elif kind == "exit":
            yield Exited(99)


class FakeTuiRun(TuiRun):
    def launch(self, prompt, spec, resume, hook):
        return Launch((*_argv(self.adapter, prompt, spec, resume), *hook))

    def reply(self, payload):
        return payload.get("last_message") or ""

    def pending(self, payload):
        return len(payload.get("tasks", []))
