"""Shared fixtures for unit and integration coverage."""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import Result, Spec, composer, run

from delegate.tests.case import Case
from delegate.tests.unit.runs import DONE, NAME, turn
from session.tests import fake_adapter
from session.tests.modules import adapter_loader

SID = "11111111-2222-3333-4444-555555555555"


NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}


FAILED = {**DONE, "status": "failed", "deliverable": ""}


INVALID = {**DONE, "status": "maybe"}


INVALID_ERROR = "status 'maybe' is not one of done, needs_input, failed"


GH = {"type": "github", "repo": "o/docs", "branch": "main", "dir": "R/"}


PR = {"type": "pull-request"}


def ended(text) -> dict:
    """A step printing a result record (TurnEnd) of `text`."""
    return {"out": {"type": "result", "text": text}}


PENDING = turn("Waiting for the build.", pending=1)


class Base(Case):
    """dummy-tester's echo over the fake runtime in workdir `work` (not made), out `out`, the cwd
    the workdir.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.channel = os.path.join(self.work, "run.jsonl")
        self.scenario, self.log = (
            os.path.join(self.tmp, name) for name in ("scenario.json", "runtime.log")
        )
        self.driver_dependencies = {
            "class_loader": adapter_loader({"agent_pm.session.adapters.fake": fake_adapter})
        }
        self.configure_session(
            {
                "fake": {
                    "implementation": "agent_pm.session.adapters.fake:FakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, fixes=None, output=None, on_event=None, **kw) -> Result:
        """run() over the fake with `steps` (`fixes`: its steps on --resume); self.events gets each
        callback, self.err the stderr.
        """
        with open(self.scenario, "w") as f:
            json.dump(
                {"steps": list(steps), **({"resume": list(fixes)} if fixes is not None else {})}, f
            )
        config = composer.resolve("dummy-tester")
        config = replace(config, output=output) if output else config
        self.events, err = [], io.StringIO()

        def on(event):
            self.events.append(event)
            if on_event:
                on_event(event)

        kw = {
            "runtime": "fake",
            "mode": "headless",
            "out": self.out,
            "cwd": self.work,
            "input": "Echo me.",
            **kw,
        }
        try:
            with redirect_stderr(err):
                return run(
                    config,
                    Spec(kw.pop("input"), self.work, task="echo", **kw),
                    on_event=on,
                    driver=self.driver,
                )
        finally:
            self.err = err.getvalue()

    def records(self) -> list[dict]:
        with open(self.channel) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def launches(self) -> list[dict]:
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path) -> str:
        with open(path) as f:
            return f.read()

    def write(self, path, text) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def fresh(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def spy_save(self):
        patch = mock.patch("os.replace", wraps=os.replace)
        self.addCleanup(patch.stop)
        return patch.start()


LEFT_OPEN = f"session {NAME} left open: tmux attach -t '={NAME}'\n"


class UnstreamedHeadless(fake_adapter.FakeHeadless):
    def events(self, lines):
        raise ValueError("no stream")

class UnstreamedAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return UnstreamedHeadless(self)
