import io
import os
from contextlib import redirect_stderr

from agent_pm.linear_orchestrator.engine import config, run
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.run.cli_fixtures import Cli as CliBase
from orchestrator_test_helpers.unit.run.data import NOW, RUNTIME
from orchestrator_test_helpers.unit.run_fixtures import ID, SID


class Cli1(CliBase):
    def test_tui_is_off_by_default_and_main_hands_delegate_the_config_and_driver(self):
        self.done()
        self.assertEqual(self.main(), 0)
        role, spec = self.call()
        self.assertEqual((spec.mode, spec.runtime), ("headless", RUNTIME))
        self.assertEqual(role, config.load().roles["engineer"].run)
        self.assertIs(self.delegate.driver, self.api)
        self.assertEqual(linear.LOGS_DIR, self.logs)
        self.assertEqual(self.err, "")

    def test_tui_options_need_tui(self):
        for extra in (
            ["--split", "right"],
            ["--split-from", "dev"],
            ["--events", os.path.join(self.work, "events.log")],
            ["--opener", "mine"],
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.main(*extra), 2)
                self.assertEqual(
                    self.err, "run.py: --split, --split-from, --opener and --events need --tui\n"
                )
        self.nothing_ran()

    def test_argparse_errors(self):
        argv = self.argv()
        no_runtime = [x for x in argv if not x.startswith("--runtime")]
        for bad in (
            argv[:-1],
            argv[2:],
            no_runtime,
            argv + ["--k", "1"],
            argv + ["--runner", "tui"],
            argv + ["--tui=1"],
            argv + ["--mode", "later"],
        ):
            with (
                self.subTest(argv=bad),
                self.assertRaises(SystemExit) as cm,
                redirect_stderr(io.StringIO()),
            ):
                run.main(bad, gql=self.gql, run=self.delegate, driver=self.api, now=lambda: NOW)
            self.assertEqual(cm.exception.code, 2)
        self.nothing_ran()

    def test_bad_issue_or_session_id(self):
        argv = self.argv()
        for flag, value in (("--issue", "task-7"), ("--sid", "not-a-sid"), ("--sid", "z" * 36)):
            with self.subTest(flag=flag, value=value):
                bad = [f"{flag}={value}" if x.startswith(flag + "=") else x for x in argv]
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(
                        run.main(
                            bad, gql=self.gql, run=self.delegate, driver=self.api, now=lambda: NOW
                        ),
                        2,
                    )
                issue, sid = (value, SID) if flag == "--issue" else (ID, value)
                self.assertEqual(
                    err.getvalue(), f"run.py: bad issue or session id: {issue} {sid}\n"
                )
        self.nothing_ran()

    def test_bad_runtime(self):
        for runtime in ("", "cl\x07aude"):
            with self.subTest(runtime=runtime):
                self.assertEqual(self.main(runtime=runtime), 2)
                self.assertEqual(self.err, f"run.py: bad runtime: {runtime!r}\n")
        self.nothing_ran()
