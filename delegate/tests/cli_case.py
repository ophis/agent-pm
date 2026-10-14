"""Shared fixtures for unit and integration coverage."""

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import cli
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import ConfigError

from delegate.tests.application_case import Base as ApplicationCase
from session.tests import fake_adapter

KEYS = {
    "client",
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


class Base(ApplicationCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        for key in ("TMUX", "TMUX_PANE"):
            os.environ.pop(key, None)

    ran = None

    def delegate_cls(self) -> type[HarnessDelegate]:
        """HarnessDelegate on this test's driver; its run returns self.ran when set. Its Session
        class is configured apart from the shared one.
        """
        case, driver = self, self.driver
        session_cls = type("CliSession", (driver.session_cls,), {})

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
                code = cli.main(
                    list(argv), delegate_cls=self.delegate_cls(), application=self.application
                )
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def runs(
        self,
        *extra,
        role="dummy-tester",
        task="echo",
        input="Hello.",
        client="fake",
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
            *(("--client", client) if client else ()),
            *extra,
            stdin=stdin,
        )
