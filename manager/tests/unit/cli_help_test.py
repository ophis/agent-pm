import argparse
import contextlib
import io
import os
import shlex
from pathlib import Path
from unittest import mock

from agent_pm.manager import cli

from manager.tests.case import Case

COMMANDS = [
    "hold",
    "run",
    "worker",
    "issue",
    "role",
    "attach",
    "status",
    "report",
    "resume",
    "hide",
    "show",
    "close",
    "forget",
    "close-all",
    "note",
    "release",
    "next-event",
]


class Help(Case):
    def help(self, command=None, flag="--help"):
        out = io.StringIO()
        argv = ([] if command is None else [command]) + [flag]
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.main(argv)
        self.assertEqual(raised.exception.code, 0)
        return out.getvalue()

    def test_every_surface_has_purpose_and_safe_help_under_invalid_runtime_context(self):
        before = sorted(Path(self.home).rglob("*"))
        source = mock.Mock()
        source.read.side_effect = AssertionError("help read stdin")
        with (
            mock.patch.dict(
                os.environ,
                {"AGENT_PM_AGENT_SESSION": "1", "AGENT_PM_HOLDER_ID": "invalid"},
            ),
            mock.patch("sys.stdin", source),
            mock.patch("subprocess.run") as run,
            mock.patch("subprocess.Popen") as popen,
        ):
            for command in [None, *COMMANDS]:
                for flag in ("--help", "-h"):
                    with self.subTest(command=command, flag=flag):
                        text = self.help(command, flag)
                        self.assertIn("agent-pm-crew", text)
                        if command is not None:
                            self.assertIn("Examples:", text)
                            self.assertIn("agent-pm-crew --help", text)
            run.assert_not_called()
            popen.assert_not_called()
        self.assertEqual(sorted(Path(self.home).rglob("*")), before)

    def test_root_lists_each_command_with_a_purpose(self):
        parser = cli._parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(sub.choices), set(COMMANDS))
        self.assertEqual({a.dest for a in sub._choices_actions}, set(COMMANDS))
        self.assertTrue(all(a.help for a in sub._choices_actions))

    def test_boundary_help_and_recovery_are_explicit(self):
        for verb, owner in (
            ("worker", "agent-pm-session start --help"),
            ("issue", "agent-pm-linear router --help"),
            ("role", "agent-pm-delegate run --help"),
        ):
            with self.subTest(verb=verb):
                text = self.help(verb)
                self.assertIn(owner, text)
                self.assertIn("before", text)
                self.assertIn("Forwarded --help", text)
        self.assertIn("-- COMMAND [ARG ...]", self.help("hold"))
        self.assertIn("-- COMMAND [ARG ...]", self.help("run"))
        self.assertIn("--after and --gen", self.help("attach"))
        self.assertIn("monitor expired", self.help("next-event"))

    def test_published_worker_example_keeps_session_and_client_tokens(self):
        parser = cli._parser()
        argv = shlex.split(
            "worker --manager demo helper --client codex --display none -- --model 'a b'"
        )
        parsed = parser.parse_args(argv)
        self.assertEqual((parsed.manager, parsed.key), ("demo", "helper"))
        self.assertEqual(
            parsed.arguments, ["--client", "codex", "--display", "none", "--", "--model", "a b"]
        )
        self.assertEqual(parser.parse_args(["worker", "helper", "--help"]).arguments, ["--help"])

    def test_command_tail_is_not_interpreted_as_outer_help(self):
        with mock.patch.object(cli, "_hold", return_value=0) as hold:
            self.assertEqual(cli.main(["hold", "--manager", "demo", "--", "codex", "--help"]), 0)
        self.assertEqual(hold.call_args.args[0].command, ("codex", "--help"))
