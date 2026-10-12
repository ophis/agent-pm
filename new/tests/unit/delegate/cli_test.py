"""delegate/cli.py in process over the fake runtime: argv rules, --list, --dry-run, exit codes and messages; main's
drive_test Main, RoleRuns and TeamRuns cases of its command line; --mode in_process over the in_process-only fake."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

from agent_pm import composer
from agent_pm.adapters import base
from agent_pm.composer.prompt import RESUME
from agent_pm.delegate import Outcome, Result, cli, runner
from agent_pm.delegate.runner import STOP_LIMIT
from agent_pm.driver import registry

from new.tests import fake_adapter
from new.tests.case import PY, Case, child_env
from new.tests.unit.delegate.runner_test import (
    DONE,
    START,
    STOP,
    FakeHandle,
    block,
    limit,
    no_child,
    said,
    turn,
)

SID = "11111111-2222-3333-4444-555555555555"
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
    def check(self, prompt, spec, resume):
        raise base.ConfigError("refused by check")


class Base(Case):
    """A HOME of the test's own, the fake runtime registered, the cwd a temp dir, workdir `work` (not made)."""

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
        self.enterContext(mock.patch.dict(sys.modules, {"agent_pm.adapters.fake": fake_adapter}))
        registry.configure(
            {
                "adapters": {
                    "fake": {
                        "implementation": "adapters.fake:FakeAdapter",
                        "scenario": self.scenario,
                        "log": self.log,
                    }
                }
            }
        )
        self.steps(said(START, block(DONE)))

    def steps(self, *steps):
        with open(self.scenario, "w") as f:
            json.dump(list(steps), f)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write(text)

    def cli(self, *argv, stdin=""):
        """(exit code, stdout, stderr) of cli.main(argv), an argparse exit's code included."""
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv))
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


