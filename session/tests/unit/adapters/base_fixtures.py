"""Shared adapter fixtures for isolated session tests."""

from agent_pm.session.adapters.types import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Adapter,
    Headless,
    InProcess,
    Tui,
)
from agent_pm.session.engine.types import (
    ConfigError,
    Launch,
    Reply,
    SessionRequest,
)

from session.tests.case import Case

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class StubAdapter(Adapter):
    keys = frozenset({"model", "flags"})

    def headless(self):
        return StubHeadless()

    def tui(self):
        return StubTui()


class StubHeadless(Headless):
    def launch(self, prompt, _request: SessionRequest):
        return Launch(("stub", prompt))

    def parse(self, record):
        return [Reply(text) for text in record.get("texts", [])]


class StubTui(Tui):
    def launch(self, _prompt, _request: SessionRequest, hook):
        return Launch(("stub", *hook))

    def reply(self, payload):
        return payload.get("last", "")


class StubInProcess(InProcess):
    def run(self, prompt, _request: SessionRequest):
        yield Reply(prompt)


class CheckOnlyInProcess(InProcess):
    def check(self, _request: SessionRequest):
        return None


class LabeledTui(StubTui):
    hooks = {"end": TURN_END, "wait": BLOCKED, "work": ACTIVITY}


class DetailedTui(LabeledTui):
    def pending(self, payload):
        return len(payload.get("tasks", []))

    def activity(self, payload):
        return payload.get("doing", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class NativeIdTui(LabeledTui):
    def session_id(self, payload):
        return payload.get("thread", "")


class Refusals(Case):
    def refused(self, build, message):
        with self.assertRaises(ConfigError) as caught:
            build()
        self.assertEqual(str(caught.exception), message)
