"""The test-only Adapter, client `fake`, importing only agent_pm.session.extension; caller-owned
loader tables supply it; its mode objects keep it. Config keys: scenario, log, tui (true: a Tui).
Its client is client.py (CLIENT), run by this Python; a limit record {resets_at} parses to
LimitHit, an exit record to Exited(99), which the session drops. InProcessFakeAdapter, in_process
only, logs {prompt, sid, resume, pid, thread} and runs out, err, sleep, exit and raise steps.
"""

import json
import os
import shlex
import sys
import threading
import time

from agent_pm.session.extension import (
    Activity,
    Adapter,
    Blocked,
    ClientBound,
    Exited,
    HookKind,
    InProcess,
    JsonHeadless,
    Launch,
    LimitHit,
    Reply,
    SessionInfo,
    SessionStatus,
    Spec,
    Stderr,
    Tui,
    TurnEnd,
)

CLIENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.py")


class FakeAdapter(Adapter):
    keys = frozenset({"scenario", "log", "tui"})
    client_note = "FAKE NOTE"

    def headless(self):
        return FakeHeadless(self)

    def tui(self):
        return FakeTui(self) if self.config.get("tui") else None


class InspectAdapter(FakeAdapter):
    def session_status(self, info: SessionInfo):
        return SessionStatus("yes", 1.5, f"{info.sid} {info.cwd} {info.transcript} {info.resume}")


class InProcessFakeAdapter(FakeAdapter):
    def headless(self):
        return None

    def tui(self):
        return None

    def in_process(self):
        return FakeInProcess(self)


def _argv(adapter, spec) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-S",
        CLIENT,
        "--scenario",
        adapter.config["scenario"],
        "--log",
        adapter.config["log"],
        "--sid",
        spec.sid,
        *(["--resume"] if spec.resume else []),
        "--",
        spec.prompt,
    )


def _parse(record):
    kind = record.get("type")
    if kind == "session":
        yield ClientBound(record["id"])
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


class FakeHeadless(JsonHeadless):
    def __init__(self, adapter: FakeAdapter):
        self.adapter = adapter

    def launch(self, spec: Spec):
        return Launch(
            _argv(self.adapter, spec),
            transcript=os.path.join(spec.workdir, f"fake-{spec.sid}.jsonl"),
            resume=f"fake --resume {shlex.quote(spec.sid)}",
        )

    def parse(self, record):
        return _parse(record)


class FakeInProcess(InProcess):
    def __init__(self, adapter: FakeAdapter):
        self.adapter = adapter

    def run(self, spec: Spec):
        config = self.adapter.config
        with open(config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "prompt": spec.prompt,
                        "sid": spec.sid,
                        "resume": spec.resume,
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
            scenario["resume"]
            if spec.resume and "resume" in scenario
            else scenario.get("steps", [])
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
    hooks = {"stop": HookKind.TURN_END, "ask": HookKind.BLOCKED, "tool": HookKind.ACTIVITY}

    def __init__(self, adapter: FakeAdapter):
        self.adapter = adapter

    def launch(self, spec: Spec, hook):
        return Launch((*_argv(self.adapter, spec), *hook))

    def reply(self, payload):
        return payload.get("last_message") or ""

    def pending(self, payload):
        return len(payload.get("tasks", []))
