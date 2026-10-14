"""Shared fixtures for unit and integration coverage."""

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import cli
from agent_pm.session import ConfigError

from delegate.tests.application_case import Base as ApplicationCase
from session.tests import fake_adapter

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


class Base(ApplicationCase):
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