class List(Base):
    def test_list_prints_each_role_and_task_with_its_source(self):
        code, out, err = self.cli("--list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line for line in lines if not line.startswith("  ")], HEADS)
        echo = composer.index("dummy-tester")["echo"]
        self.assertTrue(
            out.endswith(f"\ndummy-tester  local  built-in\n  echo: {echo}  [built-in]\n"), out
        )
        self.assertTrue(
            all(line.endswith("  [built-in]") for line in lines if line.startswith("  "))
        )
        self.assertNotIn("<tasks>", out)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_list_abbreviated_is_list_and_takes_no_other_option(self):
        self.assertEqual(self.cli("--li"), self.cli("--list"))
        for argv, extra in (
            (["--list", "--role", "pm"], "--role"),
            (["--li", "--rol", "pm"], "--rol"),
            (["--list", "--dry-run"], "--dry-run"),
            (["--list", "--runtime=fake"], "--runtime=fake"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"--list takes no other option, not {extra}\n", err)
        code, out, err = self.cli()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the following arguments are required: --role", err)

    def test_list_stops_at_a_config_defect(self):
        for local, want in (
            (
                "[roles.ghost]\ntier = 3\n",
                "roles.ghost: no charter roles/ghost.md in core or team_dirs",
            ),
            ('team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.assertEqual(self.cli("--list"), (2, "", f"delegate: {want}\n"))

    def test_it_runs_as_a_module_from_the_import_root(self):
        done = subprocess.run(
            [*PY, "-m", "agent_pm.delegate.cli", "--list"],
            capture_output=True,
            text=True,
            env=child_env(),
            cwd=self.tmp,
            timeout=60,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, self.cli("--list")[1])


class Usage(Base):
    def test_a_run_needs_input_out_and_workdir(self):
        for argv in (
            ("--out", "o.md"),
            ("--input", "x", "--workdir", self.work),
            ("--input", "x", "--out", "o.md"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.cli("--role", "dummy-tester", "--runtime", "fake", *argv),
                    (2, "", "delegate: --input, --out and --workdir are required\n"),
                )
        self.assertEqual(self.launches(), [])

    def test_resume_needs_sid(self):
        self.assertEqual(self.runs("--resume"), (2, "", "delegate: --resume needs --sid\n"))
        self.assertEqual(self.launches(), [])

    def test_runtime_given_twice_exits_2(self):
        for extra in (("--runtime", "fake"), ("--runtime=fake",)):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--runtime given twice", err)
        self.assertEqual(self.launches(), [])

    def test_out_or_workdir_given_twice_exits_2_before_anything_starts(self):
        for extra, error in (
            (("--out", "b.md"), "--out given twice"),
            (("--workdir", "b"), "--workdir given twice"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(error, err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_a_config_error_exits_2_before_anything_starts(self):
        cases = [
            ("an unknown role", {"role": "ghost", "task": None}, "", "unknown role 'ghost'"),
            (
                "a task off the index",
                {"role": "engineer", "task": "deep-research"},
                "",
                "task 'deep-research' is not one of engineer's tasks (build, light-build)",
            ),
            (
                "a bad team_dirs",
                {},
                'team_dirs = "x"\n',
                "team_dirs: want a list of absolute or ~ paths, got 'x'",
            ),
            (
                "a bad tier",
                {},
                "[roles.dummy-tester]\ntier = 9\n",
                "tier must be an integer 1–4, got 9",
            ),
            ("an unknown runtime", {"runtime": "nope"}, "", "no [adapters.nope] table"),
        ]
        for name, kw, local, want in cases:
            self.local(local)
            for dry in ((), ("--dry-run",)) if name != "an unknown runtime" else ((),):
                with self.subTest(name, dry=dry):
                    code, out, err = self.runs(*dry, **kw)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertTrue(err.splitlines()[-1].startswith(f"delegate: {want}"), err)
                    self.assertFalse(os.path.exists(self.work))
        self.assertEqual(self.launches(), [])


class Input(Base):
    def test_input_is_text_from_the_argument_or_stdin_never_a_files_content(self):
        with open(os.path.join(self.tmp, "input.md"), "w") as f:
            f.write("SECRET")
        for given, stdin, want in (("input.md", "", "input.md"), ("-", "Use B.\n", "Use B.\n")):
            with self.subTest(given=given):
                code, _, err = self.runs(input=given, stdin=stdin)
                self.assertEqual(code, 0, err)
                inputs = [r["text"] for r in self.records() if r["kind"] == "input"]
                prompt = self.launches()[-1]["argv"][-1]
                self.assertEqual(inputs[-1], want)
                self.assertTrue(prompt.endswith(f"\n# Input\n\n{want.strip()}\n"), prompt[-100:])
                self.assertNotIn("SECRET", prompt)


class DryRun(Base):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        code, out, err = self.runs("--sid", SID, "--dry-run")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(set(shown), KEYS)
        self.assertEqual(
            {k: v for k, v in shown.items() if k not in ("dirs", "reads", "commands", "prompt")},
            {
                "runtime": "fake",
                "mode": "headless",
                "sid": SID,
                "resume": False,
                "cwd": self.tmp,
                "project": False,
                "tier": 3,
                "effort": "low",
            },
        )
        self.assertEqual(
            (shown["dirs"], shown["reads"], shown["commands"]),
            ([self.work], [os.path.join(composer.TEAM, "tasks")], []),
        )
        self.assertIn("**Your task**: `echo` (`<tasks>/echo.md`).", shown["prompt"])
        self.assertTrue(shown["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(
            err, f"delegate: cwd {self.tmp} is not in trusted_dirs: its project settings are off\n"
        )
        self.assertEqual(
            (self.launches(), os.path.exists(self.work), os.listdir(self.agent_pm)), ([], False, [])
        )

    def test_a_new_run_gets_a_sid_and_a_resume_continues_its_task(self):
        code, out, _ = self.runs("--dry-run", task=None)
        shown = json.loads(out)
        self.assertEqual((code, uuid.UUID(shown["sid"]).version, shown["resume"]), (0, 4, False))
        self.assertIn("**Your task**: pick it from", shown["prompt"])
        code, out, _ = self.runs("--sid", SID, "--resume", "--dry-run")
        shown = json.loads(out)
        self.assertEqual(
            (code, shown["sid"], shown["resume"], shown["cwd"]), (0, SID, True, self.work)
        )
        self.assertTrue(shown["prompt"].startswith(RESUME))
        self.assertFalse(os.path.exists(self.work))

    def test_the_runtime_defaults_to_the_roles_in_the_config(self):
        code, out, _ = self.runs("--dry-run", runtime=None)
        self.assertEqual(
            (code, json.loads(out)["runtime"]), (0, composer.resolve("dummy-tester").runtime)
        )
        self.local('runtime = "fake"\n')
        code, _, err = self.runs(runtime=None)
        self.assertEqual((code, len(self.launches())), (0, 1), err)


class Exits(Base):
    def test_a_done_run_exits_0_naming_its_session_and_status(self):
        code, out, err = self.runs("--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        lines = err.splitlines()
        self.assertLess(
            lines.index(f"delegate: session {SID}"), lines.index("Progress (start): go")
        )
        self.assertTrue(err.endswith("delegate: status done\n"), err)
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.assertEqual([r["kind"] for r in self.records()][-2:], ["result", "end"])

    def test_a_resume_continues_its_session(self):
        code, _, err = self.runs("--sid", SID, "--resume")
        self.assertEqual(code, 0, err)
        (launch,) = self.launches()
        argv = launch["argv"]
        self.assertEqual((argv[argv.index("--sid") + 1], "--resume" in argv), (SID, True))
        self.assertTrue(argv[-1].startswith(RESUME))

    def test_exit_codes(self):
        at = 1767225600.0
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        cases = [
            (
                "valid, the client exited 7",
                [said(START, block(DONE)), {"exit": 7}],
                0,
                "delegate: status done",
            ),
            (
                "needs_input",
                [said(START, block({**DONE, "status": "needs_input", "questions": ["Q?"]}))],
                0,
                "delegate: status needs_input",
            ),
            ("no outcome", [said(START)], 1, "delegate: the agent run returned no outcome"),
            (
                "invalid twice",
                [said(START, block({**DONE, "status": "maybe"}))],
                1,
                "delegate: invalid outcome: status 'maybe' is not one of done, needs_input, failed",
            ),
            (
                "no outcome, the client exited 5",
                [said(START), {"exit": 5}],
                3,
                "delegate: the client exited 5",
            ),
            (
                "a usage limit",
                [said(START), limit(at), {"sleep": 30}],
                3,
                f"delegate: usage limit; resets at {when}",
            ),
            (
                "an unknown reset time",
                [said(START), limit(None), {"sleep": 30}],
                3,
                "delegate: usage limit; reset time unknown",
            ),
        ]
        for name, steps, want, last in cases:
            with self.subTest(name):
                shutil.rmtree(self.work, ignore_errors=True)
                self.steps(*steps)
                code, out, err = self.runs()
                self.assertEqual((code, out), (want, ""), err)
                self.assertTrue(err.endswith(f"\n{last}\n"), err)

    def test_a_client_that_cannot_start_exits_3(self):
        with mock.patch(
            "subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")
        ):
            code, out, err = self.runs()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("\ndelegate: headless: cannot start ", err)

    def test_agent_text_is_printed_printable(self):
        for result, want, line in (
            (
                Result(0, None, "invalid outcome: a\x1b[2Jb\x07"),
                1,
                "delegate: invalid outcome: a[2Jb",
            ),
            (
                Result(9, Outcome("done", "t", "s"), "the client exited 9"),
                0,
                "delegate: status done",
            ),
        ):
            with self.subTest(want=want), mock.patch.object(cli, "run", return_value=result):
                self.assertEqual(self.runs(), (want, "", f"{line}\n"))


class Deliverable(Base):
    """--out -: the deliverable on stdout; --out PATH: a stderr line naming the file holding it."""

    def test_out_dash_prints_the_deliverable_exactly_and_saves_nothing(self):
        code, out, err = self.runs(out="-")
        self.assertEqual((code, out), (0, DONE["deliverable"]), err)
        self.assertNotIn("delegate: deliverable", err)
        self.assertTrue(err.endswith("delegate: status done\n"), err)
        self.assertEqual(os.listdir(self.work), ["run.jsonl"])

    def test_out_dash_drops_control_characters_but_newlines_and_tabs(self):
        text = "a\x1b]0;x\x07b\n\tc\x1b[2Jd\x9be\n"
        self.steps(said(START, block({**DONE, "deliverable": text})))
        code, out, err = self.runs(out="-")
        self.assertEqual((code, out), (0, "a]0;xb\n\tc[2Jde\n"), err)

    def test_a_dry_run_with_out_dash_plans_no_out(self):
        with mock.patch.object(cli, "plan", wraps=cli.plan) as planned:
            code, out, err = self.runs("--dry-run", out="-")
        self.assertEqual(code, 0, err)
        self.assertIsNone(planned.call_args.kwargs["out"])
        self.assertIn("`<Workdir>/tmp/deliverable.md`", json.loads(out)["prompt"])
        self.assertFalse(os.path.exists(self.work))

    def test_out_path_is_named_on_stderr_holding_the_deliverable(self):
        code, out, err = self.runs(out=os.path.join("work", "out.md"))
        self.assertEqual((code, out), (0, ""), err)
        self.assertTrue(
            err.endswith(f"\ndelegate: deliverable {self.out}\ndelegate: status done\n"), err
        )

    def test_no_path_line_without_a_deliverable_at_out(self):
        asking = {**DONE, "status": "needs_input", "questions": ["Q?"], "deliverable": ""}
        self.steps(said(START, block(asking)))
        code, out, err = self.runs()
        self.assertEqual((code, out), (0, ""), err)
        self.assertNotIn("delegate: deliverable", err)
        elsewhere = Result(0, Outcome("done", "t", "s", deliverable="# Doc\n"))
        with mock.patch.object(cli, "run", return_value=elsewhere):
            self.assertEqual(self.runs(), (0, "", "delegate: status done\n"))


class InProcess(Base):
    """The in_process-only fake, and RefusingAdapter (runtime `refusing`); no child process starts."""

    def setUp(self):
        super().setUp()
        refusing = types.ModuleType("agent_pm.adapters.refusing")
        refusing.RefusingAdapter = RefusingAdapter
        self.enterContext(mock.patch.dict(sys.modules, {"agent_pm.adapters.refusing": refusing}))
        classes = {"fake": "InProcessFakeAdapter", "refusing": "RefusingAdapter"}
        registry.configure(
            {
                "adapters": {
                    name: {
                        "implementation": f"adapters.{name}:{cls}",
                        "scenario": self.scenario,
                        "log": self.log,
                    }
                    for name, cls in classes.items()
                }
            }
        )

    def runs(self, *extra, **kw):
        with no_child():
            return super().runs(*extra, **kw)

    def test_a_done_run_exits_0_naming_its_session_and_status(self):
        code, out, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        lines = err.splitlines()
        self.assertLess(
            lines.index(f"delegate: session {SID}"), lines.index("Progress (start): go")
        )
        self.assertLess(lines.index("Progress (start): go"), lines.index("delegate: status done"))
        self.assertTrue(err.endswith("delegate: status done\n"), err)
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.assertEqual(
            [r["kind"] for r in self.records()], ["session", "input", "progress", "result", "end"]
        )
        self.assertEqual([(e["sid"], e["pid"]) for e in self.launches()], [(SID, os.getpid())])

    def test_headless_and_tui_exit_2_and_nothing_runs(self):
        for mode, want in (
            ("headless", "runtime 'fake' does not support headless mode"),
            ("tui", "runtime 'fake' does not support tui mode"),
        ):
            with self.subTest(mode=mode):
                code, out, err = self.runs("--mode", mode)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_claude_and_codex_refuse_it_with_exit_2(self):
        registry.configure({"adapters": registry.tables(registry.CONFIG, os.devnull)})
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                code, out, err = self.runs("--mode", "in_process", runtime=runtime)
                self.assertEqual(
                    (code, out, err.splitlines()[-1]),
                    (2, "", f"delegate: runtime {runtime!r} does not support in_process mode"),
                )
                self.assertFalse(os.path.exists(self.work))

    def test_dry_run_shows_the_mode(self):
        code, out, err = self.runs("--mode", "in_process", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"]), (0, "in_process"), err)
        self.assertFalse(os.path.exists(self.work))

    def test_a_check_refusal_exits_2_with_no_record(self):
        code, out, err = self.runs("--mode", "in_process", runtime="refusing")
        self.assertEqual((code, out, err.splitlines()[-1]), (2, "", "delegate: refused by check"))
        self.assertEqual(
            (os.path.exists(os.path.join(self.work, "run.jsonl")), self.launches()), (False, [])
        )


class Tui(Base):
    def test_tui_mode_runs_the_tui_policy_in_the_drivers_default_session_a_give_up_exiting_1(self):
        for events, want, last in (
            ([turn(START, block(DONE))], 0, "delegate: status done"),
            ([STOP] * (1 + STOP_LIMIT), 1, "delegate: the agent run returned no outcome"),
        ):
            with self.subTest(want=want):
                handle = FakeHandle(events)
                with mock.patch.object(runner.api, "start", side_effect=handle.start) as start:
                    code, out, err = self.runs("--mode", "tui")
                self.assertEqual((code, out, err.splitlines()[-1]), (want, "", last))
                kw = start.call_args.kwargs
                self.assertEqual(
                    (start.call_args.args[:2], kw["name"], kw["events"]),
                    (("fake", "tui"), None, None),
                )

    def test_a_runtime_without_tui_exits_2_and_a_dry_run_plans_it(self):
        code, out, err = self.runs("--mode", "tui")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.splitlines()[-1], "delegate: runtime 'fake' does not support tui mode")
        self.assertFalse(os.path.exists(self.work))
        code, out, _ = self.runs("--mode", "tui", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"], self.launches()), (0, "tui", []))


if __name__ == "__main__":
    unittest.main()
