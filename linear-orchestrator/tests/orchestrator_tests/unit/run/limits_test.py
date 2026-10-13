import io
import json
import os
from contextlib import redirect_stderr
from unittest import mock

from agent_pm.delegate import Limit, Result
from agent_pm.linear_orchestrator.engine import config, run
from agent_pm.linear_orchestrator.linear import linear, writeback
from orchestrator_tests.unit.board_ids import STATES
from orchestrator_tests.unit.run.data import (
    NOW,
    REPAIR,
    RUNNING,
    RUNTIME,
    T,
    cid,
    created,
    ended,
    updated,
)
from orchestrator_tests.unit.run.fixtures import Case
from orchestrator_tests.unit.run.support import (
    ID,
    KEY,
    SID,
    UUID,
    Gql,
)


class RunUsageLimits(Case):
    def test_a_usage_limit_writes_back_and_notes_it_in_the_runs_log(self):
        for at, said in (
            (T, f"resets at {writeback.local_time(T)}."),
            (None, "reset time unknown."),
        ):
            with self.subTest(at=at):
                self.gql = Gql()
                ledger = os.path.join(self.rd, writeback.LEDGER)
                if os.path.exists(ledger):
                    os.remove(ledger)
                self.done(result=Result(-9, None, "usage limit", Limit(at)))
                n = len(self.said())
                self.assertEqual(self.main(), 0)
                self.assertEqual(
                    self.said()[n:],
                    [
                        f"run launch TASK-7 mode=new sid={SID}",
                        f"run end TASK-7 sid={SID} exit=-9",
                        f"writeback step TASK-7 sid={SID} step=limit",
                    ],
                )
                self.assertEqual(
                    self.gql.calls,
                    [
                        *created(RUNNING),
                        ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                        (
                            "comment",
                            KEY,
                            {"c": cid("limit"), "i": UUID, "b": f"Usage limit, {said}"},
                        ),
                        ("reread", KEY, {"i": UUID}),
                        ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
                        *updated(ended("limit", -9)),
                        REPAIR,
                    ],
                )
                with open(self.runs) as f:
                    last = json.loads(f.read().splitlines()[-1])
                self.assertEqual(
                    {k: v for k, v in last.items() if k != "ts"},
                    {
                        "kind": "limit",
                        "issue": ID,
                        "sid": SID,
                        "runtime": RUNTIME,
                        "resets_at": writeback.local_time(at),
                    },
                )

    def test_a_failed_runs_note_is_a_run_error_and_the_session_comment_still_ends(self):
        os.makedirs(self.runs)
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:],
            [
                f"run end TASK-7 sid={SID} exit=-9",
                f"writeback step TASK-7 sid={SID} step=limit",
                (
                    f"run run-error TASK-7 error=IsADirectoryError: [Errno 21] Is a directory: '"
                    f"{self.runs}'"
                ),
            ],
        )
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])

    def test_any_runs_note_exception_is_a_run_error_and_the_session_comment_still_ends(self):
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        with mock.patch.object(os, "fsync", side_effect=OSError("disk")):
            self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[-1], "run run-error TASK-7 error=OSError: disk")
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])


class RunArguments(Case):
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
