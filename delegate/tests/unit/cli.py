"""Shared CLI and harness test setup over the configured fake runtime."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import cli

from delegate.tests.case import Case, local_section
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE, START, said
from session.tests import fake_adapter
from session.tests.modules import adapter_loader

SID = "11111111-2222-3333-4444-555555555555"
TUI = "dummy-tester-11111111"
DRIVER = f"{TUI}-drive"
SPLIT = "--split and --split-from need --detach --mode tui"


class FakeApplication(cli.Application):
    def __init__(self, case):
        self.case = case

    @property
    def driver(self):
        return self.case.driver

    def plan(self, config, spec):
        return cli.Application(self.driver).plan(config, spec)

    def run(self, config, spec, *, on_event):
        return cli.Application(self.driver).run(config, spec, on_event=on_event)


class Base(Case):
    """A HOME of the test's own, the fake runtime registered, the cwd a temp dir, workdir `work`
    (not made).
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
        self.configure_session(
            {
                "fake": {
                    "implementation": "agent_pm.session.adapters.fake:FakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )
        self.application = FakeApplication(self)
        self.steps(said(START, block(DONE)))

    def steps(self, *steps):
        with open(self.scenario, "w") as f:
            json.dump(list(steps), f)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(local_section(text, "delegate"))

    def cli(self, *argv, stdin=""):
        """(exit code, stdout, stderr) of cli.main(argv), an argparse exit's code included."""
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv), application=self.application)
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

    def launches(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def records(self) -> list[dict]:
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]
