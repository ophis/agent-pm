"""The test-only fake adapter, runtime `fake`: a whole Adapter in one package, importing only adapters.base, so a test
can register it in-process (sys.modules["adapters.fake"]) or copy it to src/adapters/fake/ unchanged. Its runtime is
runtime.py (RUNTIME), run by this Python.

Config keys: scenario (the scenario file), log (the runtime's log file), tui (true: a Tui). FakeHeadless.parse
maps each JSON record by its "type": session {id} → SessionBound, activity {name} → Activity, message {text} → Reply,
blocked {reason} → Blocked, result {text, pending} → TurnEnd, exit → Exited(99) (which the Driver drops); any other
record → nothing. FakeTui's hook labels: stop (TURN_END: reply its last_message, pending its tasks' count), ask
(BLOCKED), tool (ACTIVITY)."""
import os
import shlex
import sys

from adapters.base import (ACTIVITY, BLOCKED, TURN_END, Activity, Adapter, Blocked, Exited, Headless, Launch, Reply,
                           SessionBound, Tui, TurnEnd)

RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.py")


class FakeAdapter(Adapter):
    keys = frozenset({"scenario", "log", "tui"})
    runtime_note = "FAKE NOTE"

    def headless(self):
        return FakeHeadless(self)

    def tui(self):
        return FakeTui(self) if self.config.get("tui") else None


def _argv(adapter, prompt, spec, resume) -> tuple[str, ...]:
    return (sys.executable, RUNTIME, "--scenario", adapter.config["scenario"], "--log", adapter.config["log"],
            "--sid", spec.sid, *(["--resume"] if resume else []), "--", prompt)


class FakeHeadless(Headless):
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
        elif kind == "exit":
            yield Exited(99)


class FakeTui(Tui):
    hooks = {"stop": TURN_END, "ask": BLOCKED, "tool": ACTIVITY}

    def launch(self, prompt, spec, resume, hook):
        return Launch((*_argv(self.adapter, prompt, spec, resume), *hook))

    def reply(self, payload):
        return payload.get("last_message") or ""

    def pending(self, payload):
        return len(payload.get("tasks", []))
