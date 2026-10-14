"""Command-line fixture over the engine application's configured fake runtime."""

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import cli

from delegate.tests.unit.engine.application_case import Base as ApplicationCase


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
