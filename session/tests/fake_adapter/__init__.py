"""The test-only Adapter, runtime `fake`, importing only adapters.base: a test registers it, as
agent_pm.session.adapters.fake, in sys.modules or a core copy. Config keys: scenario, log, tui
(true: a Tui). Its runtime is runtime.py (RUNTIME), run by this Python; a limit record {resets_at}
parses to LimitHit, an exit record to Exited(99), which the Driver drops. InProcessFakeAdapter,
in_process only, logs {prompt, sid, resume, pid, thread} and runs out, err, sleep, exit and raise
steps.
"""

import json
import os
import shlex
import sys
import threading
import time

from agent_pm.session.adapters.base import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Adapter,
    Headless,
    InProcess,
    Tui,
)
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    Exited,
    Launch,
    LimitHit,
    Reply,
    SessionBound,
    SessionRequest,
    Stderr,
    TurnEnd,
)

RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.py")


class FakeAdapter(Adapter):
    keys = frozenset({"scenario", "log", "tui"})
    runtime_note = "FAKE NOTE"

    def headless(self):
        return FakeHeadless(self)

    def tui(self):
        return FakeTui(self) if self.config.get("tui") else None


class InProcessFakeAdapter(FakeAdapter):
    def headless(self):
        return None

    def tui(self):
        return None

    def in_process(self):
        return FakeInProcess(self)


def _argv(adapter, prompt, spec, resume) -> tuple[str, ...]:
    return (
        sys.executable,
        RUNTIME,
        "--scenario",
        adapter.config["scenario"],
        "--log",
        adapter.config["log"],
        "--sid",
        spec.sid,
        *(["--resume"] if resume else []),
        "--",
        prompt,
    )


def _parse(record):
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


class FakeHeadless(Headless):
    def launch(self, prompt, request: SessionRequest):
        spec = request.spec
        resume = request.resumed
        return Launch(
            _argv(self.adapter, prompt, spec, resume),
            transcript=os.path.join(spec.workdir, f"fake-{spec.sid}.jsonl"),
            resume=f"fake --resume {shlex.quote(spec.sid)}",
        )

    def parse(self, record):
        return _parse(record)


class FakeInProcess(InProcess):
    def run(self, prompt, request: SessionRequest):
        spec = request.spec
        resume = request.resumed
        config = self.adapter.config
        with open(config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "prompt": prompt,
                        "sid": spec.sid,
                        "resume": resume,
                        "pid": os.getpid(),
                        "thread": threading.get_ident(),
                    }
                )
                + "\n"
            )
        with open(config["scenario"], encoding="utf-8") as f:
            scenario = json.load(f)
        if isinstance(scenario, list):
            scenario = {"steps": scenario}
        for step in (
            scenario["resume"] if resume and "resume" in scenario else scenario.get("steps", [])
        ):
            if "out" in step:
                yield from _parse(step["out"])
            elif "err" in step:
                yield Stderr(step["err"])
            elif "sleep" in step:
                time.sleep(step["sleep"])
            elif "exit" in step:
                return
            elif "raise" in step:
                raise RuntimeError(step["raise"])


class FakeTui(Tui):
    hooks = {"stop": TURN_END, "ask": BLOCKED, "tool": ACTIVITY}

    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        resume = request.resumed
        return Launch((*_argv(self.adapter, prompt, spec, resume), *hook))

    def reply(self, payload):
        return payload.get("last_message") or ""

    def pending(self, payload):
        return len(payload.get("tasks", []))
