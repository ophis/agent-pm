"""Startup behavior."""

import dataclasses
import os
import signal
import stat
import subprocess
from datetime import datetime
from unittest import mock

from agent_pm.delegate import (
    Begun,
    ConfigError,
    Finished,
    Progress,
    Result,
)

from delegate.tests.replies import bare, block
from delegate.tests.unit.engine.runner.case import FAILED, GH, INVALID, NEEDS, PR, SID, Base
from delegate.tests.unit.runs import (
    DONE,
    START,
    said,
)


class Start(Base):
    """Main's drive_test Start cases."""

    def test_the_record_is_session_input_the_marks_result_and_end(self):
        r = self.go(said(START, block(DONE)), input="  Answers:\n- B\n")
        records, sid = self.records(), self.events[0].sid
        self.assertEqual(
            [e["kind"] for e in records], ["session", "input", "progress", "result", "end"]
        )
        mine = records[1:]
        self.assertEqual(
            [list(e) for e in mine],
            [
                ["ts", "kind", "text"],
                ["ts", "kind", "name", "text"],
                ["ts", "kind", "outcome"],
                ["ts", "kind", "sid", "rc"],
            ],
        )
        for e in mine:
            self.assertIsNotNone(datetime.fromisoformat(e["ts"]).utcoffset(), e["ts"])
        kept = {k: v for k, v in dataclasses.asdict(r.outcome).items() if k != "deliverable"}
        self.assertEqual(
            [bare(e) for e in mine],
            [
                {"kind": "input", "text": "  Answers:\n- B\n"},
                {"kind": "progress", "name": "start", "text": "go"},
                {"kind": "result", "outcome": kept},
                {"kind": "end", "sid": sid, "rc": 0},
            ],
        )
        self.assertEqual(
            (records[0]["sid"], kept["url"], r.outcome.deliverable), (sid, self.out, "# Doc\n")
        )
        self.assertEqual(sorted(os.listdir(self.work)), ["out.md", "run.jsonl"])

    def test_a_destination_decides_the_url_and_each_saves_a_deliverable(self):
        url, pr_url = "https://github.com/o/docs/blob/main/x.md", "https://github.com/o/n/pull/7"
        for output, data, want_url, saved in (
            ({"type": "local"}, DONE, self.out, "# Doc\n"),
            ({"type": "orchestrator"}, DONE, "", "# Doc\n"),
            (GH, {**DONE, "url": url}, url, "# Doc\n"),
            (PR, {**DONE, "url": pr_url}, pr_url, "# Doc\n"),
            ({"type": "local"}, NEEDS, "", None),
            (GH, NEEDS, "", None),
        ):
            with self.subTest(output=output, status=data["status"]):
                self.fresh()
                r = self.go(said(START, block(data)), output=output)
                self.assertEqual(r.outcome.url, want_url)
                self.assertEqual(
                    self.read(self.out) if saved else os.path.lexists(self.out), saved or False
                )
                self.assertEqual(
                    self.last("result")["outcome"]["questions"], data.get("questions", [])
                )

    def test_a_failed_save_raises_where_out_is_the_deliverables_home_else_is_noted(self):
        denied = PermissionError(13, "Permission denied")
        for output in ({"type": "local"}, {"type": "orchestrator"}):
            with self.subTest(output=output):
                self.fresh()
                with mock.patch("os.replace", side_effect=denied), self.assertRaises(OSError):
                    self.go(said(START, block(DONE)), output=output)
                self.assertEqual(
                    (self.last("result")["error"], os.path.lexists(self.out)),
                    (f"stopped: PermissionError: {denied}", False),
                )
        for output, url in (
            (GH, "https://github.com/o/docs/blob/main/x.md"),
            (PR, "https://github.com/o/n/pull/7"),
        ):
            with self.subTest(output=output):
                self.fresh()
                with mock.patch("os.replace", side_effect=denied):
                    r = self.go(said(START, block({**DONE, "url": url})), output=output)
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.outcome.url, r.error), (0, "done", url, "")
                )
                self.assertEqual(self.events[-1], Finished(r))
                self.assertEqual(
                    self.kinds(), ["session", "input", "progress", "save_error", "result", "end"]
                )
                self.assertEqual(
                    bare(self.last("save_error")), {"kind": "save_error", "error": str(denied)}
                )
                self.assertIn(
                    f"delegate: cannot save the deliverable to {self.out}: {denied}\n", self.err
                )
                self.assertFalse(os.path.lexists(self.out))

    def test_without_out_nothing_is_saved(self):
        r = self.go(said(START, block(DONE)), out=None)
        self.assertEqual(
            (r.outcome.status, r.outcome.url, r.outcome.deliverable), ("done", "", "# Doc\n")
        )
        self.assertEqual(os.listdir(self.work), ["run.jsonl"])

    def test_a_stale_out_never_stands_for_the_deliverable(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Doc\n")
        seen = []
        self.go(
            said(START, block(DONE)),
            on_event=lambda e: (
                seen.append(os.path.lexists(self.out)) if isinstance(e, Begun) else None
            ),
        )
        self.assertEqual((seen, save_spy.call_count, self.read(self.out)), ([False], 1, "# Doc\n"))

    def test_a_failed_client_does_not_reuse_a_previous_runs_out(self):
        self.go(said(START, block(DONE)))
        r = self.go(said(START), {"exit": 143})
        self.assertEqual(
            (r, os.path.lexists(self.out)), (Result(143, None, "the client exited 143"), False)
        )

    def test_a_driver_refusal_leaves_out_as_it_was(self):
        for name, kw in (
            ("an unknown runtime", {"runtime": "nope"}),
            ("a bad sid", {"sid": "a/b"}),
        ):
            with self.subTest(name):
                self.write(self.out, "# Earlier\n")
                with self.assertRaises(ConfigError):
                    self.go(said(START, block(DONE)), **kw)
                self.assertEqual(self.read(self.out), "# Earlier\n")

    def test_a_stale_out_that_cannot_be_removed_stops_the_session_as_the_result(self):
        blocker, procs, popen = os.path.join(self.tmp, "file"), [], subprocess.Popen
        self.write(blocker, "")

        def spawn(*args, **kw):
            procs.append(popen(*args, **kw))
            return procs[-1]

        with mock.patch("subprocess.Popen", side_effect=spawn):
            r = self.go(
                {"sleep": 30}, said(START, block(DONE)), out=os.path.join(blocker, "out.md")
            )
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, []))
        self.assertRegex(r.error, r"^cannot remove the stale out: .*Not a directory")
        self.assertEqual(
            (procs[0].returncode, self.kinds()),
            (-signal.SIGKILL, ["session", "input", "result", "end"]),
        )
        self.assertEqual(self.last("result")["error"], r.error)

    def test_a_resume_keeps_the_draft_its_session_wrote_to_out(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Draft\n")
        seen = []
        r = self.go(
            said(block({**DONE, "deliverable": "# Draft\n"})),
            sid=SID,
            resume=True,
            on_event=lambda e: seen.append(self.read(self.out)) if isinstance(e, Begun) else None,
        )
        self.assertEqual(
            (seen, save_spy.call_count, self.read(self.out)), (["# Draft\n"], 0, "# Draft\n")
        )
        argv = self.launches()[0]["argv"]
        self.assertEqual(
            (argv[argv.index("--sid") + 1], "--resume" in argv, r.outcome.status),
            (SID, True, "done"),
        )

    def test_a_deliverable_the_agent_wrote_to_out_is_not_saved_again(self):
        save_spy, written = self.spy_save(), []

        def write(event):
            if isinstance(event, Begun):
                self.write(self.out, "# Doc\n")
                written.append(os.stat(self.out))

        for dest in ("local", "orchestrator"):
            with self.subTest(dest):
                r = self.go(said(START, block(DONE)), output={"type": dest}, on_event=write)
                st = os.stat(self.out)
                self.assertEqual(
                    (st.st_ino, st.st_mtime_ns), (written[-1].st_ino, written[-1].st_mtime_ns)
                )
                self.assertEqual((save_spy.called, self.read(self.out)), (False, "# Doc\n"))
                self.assertEqual(
                    (r.outcome.url, r.outcome.deliverable),
                    (self.out if dest == "local" else "", "# Doc\n"),
                )

    def test_an_out_not_a_regular_file_holding_the_deliverable_is_replaced_never_followed_or_waited_on(
        self,
    ):
        save_spy, target = self.spy_save(), os.path.join(self.tmp, "zshrc")

        def write(text):
            return lambda: self.write(self.out, text)

        def link(text):
            def plant():
                self.write(target, text)
                os.symlink(target, self.out)

            return plant

        cases = [(f"differs: {w!r}", write(w), None) for w in ("", "# Do", "# Doc\n\n", "# Dac\n")]
        cases += [
            ("fifo", lambda: os.mkfifo(self.out), None),
            ("symlink", link("mine"), "mine"),
            ("symlink to the deliverable", link("# Doc\n"), "# Doc\n"),
        ]
        for name, plant, kept in cases:
            with self.subTest(name):
                save_spy.reset_mock()
                self.go(
                    said(START, block(DONE)),
                    on_event=lambda e: plant() if isinstance(e, Begun) else None,
                )
                self.assertEqual(
                    (
                        save_spy.call_count,
                        stat.S_ISREG(os.lstat(self.out).st_mode),
                        self.read(self.out),
                    ),
                    (1, True, "# Doc\n"),
                )
                if kept is not None:
                    self.assertEqual(self.read(target), kept)

    def test_a_valid_outcome_survives_a_nonzero_exit_with_its_diagnostic(self):
        r = self.go(said(START, block(DONE)), {"exit": 7})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (7, "done", "the client exited 7")
        )
        self.assertEqual(
            ([type(e) for e in self.events], self.read(self.out)),
            ([Begun, Progress, Finished], "# Doc\n"),
        )
        saved = self.last("result")
        self.assertEqual(
            (saved["outcome"]["url"], saved["error"], self.last("end")["rc"]),
            (self.out, r.error, 7),
        )

    def test_a_run_without_a_valid_outcome_records_its_error_and_gets_no_finished(self):
        for steps, rc, error in (
            ([said("bye")], 0, "the agent run returned no outcome"),
            ([said("bye"), {"exit": 143}], 143, "the client exited 143"),
            ([said(block(INVALID)), {"exit": 143}], 143, "the client exited 143"),
        ):
            with self.subTest(steps=steps):
                self.fresh()
                r = self.go(*steps)
                self.assertEqual(
                    (r, [type(e) for e in self.events]), (Result(rc, None, error), [Begun])
                )
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": error},
                        {"kind": "end", "sid": self.events[0].sid, "rc": rc},
                    ),
                )

    def test_done_or_failed_without_start_reports_it_missing(self):
        for data in (DONE, FAILED):
            with self.subTest(data["status"]):
                self.fresh()
                r = self.go(said("[agent-pm-progress:round] one", block(data)))
                self.assertEqual(r.outcome.status, data["status"])
                self.assertIn("delegate: missing progress mark: start\n", self.err)
                self.assertEqual(
                    self.kinds(), ["session", "input", "progress", "missing", "result", "end"]
                )
                self.assertEqual(bare(self.last("missing")), {"kind": "missing", "name": "start"})

    def test_no_missing_report_when_start_was_seen_needs_input_resumed_or_no_outcome(self):
        for steps, kw in (
            ([said(START, block(DONE))], {}),
            ([said(block(NEEDS))], {}),
            ([said(block(DONE))], {"sid": SID, "resume": True}),
            ([said("bye")], {}),
        ):
            with self.subTest(steps=steps, **kw):
                self.fresh()
                self.go(*steps, **kw)
                self.assertNotIn("missing", self.err)
                self.assertNotIn("missing", self.kinds())
