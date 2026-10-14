"""Shared CLI fixtures for unit and integration coverage over the configured fake runtime."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import cli
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import ConfigError

from delegate.tests.case import Case
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE, START, said
from session.tests import fake_adapter
from session.tests.modules import adapter_loader
from utils.tests.case import local_section

SID = "11111111-2222-3333-4444-555555555555"
TUI = "dummy-tester-11111111"
HARNESS = f"{TUI}-drive"

KEYS = {
    "runtime",
    "mode",
    "sid",
    "resume",
    "cwd",
    "project",
    "dirs",
    "reads",
    "commands",
    "tier",
    "effort",
    "prompt",
}
PLACEMENT = {"sessions", "hosts", "callback"}


HEADS = [
    "researcher  local  built-in",
    "pm  local  built-in",
    "engineer  pull-request  built-in",
    "dummy-tester  local  built-in",
]


class RefusingAdapter(fake_adapter.InProcessFakeAdapter):
    def in_process(self):
        return RefusingRun(self)


class RefusingRun(fake_adapter.FakeInProcess):
    def __init__(self, adapter):
        super().__init__(adapter)
        self.refusal = ConfigError("refused by check")

    def check(self, spec):
        raise self.refusal


class Base(Case):
    """A HOME of the test's own, the fake runtime registered, the cwd a temp dir, workdir `work`
    (not made), TMUX unset.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work, self.scenario, self.log = (
            os.path.join(self.tmp, n) for n in ("work", "scenario.json", "fake.log")
        )
        self.out = os.path.join(self.work, "out.md")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.driver_dependencies = {
            "class_loader": adapter_loader({"agent_pm.session.adapters.fake": fake_adapter})
        }
        self.configure_fake()
        self.steps(said(START, block(DONE)))
        self.enterContext(mock.patch.dict(os.environ))
        for key in ("TMUX", "TMUX_PANE"):
            os.environ.pop(key, None)

    def configure_fake(self, *, tui=False):
        self.configure_session(
            {
                "fake": {
                    "implementation": "agent_pm.session.adapters.fake:FakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                    "tui": tui,
                }
            }
        )

    def steps(self, *steps):
        with open(self.scenario, "w") as f:
            json.dump(list(steps), f)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(local_section(text, "delegate"))

    def launches(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def records(self) -> list[dict]:
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    ran = None

    def delegate_cls(self) -> type[HarnessDelegate]:
        """HarnessDelegate on this test's driver; its run returns self.ran when set. Its Session
        class is configured apart from the shared one and loads no class by name (the driver holds
        the fake runtime).
        """
        case, driver = self, self.driver
        session_cls = type(
            "CliSession",
            (driver.session_cls,),
            {"class_roots": classmethod(lambda _cls, _runtime: ())},
        )

        class Driven(HarnessDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, driver=driver, session_cls=session_cls, **placement)

            def run(self, on_event=None):
                return super().run(on_event) if case.ran is None else case.ran

            @classmethod
            def preview(cls, role, spec, **placement):
                return super().preview(
                    role, spec, driver=driver, session_cls=session_cls, **placement
                )

        return Driven

    def cli(self, *argv, stdin=""):
        """(exit code, stdout, stderr) of cli.main(argv), an argparse exit's code included."""
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv), delegate_cls=self.delegate_cls())
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def runs(
        self,
        *extra,
        role="dummy-tester",
        task="echo",
        input="Hello.",
        runtime="fake",
        stdin="",
        out=None,
    ):
        return self.cli(
            "run",
            "--role",
            role,
            *(("--task", task) if task else ()),
            "--input",
            input,
            "--out",
            out or self.out,
            "--workdir",
            self.work,
            *(("--runtime", runtime) if runtime else ()),
            *extra,
            stdin=stdin,
        )
