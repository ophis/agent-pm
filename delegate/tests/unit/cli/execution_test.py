"""Execution behavior."""

import json
import os
import shutil
import sys
import types
import uuid
from datetime import datetime
from unittest import mock

from agent_pm.delegate import Outcome, Result, composer
from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.runner import STOP_LIMIT

from delegate.tests.case import committed
from delegate.tests.replies import block
from delegate.tests.unit.cli.case import KEYS, SID, Base, RefusingAdapter
from delegate.tests.unit.runs import DONE, START, STOP, FakeHandle, limit, no_child, said, turn


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

    def test_a_dry_run_configures_the_session(self):
        code, stdout, err = self.runs("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(stdout)["runtime"], "fake")

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
            with (
                self.subTest(want=want),
                mock.patch.object(self.application, "run", return_value=result),
            ):
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
        with mock.patch.object(self.application, "plan", wraps=self.application.plan) as planned:
            code, out, err = self.runs("--dry-run", out="-")
        self.assertEqual(code, 0, err)
        self.assertIsNone(planned.call_args.args[1].out)
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
        with mock.patch.object(self.application, "run", return_value=elsewhere):
            self.assertEqual(self.runs(), (0, "", "delegate: status done\n"))


class InProcess(Base):
    """The in_process-only fake, and RefusingAdapter (runtime `refusing`); no child process starts."""

    def setUp(self):
        super().setUp()
        refusing = types.ModuleType("agent_pm.session.adapters.refusing")
        refusing.RefusingAdapter = RefusingAdapter
        sys.modules[refusing.__name__] = refusing
        self.addCleanup(sys.modules.pop, refusing.__name__, None)
        classes = {"fake": "InProcessFakeAdapter", "refusing": "RefusingAdapter"}
        self.configure_session(
            {
                name: {
                    "implementation": f"adapters.{name}:{cls}",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name, cls in classes.items()
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
        self.configure_session(committed())
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
                with mock.patch.object(self.driver, "start", side_effect=handle.start) as start:
                    code, out, err = self.runs("--mode", "tui")
                self.assertEqual((code, out, err.splitlines()[-1]), (want, "", last))
                kw = start.call_args.kwargs
                self.assertEqual(
                    (start.call_args.args[:2], start.call_args.args[3].name, kw["options"].events),
                    (("fake", "tui"), None, None),
                )

    def test_a_runtime_without_tui_exits_2_and_a_dry_run_plans_it(self):
        code, out, err = self.runs("--mode", "tui")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.splitlines()[-1], "delegate: runtime 'fake' does not support tui mode")
        self.assertFalse(os.path.exists(self.work))
        code, out, _ = self.runs("--mode", "tui", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"], self.launches()), (0, "tui", []))
