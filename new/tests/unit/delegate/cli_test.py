"""delegate/cli.py in process over the fake runtime: argv rules, --list, --dry-run, exit codes and messages; main's
drive_test Main, RoleRuns and TeamRuns cases of its command line."""
from new.tests import fake_adapter, hermetic
from new.tests.unit.delegate.runner_test import DONE, START, block, limit, said

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

import composer
from composer.prompt import RESUME
from delegate import Outcome, Result, cli
from driver import registry

SID = "11111111-2222-3333-4444-555555555555"
KEYS = {"runtime", "mode", "sid", "resume", "cwd", "project", "dirs", "commands", "tier", "effort", "prompt"}
HEADS = ["researcher  local  built-in", "pm  local  built-in", "engineer  pull-request  built-in",
         "dummy-tester  local  built-in"]


class Base(unittest.TestCase):
    """A HOME of the test's own, the fake runtime registered, the cwd a temp dir, workdir `work` (not made)."""
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work, self.scenario, self.log = (os.path.join(self.tmp, n) for n in ("work", "scenario.json", "fake.log"))
        self.out = os.path.join(self.work, "out.md")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.enterContext(mock.patch.dict(sys.modules, {"adapters.fake": fake_adapter}))
        self.addCleanup(registry.reset)
        registry.configure({"adapters": {"fake": {"implementation": "adapters.fake:FakeAdapter",
                                                  "scenario": self.scenario, "log": self.log}}})
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
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def runs(self, *extra, role="dummy-tester", task="echo", input="Hello.", runtime="fake", stdin=""):
        return self.cli("--role", role, *(("--task", task) if task else ()), "--input", input, "--out", self.out,
                        "--workdir", self.work, *(("--runtime", runtime) if runtime else ()), *extra, stdin=stdin)

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
        self.assertTrue(out.endswith(f"\ndummy-tester  local  built-in\n  echo: {echo}  [built-in]\n"), out)
        self.assertTrue(all(line.endswith("  [built-in]") for line in lines if line.startswith("  ")))
        self.assertNotIn("<tasks>", out)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_list_abbreviated_is_list_and_takes_no_other_option(self):
        self.assertEqual(self.cli("--li"), self.cli("--list"))
        for argv, extra in ((["--list", "--role", "pm"], "--role"), (["--li", "--rol", "pm"], "--rol"),
                            (["--list", "--dry-run"], "--dry-run"), (["--list", "--runtime=fake"], "--runtime=fake")):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"--list takes no other option, not {extra}\n", err)
        code, out, err = self.cli()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the following arguments are required: --role", err)

    def test_list_stops_at_a_config_defect(self):
        for local, want in (("[roles.ghost]\ntier = 3\n",
                             "roles.ghost: no charter roles/ghost.md in core or team_dirs"),
                            ('team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'")):
            with self.subTest(local=local):
                self.local(local)
                self.assertEqual(self.cli("--list"), (2, "", f"delegate: {want}\n"))

    def test_the_script_puts_src_first_in_place_of_its_own_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run([sys.executable, cli.__file__, "--list"], capture_output=True, text=True, env=env,
                              cwd=self.tmp, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, self.cli("--list")[1])


class Usage(Base):
    def test_a_run_needs_input_out_and_workdir(self):
        for argv in (("--out", "o.md"), ("--input", "x", "--workdir", self.work), ("--input", "x", "--out", "o.md")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli("--role", "dummy-tester", "--runtime", "fake", *argv),
                                 (2, "", "delegate: --input, --out and --workdir are required\n"))
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

    def test_a_config_error_exits_2_before_anything_starts(self):
        cases = [("an unknown role", {"role": "ghost", "task": None}, "", "unknown role 'ghost'"),
                 ("a task off the index", {"role": "engineer", "task": "deep-research"}, "",
                  "task 'deep-research' is not one of engineer's tasks (build, light-build)"),
                 ("tui", {"mode": "tui"}, "", "tui mode is not available yet"),
                 ("a bad team_dirs", {}, 'team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
                 ("a bad tier", {}, "[roles.dummy-tester]\ntier = 9\n", "tier must be an integer 1–4, got 9"),
                 ("an unknown runtime", {"runtime": "nope"}, "", "no [adapters.nope] table")]
        for name, kw, local, want in cases:
            self.local(local)
            mode = ("--mode", kw.pop("mode")) if "mode" in kw else ()
            for dry in ((), ("--dry-run",)) if name != "an unknown runtime" else ((),):
                with self.subTest(name, dry=dry):
                    code, out, err = self.runs(*mode, *dry, **kw)
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
        self.assertEqual({k: v for k, v in shown.items() if k not in ("dirs", "commands", "prompt")},
                         {"runtime": "fake", "mode": "headless", "sid": SID, "resume": False, "cwd": self.tmp,
                          "project": False, "tier": 3, "effort": "low"})
        self.assertIn(os.path.join(composer.TEAM, "tasks"), shown["dirs"])
        self.assertEqual(shown["commands"], [])
        self.assertIn("**Your task**: `echo` (`<tasks>/echo.md`).", shown["prompt"])
        self.assertTrue(shown["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(err, f"delegate: cwd {self.tmp} is not in trusted_dirs: its project settings are off\n")
        self.assertEqual((self.launches(), os.path.exists(self.work), os.listdir(self.agent_pm)), ([], False, []))

    def test_a_new_run_gets_a_sid_and_a_resume_continues_its_task(self):
        code, out, _ = self.runs("--dry-run", task=None)
        shown = json.loads(out)
        self.assertEqual((code, uuid.UUID(shown["sid"]).version, shown["resume"]), (0, 4, False))
        self.assertIn("**Your task**: pick it from", shown["prompt"])
        code, out, _ = self.runs("--sid", SID, "--resume", "--dry-run")
        shown = json.loads(out)
        self.assertEqual((code, shown["sid"], shown["resume"], shown["cwd"]), (0, SID, True, self.work))
        self.assertTrue(shown["prompt"].startswith(RESUME))
        self.assertFalse(os.path.exists(self.work))

    def test_the_runtime_defaults_to_the_roles_in_the_config(self):
        code, out, _ = self.runs("--dry-run", runtime=None)
        self.assertEqual((code, json.loads(out)["runtime"]), (0, composer.resolve("dummy-tester").runtime))
        self.local('runtime = "fake"\n')
        code, _, err = self.runs(runtime=None)
        self.assertEqual((code, len(self.launches())), (0, 1), err)


class Exits(Base):
    def test_a_done_run_exits_0_naming_its_session_and_status(self):
        code, out, err = self.runs("--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        lines = err.splitlines()
        self.assertLess(lines.index(f"delegate: session {SID}"), lines.index("Progress (start): go"))
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
        cases = [("valid, the client exited 7", [said(START, block(DONE)), {"exit": 7}], 0, "delegate: status done"),
                 ("needs_input", [said(START, block({**DONE, "status": "needs_input", "questions": ["Q?"]}))], 0,
                  "delegate: status needs_input"),
                 ("no outcome", [said(START)], 1, "delegate: the agent run returned no outcome"),
                 ("invalid twice", [said(START, block({**DONE, "status": "maybe"}))], 1,
                  "delegate: invalid outcome: status 'maybe' is not one of done, needs_input, failed"),
                 ("no outcome, the client exited 5", [said(START), {"exit": 5}], 3, "delegate: the client exited 5"),
                 ("a usage limit", [said(START), limit(at), {"sleep": 30}], 3,
                  f"delegate: usage limit; resets at {when}"),
                 ("an unknown reset time", [said(START), limit(None), {"sleep": 30}], 3,
                  "delegate: usage limit; reset time unknown")]
        for name, steps, want, last in cases:
            with self.subTest(name):
                shutil.rmtree(self.work, ignore_errors=True)
                self.steps(*steps)
                code, out, err = self.runs()
                self.assertEqual((code, out), (want, ""), err)
                self.assertTrue(err.endswith(f"\n{last}\n"), err)

    def test_a_client_that_cannot_start_exits_3(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")):
            code, out, err = self.runs()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("\ndelegate: headless: cannot start ", err)

    def test_agent_text_is_printed_printable(self):
        for result, want, line in ((Result(0, None, "invalid outcome: a\x1b[2Jb\x07"), 1,
                                    "delegate: invalid outcome: a[2Jb"),
                                   (Result(9, Outcome("done", "t", "s"), "the client exited 9"), 0,
                                    "delegate: status done")):
            with self.subTest(want=want), mock.patch.object(cli, "run", return_value=result):
                self.assertEqual(self.runs(), (want, "", f"{line}\n"))


if __name__ == "__main__":
    unittest.main()
